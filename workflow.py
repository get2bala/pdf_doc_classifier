"""Persistent, pollable workflow orchestration.

The coordinator deliberately has no dependency on Qt or a filesystem watcher.
The UI may call :meth:`scan_once`, while :meth:`start` runs the same operation
on a small background thread.  SQLite remains the source of truth, so claimed
jobs can be recovered after an unclean shutdown.
"""

import hashlib
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from classifier import AnalysisPipeline
from pdf_engine import PdfExporter, page_count


AUTOMATION_MODES = {"SUGGESTIONS", "PREASSIGN", "AUTOMATIC"}


def sha256_file(path: str, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def employee_id_from_path(path: str) -> str:
    """A predictable default that operators can correct during review."""
    return Path(path).stem.strip() or "UNKNOWN"


@dataclass(frozen=True)
class StablePdf:
    path: str
    size: int
    mtime_ns: int


class InboxScanner:
    """Return PDFs only after their size and mtime are unchanged across scans."""

    def __init__(self, stable_scans: int = 2):
        if stable_scans < 1:
            raise ValueError("stable_scans must be at least one")
        self.stable_scans = stable_scans
        self._observed = {}  # type: Dict[str, tuple]
        self._emitted = set()

    def scan_once(self, inbox: str) -> List[StablePdf]:
        root = Path(inbox).expanduser()
        if not root.is_dir():
            return []
        present = set()
        stable = []
        for path in sorted(root.iterdir(), key=lambda item: item.name.lower()):
            if not path.is_file() or path.suffix.lower() != ".pdf":
                continue
            # Common scanner/copy temporary names are ignored even if they end
            # in .pdf.  They will be seen after the producer renames them.
            if path.name.startswith((".", "~")):
                continue
            resolved = str(path.resolve())
            present.add(resolved)
            try:
                stat = path.stat()
            except OSError:
                continue
            signature = (stat.st_size, stat.st_mtime_ns)
            previous = self._observed.get(resolved)
            count = previous[1] + 1 if previous and previous[0] == signature else 1
            self._observed[resolved] = (signature, count)
            if count >= self.stable_scans and resolved not in self._emitted:
                self._emitted.add(resolved)
                stable.append(StablePdf(resolved, stat.st_size, stat.st_mtime_ns))
        for missing in set(self._observed) - present:
            self._observed.pop(missing, None)
            self._emitted.discard(missing)
        return stable


class WorkflowCoordinator:
    """Coordinates discovery, OCR/classification and verified generation jobs."""

    def __init__(
        self,
        database,
        inbox: Optional[str] = None,
        output: Optional[str] = None,
        completed: Optional[str] = None,
        error: Optional[str] = None,
        scanner: Optional[InboxScanner] = None,
        analyzer=None,
        exporter=None,
        automation_mode: str = "PREASSIGN",
        automatic_ocr: bool = True,
        minimum_score: float = 80.0,
        minimum_matches: int = 2,
        poll_interval: float = 2.0,
        hash_func: Callable[[str], str] = sha256_file,
        page_counter: Callable[[str], int] = page_count,
    ):
        mode = automation_mode.strip().upper()
        if mode not in AUTOMATION_MODES:
            raise ValueError("Unknown automation mode: {}".format(automation_mode))
        self.database = database
        self.inbox = inbox
        self.output = output
        self.completed = completed
        self.error = error
        self.scanner = scanner or InboxScanner()
        self.analyzer = analyzer
        self.exporter = exporter or PdfExporter(database)
        self.automation_mode = mode
        self.automatic_ocr = bool(automatic_ocr)
        self.minimum_score = float(minimum_score)
        self.minimum_matches = int(minimum_matches)
        self.poll_interval = max(0.1, float(poll_interval))
        self.hash_func = hash_func
        self.page_counter = page_counter
        self._stop_event = threading.Event()
        self._thread = None  # type: Optional[threading.Thread]
        self.last_error = None  # type: Optional[str]
        self.last_notice = None  # type: Optional[str]

    @classmethod
    def from_settings(cls, database, **overrides):
        settings = database.list_settings()
        values = {
            "inbox": settings.get("paths/input"),
            "output": settings.get("paths/output"),
            "completed": settings.get("paths/completed"),
            "error": settings.get("paths/error"),
            "automatic_ocr": settings.get(
                "processing/automatic_text_extraction",
                settings.get("ocr/automatic", True)),
            "automation_mode": settings.get(
                "classification/automation_mode", "PREASSIGN"),
            "minimum_score": settings.get("classification/minimum_score", 80),
            "minimum_matches": settings.get("classification/minimum_matches", 2),
        }
        values.update(overrides)
        return cls(database, **values)

    def reload_settings(self):
        """Apply configuration changes without restarting the application."""
        settings = self.database.list_settings()
        self.inbox = settings.get("paths/input") or None
        self.output = settings.get("paths/output") or None
        self.completed = settings.get("paths/completed") or None
        self.error = settings.get("paths/error") or None
        self.automatic_ocr = bool(settings.get(
            "processing/automatic_text_extraction",
            settings.get("ocr/automatic", True)))
        mode = str(settings.get(
            "classification/automation_mode", "PREASSIGN")).upper()
        if mode not in AUTOMATION_MODES:
            raise ValueError("Unknown automation mode: {}".format(mode))
        self.automation_mode = mode
        self.minimum_score = float(settings.get("classification/minimum_score", 80))
        self.minimum_matches = int(settings.get(
            "classification/minimum_matches", 2))

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self):
        if self.is_running:
            return
        self.database.recover_stale_jobs()
        self._queue_incomplete_text_jobs()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="document-workflow", daemon=True
        )
        self._thread.start()

    def _queue_incomplete_text_jobs(self):
        """Retry failed legacy/extraction pages after restart.

        This also migrates documents processed by the former Tesseract adapter:
        their FAILED page_analysis rows are picked up by
        ``list_pending_ocr_pages`` and replaced by embedded-text results.
        """
        if not self.automatic_ocr:
            return
        for document in self.database.list_source_documents():
            if document.get("generation_status") == "COMPLETE":
                continue
            pages = self.database.list_pending_ocr_pages(document["id"])
            if pages:
                self.database.create_background_job(
                    document["id"], "OCR", len(pages), max_attempts=3)

    def stop(self, timeout: float = 5.0):
        self._stop_event.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self.scan_once()
                # Drain at most one claimed job per pass, allowing a stop
                # request and newly copied files to be noticed promptly.
                self.process_next_job()
                self.last_error = None
            except Exception as exc:  # never silently kill the worker
                self.last_error = str(exc)
            self._stop_event.wait(self.poll_interval)

    def scan_once(self) -> List[int]:
        """Register newly stable PDFs and queue OCR immediately."""
        if not self.inbox:
            return []
        registered = []
        for item in self.scanner.scan_once(self.inbox):
            try:
                identity = self.hash_func(item.path)
                existing = self.database.find_document_by_sha256(identity)
                if existing:
                    self.last_notice = (
                        "Duplicate ignored: {} has the same content as {}.".format(
                            Path(item.path).name,
                            Path(existing["filepath"]).name))
                    continue
                document_id = self.ingest_file(item.path, file_sha256=identity)
                registered.append(document_id)
            except Exception as exc:
                # Invalid files still need a visible dashboard record where
                # possible, but discovery must continue for the other files.
                self.last_error = "{}: {}".format(Path(item.path).name, exc)
        return registered

    def ingest_file(self, path: str, employee_id: Optional[str] = None,
                    file_sha256: Optional[str] = None) -> int:
        """Register one known-complete PDF, used by scanning and Add PDF."""
        resolved = str(Path(path).expanduser().resolve())
        identity = file_sha256 or self.hash_func(resolved)
        existing = self.database.find_document_by_sha256(identity)
        if existing:
            return int(existing["id"])
        try:
            count = self.page_counter(resolved)
        except Exception:
            # Preserve invalid files in the dashboard before optionally moving
            # them to the configured error folder.
            document_id = self.database.register_discovered_document(
                resolved, employee_id or employee_id_from_path(resolved), 0, identity)
            self.database.update_document_workflow(
                document_id, ingestion_status="ERROR", ocr_status="FAILED",
                review_status="NOT_STARTED", generation_status="NOT_READY")
            self._move_invalid_source(document_id)
            raise
        document_id = self.database.register_discovered_document(
            resolved, employee_id or employee_id_from_path(resolved), count, identity)
        if self.automatic_ocr:
            self.database.create_background_job(
                document_id, "OCR", count, max_attempts=3)
        else:
            self.database.update_document_workflow(
                document_id, ocr_status="NOT_STARTED")
        return document_id

    def _move_invalid_source(self, document_id: int):
        if not self.error:
            return
        document = self.database.get_source_document(document_id)
        source = Path(document["filepath"])
        destination_dir = Path(self.error).expanduser().resolve()
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / source.name
        if destination.exists():
            return
        shutil.move(str(source), str(destination))
        with self.database.transaction() as conn:
            conn.execute(
                "UPDATE source_documents SET filepath=? WHERE id=?",
                (str(destination), document_id))

    def process_next_job(self) -> Optional[Dict]:
        job = self.database.claim_next_job(["OCR", "GENERATE"])
        if not job:
            return None
        try:
            if job["job_type"] == "OCR":
                self._run_ocr(job)
            elif job["job_type"] == "GENERATE":
                self._run_generation(job)
            else:
                raise ValueError("Unsupported job type: {}".format(job["job_type"]))
        except Exception as exc:
            self.database.fail_job(job["id"], str(exc))
        return job

    def _run_ocr(self, job: Dict):
        if self.analyzer is None:
            raise RuntimeError("OCR engine is not configured")
        document_id = int(job["source_document_id"])
        completed = int(job.get("items_completed") or 0)
        failed = int(job.get("items_failed") or 0)
        pages = self.database.list_pending_ocr_pages(document_id)
        for page in pages:
            page_number = int(page["page_number"] if isinstance(page, dict) else page)
            try:
                result = self.analyzer.analyze_page(document_id, page_number)
                self._apply_automation(document_id, page_number, result)
                completed += 1
                self.database.update_job_progress(
                    job["id"], items_completed=completed, items_failed=failed
                )
            except Exception as exc:
                failed += 1
                self.database.update_job_progress(
                    job["id"], items_completed=completed, items_failed=failed
                )
                self.last_error = "PDF text page {}: {}".format(page_number, exc)
        self.database.complete_job(job["id"])
        if failed and hasattr(self.database, "update_document_workflow"):
            self.database.update_document_workflow(
                document_id, ocr_status="COMPLETE_WITH_ERRORS")

    @staticmethod
    def _match_count(analysis: Dict) -> int:
        if analysis.get("matched_count") is not None:
            return int(analysis["matched_count"])
        explanation = analysis.get("explanation") or ""
        if not explanation.startswith("Matched:"):
            return 0
        return len([value for value in explanation[8:].split(",") if value.strip()])

    def _apply_automation(self, document_id: int, page_number: int, analysis: Dict):
        category_id = analysis.get("suggested_category_id")
        score = analysis.get("score")
        strong = (
            category_id is not None
            and score is not None
            and float(score) >= self.minimum_score
            and self._match_count(analysis) >= self.minimum_matches
        )
        if self.automation_mode == "SUGGESTIONS" or not strong:
            return
        assignment_status = (
            "NEEDS_REVIEW" if self.automation_mode == "PREASSIGN" else "ASSIGNED"
        )
        self.database.assign_pages(
            document_id, [page_number], int(category_id), assignment_status
        )
        if self.automation_mode == "AUTOMATIC":
            self.database.mark_analysis(document_id, [page_number], "ACCEPTED")

    def queue_generation(self, document_id: int) -> int:
        if not self.output:
            raise ValueError("Configure an output folder before generating")
        assignments = self.database.get_page_assignments(document_id)
        unresolved = [
            row["page_number"]
            for row in assignments
            if row["status"] in ("UNCLASSIFIED", "NEEDS_REVIEW")
        ]
        if unresolved:
            raise ValueError("Document has unresolved pages: {}".format(
                ", ".join(map(str, unresolved))
            ))
        plan = self.exporter.build_plan(document_id, self.output)
        return self.database.create_background_job(
            document_id, "GENERATE", len(plan), max_attempts=3
        )

    def retry_failed(self, document_id: int) -> List[int]:
        """Queue only the work that is still incomplete for a document."""
        jobs = []
        pages = self.database.list_pending_ocr_pages(document_id)
        if pages:
            jobs.append(self.database.create_background_job(
                document_id, "OCR", len(pages), max_attempts=3
            ))
        document = self.database.get_source_document(document_id)
        if document and document.get("generation_status") == "FAILED":
            jobs.append(self.queue_generation(document_id))
        return jobs

    def _run_generation(self, job: Dict):
        if not self.output:
            raise ValueError("Output folder is not configured")
        document_id = int(job["source_document_id"])
        plan = self.exporter.build_plan(document_id, self.output)
        run_id = self.database.create_generation_run(
            document_id, self.output, len(plan), job_id=job["id"]
        )
        try:
            paths = self.exporter.export(document_id, self.output)
            if len(paths) != len(plan):
                raise IOError("Generation returned an incomplete output batch")
            for item, path in zip(plan, paths):
                actual_pages = self.page_counter(path)
                expected_pages = len(item["pages"])
                if actual_pages != expected_pages or os.path.getsize(path) <= 0:
                    raise IOError("Output verification failed: {}".format(path))
                self.database.record_output_file(
                    run_id, document_id, path, expected_pages,
                    category_id=item.get("category_id"),
                    actual_page_count=actual_pages, file_size=os.path.getsize(path),
                    sha256=self.hash_func(path), status="VERIFIED"
                )
                self.database.update_job_progress(
                    job["id"], items_completed=len(
                        self.database.get_completion_manifest(document_id)["outputs"])
                )
            if not self.database.complete_generation_run(run_id):
                raise IOError("Generation manifest verification failed")
        except Exception as exc:
            self.database.complete_generation_run(run_id, error_message=str(exc))
            raise
        self.database.complete_job(job["id"])
        if self.completed:
            try:
                self._move_completed_source(document_id)
            except Exception as exc:
                # Outputs are already verified and generation is complete.
                # Keeping the source in place is safer than falsely changing
                # the completed generation into a failed/retryable job.
                self.last_error = "Outputs verified, but source was not moved: {}".format(exc)

    def _move_completed_source(self, document_id: int):
        document = self.database.get_source_document(document_id)
        source = Path(document["filepath"])
        destination_dir = Path(self.completed).expanduser().resolve()
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / source.name
        if destination.exists():
            raise FileExistsError(
                "Completed source already exists; source was not moved: {}".format(destination)
            )
        shutil.move(str(source), str(destination))
        # Preserve the new location for manifest/open-source actions.
        with self.database.transaction() as conn:
            conn.execute(
                "UPDATE source_documents SET filepath=? WHERE id=?",
                (str(destination), document_id),
            )
