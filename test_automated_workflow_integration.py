import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

from pypdf import PdfWriter

from database import DatabaseManager
from workflow import InboxScanner, WorkflowCoordinator


def make_pdf(path, pages=2):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=300)
    with path.open("wb") as handle:
        writer.write(handle)


class Analyzer:
    def __init__(self, database, category_id):
        self.database = database
        self.category_id = category_id

    def analyze_page(self, document_id, page_number):
        self.database.save_analysis(
            document_id, page_number, "alpha beta", self.category_id, 100,
            "Matched: alpha, beta", "PENDING")
        return self.database.get_analysis(document_id, page_number)


def configured_database(tmp_path, automatic=True):
    database = DatabaseManager(str(tmp_path / "automated.sqlite3"))
    database.set_setting("paths/input", str(tmp_path / "inbox"))
    database.set_setting("paths/output", str(tmp_path / "output"))
    database.set_setting("paths/completed", str(tmp_path / "completed"))
    database.set_setting("paths/error", str(tmp_path / "error"))
    database.set_setting("ocr/automatic", automatic)
    database.set_setting("classification/automation_mode", "AUTOMATIC")
    database.set_setting("classification/minimum_score", 80)
    database.set_setting("classification/minimum_matches", 2)
    return database


def test_db_settings_drive_discovery_and_ocr_without_opening_document(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    source = inbox / "EMP42.pdf"
    make_pdf(source)
    database = configured_database(tmp_path)
    category = database.add_category(
        "Identity", "alpha,beta", "COMBINE",
        "{employee_id}_{category}.pdf")
    coordinator = WorkflowCoordinator.from_settings(
        database, scanner=InboxScanner(1),
        analyzer=Analyzer(database, category))

    [document_id] = coordinator.scan_once()
    assert database.get_source_document(document_id)["ocr_status"] == "QUEUED"
    coordinator.process_next_job()
    document = database.get_source_document(document_id)
    assert document["ocr_status"] == "COMPLETE"
    assert document["review_status"] == "READY"
    assert all(row["status"] == "ASSIGNED"
               for row in database.get_page_assignments(document_id))
    dashboard = database.list_dashboard_documents("READY_TO_GENERATE")
    assert dashboard[0]["id"] == document_id
    assert dashboard[0]["review_completed"] == 2
    database.close()


def test_discovery_can_register_without_automatic_ocr(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    make_pdf(inbox / "manual.pdf", 1)
    database = configured_database(tmp_path, automatic=False)
    coordinator = WorkflowCoordinator.from_settings(
        database, scanner=InboxScanner(1))
    [document_id] = coordinator.scan_once()
    assert database.get_source_document(document_id)["ocr_status"] == "NOT_STARTED"
    assert database.query_table("background_jobs")["total"] == 0
    database.close()


def test_invalid_discovered_pdf_is_visible_and_moved_to_error(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    source = inbox / "broken.pdf"
    source.write_bytes(b"not a PDF")
    database = configured_database(tmp_path)
    coordinator = WorkflowCoordinator.from_settings(
        database, scanner=InboxScanner(1))
    assert coordinator.scan_once() == []
    [document] = database.list_source_documents()
    assert document["ingestion_status"] == "ERROR"
    assert document["ocr_status"] == "FAILED"
    assert Path(document["filepath"]).parent == (tmp_path / "error").resolve()
    assert Path(document["filepath"]).is_file()
    assert database.get_dashboard_summary()["ERRORS"] == 1
    database.close()


def test_full_automatic_real_pdf_generation_manifest_and_source_move(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    source = inbox / "EMP99.pdf"
    make_pdf(source, 2)
    database = configured_database(tmp_path)
    category = database.add_category(
        "Identity", "alpha,beta", "COMBINE",
        "{employee_id}_{category}.pdf")
    coordinator = WorkflowCoordinator.from_settings(
        database, scanner=InboxScanner(1),
        analyzer=Analyzer(database, category))

    [document_id] = coordinator.scan_once()
    coordinator.process_next_job()  # OCR and automatic assignment
    coordinator.queue_generation(document_id)
    coordinator.process_next_job()  # real staged/verified PDF export

    manifest = database.get_completion_manifest(document_id)
    assert manifest["status"] == "COMPLETE"
    assert manifest["verified_outputs"] == 1
    [output] = manifest["outputs"]
    assert output["status"] == "VERIFIED"
    assert output["sha256"]
    assert output["actual_page_count"] == 2
    assert Path(output["output_path"]).is_file()
    moved = Path(database.get_source_document(document_id)["filepath"])
    assert moved.parent == (tmp_path / "completed").resolve()
    assert moved.is_file()
    assert database.get_dashboard_summary()["COMPLETED"] == 1
    database.close()


def test_startup_requeues_failed_legacy_text_pages(tmp_path):
    source = tmp_path / "legacy.pdf"
    make_pdf(source, 1)
    database = configured_database(tmp_path)
    document = database.register_discovered_document(
        str(source), "LEGACY", 1, "legacy-hash")
    database.save_analysis(
        document, 1, "", None, None, "OCR failed", "FAILED",
        "Tesseract executable was not found")
    database.update_document_workflow(
        document, ocr_status="COMPLETE_WITH_ERRORS")
    coordinator = WorkflowCoordinator.from_settings(
        database, poll_interval=0.1)
    coordinator.start()
    coordinator.stop()
    jobs = database.query_table("background_jobs")["rows"]
    assert any(job["source_document_id"] == document
               and job["job_type"] == "OCR" for job in jobs)
    database.close()
