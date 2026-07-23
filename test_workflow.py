from pathlib import Path

import pytest

from database import DatabaseManager
from workflow import InboxScanner, WorkflowCoordinator, sha256_file


class FakeDatabase:
    def __init__(self):
        self.documents = {}
        self.hashes = {}
        self.jobs = []
        self.assignments = {}
        self.analyses = {}
        self.outputs = []
        self.runs = []
        self._next = 1

    def list_settings(self):
        return {}

    def recover_stale_jobs(self):
        return 0

    def find_document_by_sha256(self, value):
        return self.hashes.get(value)

    def register_discovered_document(self, path, employee, pages, digest):
        identifier = self._next
        self._next += 1
        self.documents[identifier] = {
            "id": identifier, "filepath": path, "employee_id": employee,
            "page_count": pages, "generation_status": "NOT_READY",
        }
        self.hashes[digest] = self.documents[identifier]
        self.assignments[identifier] = [
            {"page_number": page, "status": "UNCLASSIFIED"}
            for page in range(1, pages + 1)
        ]
        return identifier

    def create_background_job(self, document_id, kind, total, max_attempts=3):
        identifier = len(self.jobs) + 1
        self.jobs.append({
            "id": identifier, "source_document_id": document_id,
            "job_type": kind, "items_total": total, "status": "QUEUED",
            "items_completed": 0, "items_failed": 0,
        })
        return identifier

    def claim_next_job(self, kinds=None):
        for job in self.jobs:
            if job["status"] == "QUEUED" and (not kinds or job["job_type"] in kinds):
                job["status"] = "RUNNING"
                return dict(job)
        return None

    def update_job_progress(self, job_id, items_completed=None, items_failed=None,
                            items_total=None):
        job = self.jobs[job_id - 1]
        if items_completed is not None:
            job["items_completed"] = items_completed
        if items_failed is not None:
            job["items_failed"] = items_failed
        if items_total is not None:
            job["items_total"] = items_total

    def complete_job(self, job_id):
        self.jobs[job_id - 1]["status"] = "COMPLETE"

    def fail_job(self, job_id, error):
        self.jobs[job_id - 1].update(status="FAILED", error_message=error)

    def list_pending_ocr_pages(self, document_id):
        return [
            row["page_number"] for row in self.assignments[document_id]
            if row["page_number"] not in self.analyses
        ]

    def assign_pages(self, document_id, pages, category, status):
        for page in pages:
            self.assignments[document_id][page - 1].update(
                status=status, category_id=category
            )

    def mark_analysis(self, document_id, pages, status):
        for page in pages:
            self.analyses[(document_id, page)]["status"] = status

    def get_page_assignments(self, document_id):
        return self.assignments[document_id]

    def get_source_document(self, document_id):
        return self.documents.get(document_id)

    def create_generation_run(self, document_id, output, count, job_id=None):
        self.runs.append({"id": len(self.runs) + 1, "status": "RUNNING"})
        return len(self.runs)

    def record_output_file(self, *args, **kwargs):
        self.outputs.append((args, kwargs))

    def complete_generation_run(self, run_id, error_message=None):
        self.runs[run_id - 1]["status"] = "COMPLETE"
        return True

    def get_completion_manifest(self, document_id):
        return {"outputs": self.outputs}


class FakeAnalyzer:
    def __init__(self, database, fail_page=None):
        self.database = database
        self.fail_page = fail_page

    def analyze_page(self, document_id, page):
        if page == self.fail_page:
            raise RuntimeError("bad scan")
        result = {
            "suggested_category_id": 7, "score": 100,
            "matched_count": 2, "explanation": "Matched: alpha, beta",
            "status": "PENDING",
        }
        self.database.analyses[(document_id, page)] = result
        return result


class FakeExporter:
    def __init__(self, output):
        self.output = Path(output)

    def build_plan(self, document_id, output):
        return [{"path": str(self.output / "result.pdf"), "pages": [1, 2]}]

    def export(self, document_id, output):
        target = self.output / "result.pdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"generated")
        return [str(target)]


def add_document(db, source, pages=2):
    digest = sha256_file(str(source))
    return db.register_discovered_document(str(source), source.stem, pages, digest)


def test_scanner_requires_unchanged_observation(tmp_path):
    pdf = tmp_path / "EMP1.pdf"
    pdf.write_bytes(b"partial")
    scanner = InboxScanner(stable_scans=2)
    assert scanner.scan_once(str(tmp_path)) == []
    assert [item.path for item in scanner.scan_once(str(tmp_path))] == [str(pdf.resolve())]
    assert scanner.scan_once(str(tmp_path)) == []


def test_scan_registers_and_queues_ocr_and_deduplicates_content(tmp_path):
    first = tmp_path / "EMP1.pdf"
    second = tmp_path / "copy.pdf"
    first.write_bytes(b"same")
    second.write_bytes(b"same")
    db = FakeDatabase()
    coordinator = WorkflowCoordinator(
        db, inbox=str(tmp_path), scanner=InboxScanner(1),
        page_counter=lambda path: 3,
    )
    registered = coordinator.scan_once()
    assert len(registered) == 1
    assert db.jobs[0]["job_type"] == "OCR"
    assert db.jobs[0]["items_total"] == 3


