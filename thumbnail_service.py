"""Bounded thumbnail rendering and filesystem caching.

The service is deliberately independent of Qt and SQLite so callers may run it
from any worker/executor appropriate to their UI.  A renderer is injectable for
tests and alternative PDF backends.
"""

from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import tempfile
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from PIL import Image, UnidentifiedImageError

from pdf_engine import render_page


class ThumbnailError(RuntimeError):
    """Base class for controlled thumbnail failures."""


class InvalidPageError(ThumbnailError):
    """Raised when a requested page is invalid or was not rendered."""


class CorruptThumbnailError(ThumbnailError):
    """Raised when an existing cache entry is not a valid image."""


@dataclass(frozen=True)
class ThumbnailRequest:
    source_path: Path
    source_hash: str
    page_number: int
    dimensions: Tuple[int, int]

    def __post_init__(self):
        object.__setattr__(self, "source_path", Path(self.source_path))
        object.__setattr__(self, "dimensions", tuple(self.dimensions))


@dataclass(frozen=True)
class ThumbnailResult:
    request: ThumbnailRequest
    path: Path


Renderer = Callable[
    [Path, Sequence[int], Tuple[int, int]], Mapping[int, Image.Image]
]


def _default_renderer(
    source_path: Path,
    page_numbers: Sequence[int],
    dimensions: Tuple[int, int],
) -> Dict[int, Image.Image]:
    width, height = dimensions
    rendered = {}
    for page_number in page_numbers:
        image = render_page(str(source_path), page_number)
        image.thumbnail((width, height), Image.Resampling.LANCZOS)
        rendered[page_number] = image.convert("RGB")
    return rendered


class ThumbnailService:
    """Render and cache at most 50 thumbnails per request batch."""

    MAX_BATCH_SIZE = 50

    def __init__(
        self,
        cache_dir,
        *,
        renderer: Optional[Renderer] = None,
        version: str = "1",
        memory_capacity: int = 100,
    ):
        if memory_capacity < 0:
            raise ValueError("memory_capacity must not be negative")
        self.cache_dir = Path(cache_dir)
        self.renderer = renderer or _default_renderer
        self.version = str(version)
        self.memory_capacity = memory_capacity
        self._memory_cache = OrderedDict()

    @property
    def memory_cache_size(self) -> int:
        return len(self._memory_cache)

    def cache_key(self, request: ThumbnailRequest) -> str:
        identity = {
            "source_hash": request.source_hash,
            "page_number": request.page_number,
            "dimensions": list(request.dimensions),
            "renderer_version": self.version,
        }
        encoded = json.dumps(
            identity, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    def cache_path(self, request: ThumbnailRequest) -> Path:
        return self.cache_dir / "{}.jpg".format(self.cache_key(request))

    def get_batch(
        self, requests: Iterable[ThumbnailRequest]
    ) -> List[ThumbnailResult]:
        batch = list(requests)
        if len(batch) > self.MAX_BATCH_SIZE:
            raise ValueError("thumbnail batch cannot exceed 50 items")
        for request in batch:
            self._validate_request(request)

        missing = []
        for request in batch:
            path = self.cache_path(request)
            if path.exists():
                self._load_cached(request, path)
            else:
                missing.append(request)

        groups = defaultdict(list)
        for request in missing:
            groups[(request.source_path, request.dimensions)].append(request)
        for (source_path, dimensions), group in groups.items():
            page_numbers = [request.page_number for request in group]
            images = self.renderer(source_path, page_numbers, dimensions)
            for request in group:
                image = images.get(request.page_number)
                if image is None:
                    raise InvalidPageError(
                        "renderer did not return page {}".format(
                            request.page_number
                        )
                    )
                self._store(request, image)

        return [
            ThumbnailResult(request=request, path=self.cache_path(request))
            for request in batch
        ]

    def _validate_request(self, request: ThumbnailRequest) -> None:
        if request.page_number <= 0:
            raise InvalidPageError("page number must be positive")
        if (
            len(request.dimensions) != 2
            or request.dimensions[0] <= 0
            or request.dimensions[1] <= 0
        ):
            raise ValueError("thumbnail dimensions must be positive")
        if not request.source_hash:
            raise ValueError("source_hash is required")

    def _remember(self, key: str, image: Image.Image) -> None:
        if self.memory_capacity == 0:
            return
        self._memory_cache[key] = image
        self._memory_cache.move_to_end(key)
        while len(self._memory_cache) > self.memory_capacity:
            self._memory_cache.popitem(last=False)

    def _load_cached(self, request: ThumbnailRequest, path: Path) -> None:
        key = self.cache_key(request)
        if key in self._memory_cache:
            self._memory_cache.move_to_end(key)
            return
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                loaded = image.convert("RGB").copy()
        except (OSError, UnidentifiedImageError) as exc:
            raise CorruptThumbnailError(
                "cached thumbnail is corrupt: {}".format(path)
            ) from exc
        self._remember(key, loaded)

    def _store(self, request: ThumbnailRequest, image: Image.Image) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        destination = self.cache_path(request)
        converted = image.convert("RGB")
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=".thumbnail-",
                suffix=".jpg",
                dir=str(self.cache_dir),
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
            converted.save(str(temporary_path), format="JPEG", quality=85)
            temporary_path.replace(destination)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
        self._remember(self.cache_key(request), converted.copy())

