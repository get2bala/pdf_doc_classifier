"""Contract tests for the bounded, filesystem-backed thumbnail service.

These tests intentionally describe the public API before ``thumbnail_service``
is implemented.  The service owns thumbnail caching only; database persistence
and UI scheduling are deliberately outside its contract.
"""

from pathlib import Path

import pytest
from PIL import Image

from thumbnail_service import (
    CorruptThumbnailError,
    InvalidPageError,
    ThumbnailRequest,
    ThumbnailService,
)


class RecordingRenderer:
    """Small injectable renderer that records PDF-open-sized batch calls."""

    def __init__(self):
        self.calls = []

    def __call__(self, source_path, page_numbers, dimensions):
        self.calls.append((Path(source_path), tuple(page_numbers), dimensions))
        return {
            page: Image.new("RGB", dimensions, color=(page, 20, 30))
            for page in page_numbers
        }


def requests(source: Path, source_hash: str, count: int, dimensions=(200, 280)):
    return [
        ThumbnailRequest(
            source_path=source,
            source_hash=source_hash,
            page_number=page,
            dimensions=dimensions,
        )
        for page in range(1, count + 1)
    ]


def test_batch_is_explicitly_bounded_to_fifty_items(tmp_path):
    renderer = RecordingRenderer()
    service = ThumbnailService(tmp_path / "cache", renderer=renderer)

    assert len(service.get_batch(requests(tmp_path / "a.pdf", "hash-a", 50))) == 50
    with pytest.raises(ValueError, match="50"):
        service.get_batch(requests(tmp_path / "a.pdf", "hash-a", 51))


def test_cache_key_is_deterministic_and_covers_every_render_identity_field(tmp_path):
    service = ThumbnailService(tmp_path / "cache", renderer=RecordingRenderer(), version="v3")
    base = ThumbnailRequest(tmp_path / "a.pdf", "source-a", 2, (200, 280))

    assert service.cache_key(base) == service.cache_key(base)
    assert service.cache_key(base) != service.cache_key(
        ThumbnailRequest(tmp_path / "renamed.pdf", "source-b", 2, (200, 280))
    )
    assert service.cache_key(base) != service.cache_key(
        ThumbnailRequest(tmp_path / "a.pdf", "source-a", 3, (200, 280))
    )
    assert service.cache_key(base) != service.cache_key(
        ThumbnailRequest(tmp_path / "a.pdf", "source-a", 2, (300, 420))
    )
    assert service.cache_key(base) != ThumbnailService(
        tmp_path / "other-cache", renderer=RecordingRenderer(), version="v4"
    ).cache_key(base)


def test_missing_thumbnails_are_rendered_as_jpegs_and_reused_from_filesystem_cache(
    tmp_path,
):
    renderer = RecordingRenderer()
    cache_dir = tmp_path / "cache"
    request = requests(tmp_path / "a.pdf", "hash-a", 1)[0]

    first = ThumbnailService(cache_dir, renderer=renderer).get_batch([request])
    assert len(renderer.calls) == 1
    assert first[0].path.parent == cache_dir
    assert first[0].path.suffix.lower() in {".jpg", ".jpeg"}
    assert first[0].path.read_bytes().startswith(b"\xff\xd8")

    # A new service instance proves reuse comes from disk, not process memory.
    never_called = RecordingRenderer()
    second = ThumbnailService(cache_dir, renderer=never_called).get_batch([request])
    assert never_called.calls == []
    assert second[0].path == first[0].path


def test_pages_from_the_same_pdf_are_rendered_in_one_batch_call(tmp_path):
    renderer = RecordingRenderer()
    service = ThumbnailService(tmp_path / "cache", renderer=renderer)
    source = tmp_path / "employee.pdf"

    service.get_batch(requests(source, "employee-hash", 4))

    assert renderer.calls == [(source, (1, 2, 3, 4), (200, 280))]


def test_changed_source_identity_never_reuses_an_old_thumbnail(tmp_path):
    renderer = RecordingRenderer()
    service = ThumbnailService(tmp_path / "cache", renderer=renderer)
    source = tmp_path / "employee.pdf"

    old_result = service.get_batch(requests(source, "old-hash", 1))[0]
    new_result = service.get_batch(requests(source, "new-hash", 1))[0]

    assert len(renderer.calls) == 2
    assert old_result.path != new_result.path


def test_memory_cache_is_lru_bounded_without_deleting_disk_cache(tmp_path):
    renderer = RecordingRenderer()
    service = ThumbnailService(
        tmp_path / "cache", renderer=renderer, memory_capacity=2
    )
    batch = requests(tmp_path / "a.pdf", "hash-a", 3)

    service.get_batch(batch)

    assert service.memory_cache_size == 2
    assert len(list((tmp_path / "cache").glob("*.jpg"))) == 3


def test_invalid_page_and_renderer_omission_have_controlled_errors(tmp_path):
    service = ThumbnailService(tmp_path / "cache", renderer=RecordingRenderer())
    with pytest.raises(InvalidPageError, match="positive"):
        service.get_batch(
            [ThumbnailRequest(tmp_path / "a.pdf", "hash-a", 0, (200, 280))]
        )

    def missing_page_renderer(source_path, page_numbers, dimensions):
        return {}

    missing = ThumbnailService(tmp_path / "other", renderer=missing_page_renderer)
    with pytest.raises(InvalidPageError, match="page 1"):
        missing.get_batch(requests(tmp_path / "a.pdf", "hash-a", 1))


def test_corrupt_cached_jpeg_is_reported_and_not_returned(tmp_path):
    service = ThumbnailService(tmp_path / "cache", renderer=RecordingRenderer())
    request = requests(tmp_path / "a.pdf", "hash-a", 1)[0]
    cache_path = service.cache_path(request)
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(b"not a jpeg")

    with pytest.raises(CorruptThumbnailError, match="corrupt"):
        service.get_batch([request])