@pytest.mark.parametrize("mode,status", [
    ("SUGGESTIONS", "UNCLASSIFIED"),
    ("PREASSIGN", "NEEDS_REVIEW"),
    ("AUTOMATIC", "ASSIGNED"),
])
def test_ocr_automation_modes(tmp_path, mode, status):
    source = tmp_path / "EMP.pdf"
    source.write_bytes(b"pdf")
    db = FakeDatabase()
    document_id = add_document(db, source)
    db.create_background_job(document_id, "OCR", 2)
    coordinator = WorkflowCoordinator(
        db, analyzer=FakeAnalyzer(db), automation_mode=mode
    )
    coordinator.process_next_job()
    assert [row["status"] for row in db.assignments[document_id]] == [status, status]
    assert db.jobs[0]["items_completed"] == 2
    assert db.jobs[0]["status"] == "COMPLETE"


def test_ocr_page_failure_does_not_stop_document(tmp_path):
    source = tmp_path / "EMP.pdf"
    source.write_bytes(b"pdf")
    db = FakeDatabase()
    document_id = add_document(db, source)
    db.create_background_job(document_id, "OCR", 2)
    coordinator = WorkflowCoordinator(db, analyzer=FakeAnalyzer(db, fail_page=1))
    coordinator.process_next_job()
    assert db.jobs[0]["items_completed"] == 1
    assert db.jobs[0]["items_failed"] == 1
    assert db.jobs[0]["status"] == "COMPLETE"


def test_generation_is_verified_recorded_then_source_moved(tmp_path):
    inbox = tmp_path / "inbox"
    output = tmp_path / "output"
    completed = tmp_path / "completed"
    inbox.mkdir()
    source = inbox / "EMP.pdf"
    source.write_bytes(b"source")
    db = FakeDatabase()
    document_id = add_document(db, source)
    for row in db.assignments[document_id]:
        row["status"] = "ASSIGNED"
    exporter = FakeExporter(output)
    coordinator = WorkflowCoordinator(
        db, output=str(output), completed=str(completed), exporter=exporter,
        page_counter=lambda path: 2,
    )
    coordinator.queue_generation(document_id)
    # Fake DB does not provide SQL; disable only the final metadata update while
    # still exercising the ordering and move guarantee.
    coordinator._move_completed_source = lambda doc_id: shutil_move(
        Path(db.documents[doc_id]["filepath"]), completed
    )
    coordinator.process_next_job()
    assert db.jobs[0]["status"] == "COMPLETE"
    assert db.runs[0]["status"] == "COMPLETE"
    assert db.outputs
    assert (completed / "EMP.pdf").is_file()
    assert not source.exists()


def shutil_move(source, completed):
    completed.mkdir(parents=True, exist_ok=True)
    source.rename(completed / source.name)


def test_generation_not_queued_with_unresolved_pages(tmp_path):
    source = tmp_path / "EMP.pdf"
    source.write_bytes(b"source")
    db = FakeDatabase()
    document_id = add_document(db, source)
    coordinator = WorkflowCoordinator(
        db, output=str(tmp_path / "out"), exporter=FakeExporter(tmp_path / "out")
    )
    with pytest.raises(ValueError, match="unresolved"):
        coordinator.queue_generation(document_id)


def test_real_database_background_ocr_and_generation_manifest(tmp_path):
    inbox = tmp_path / "inbox"
    output = tmp_path / "output"
    completed = tmp_path / "completed"
    inbox.mkdir()
    source = inbox / "EMP900.pdf"
    source.write_bytes(b"stable-source")
    db = DatabaseManager(str(tmp_path / "workflow.sqlite3"))
    category_id = db.add_category("Identity", "alpha,beta")
    coordinator = WorkflowCoordinator(
        db, inbox=str(inbox), output=str(output), completed=str(completed),
        scanner=InboxScanner(1), page_counter=lambda path: 2,
        exporter=FakeExporter(output), automation_mode="AUTOMATIC",
    )

    document_id = coordinator.scan_once()[0]

    class RealDbAnalyzer:
        def analyze_page(self, doc_id, page):
            db.save_analysis(
                doc_id, page, "alpha beta", category_id, 100,
                "Matched: alpha, beta", "PENDING"
            )
            return db.get_analysis(doc_id, page)

    coordinator.analyzer = RealDbAnalyzer()
    coordinator.process_next_job()
    assert all(
        row["status"] == "ASSIGNED"
        for row in db.get_page_assignments(document_id)
    )

    coordinator.queue_generation(document_id)
    coordinator.process_next_job()
    manifest = db.get_completion_manifest(document_id)
    assert manifest["status"] == "COMPLETE"
    assert manifest["verified_outputs"] == 1
    assert manifest["outputs"][0]["status"] == "VERIFIED"
    assert (completed / source.name).is_file()
    assert db.get_source_document(document_id)["filepath"] == str(
        (completed / source.name).resolve()
    )
    db.close()
