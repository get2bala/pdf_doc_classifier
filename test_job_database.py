import sqlite3

from database import DatabaseManager, SCHEMA_VERSION


def register(db, tmp_path, name="EMP1.pdf", digest="abc", pages=3):
    path = tmp_path / name
    path.write_bytes(b"%PDF test")
    return db.register_discovered_document(str(path), "EMP1", pages, digest)


def test_discovery_is_idempotent_by_content_hash(tmp_path):
    db = DatabaseManager()
    first = register(db, tmp_path)
    second = db.register_discovered_document(
        str(tmp_path / "renamed.pdf"), "OTHER", 99, "abc")
    assert first == second
    assert db.find_document_by_sha256("abc")["page_count"] == 3
    assert db.list_pending_ocr_pages(first) == [1, 2, 3]


def test_job_lifecycle_progress_retry_and_completion(tmp_path):
    db = DatabaseManager()
    document_id = register(db, tmp_path)
    job_id = db.create_background_job(document_id, "OCR", 3, max_attempts=2)
    assert db.create_background_job(document_id, "OCR", 3) == job_id
    claimed = db.claim_next_job(["OCR"])
    assert claimed["id"] == job_id
    assert claimed["attempt_count"] == 1
    assert db.update_job_progress(job_id, 2, 1)
    assert db.fail_job(job_id, "temporary")
    assert db.get_job(job_id)["status"] == "QUEUED"
    assert db.claim_next_job()["attempt_count"] == 2
    assert db.complete_job(job_id)
    job = db.get_job(job_id)
    assert job["status"] == "COMPLETE"
    assert job["items_completed"] == 3
    assert db.get_source_document(document_id)["ocr_status"] == "COMPLETE"


def test_stale_job_recovery_requeues_then_exhausts(tmp_path):
    db = DatabaseManager()
    document_id = register(db, tmp_path)
    job_id = db.enqueue_job(document_id, "OCR", 3, max_attempts=2)
    db.claim_next_job()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE background_jobs SET heartbeat_at='2000-01-01' WHERE id=?",
            (job_id,))
    assert db.recover_stale_jobs(1) == 1
    assert db.get_job(job_id)["status"] == "QUEUED"
    db.claim_next_job()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE background_jobs SET heartbeat_at='2000-01-01' WHERE id=?",
            (job_id,))
    assert db.recover_stale_jobs(1) == 1
    assert db.get_job(job_id)["status"] == "FAILED"


def test_verified_generation_manifest_controls_completion(tmp_path):
    db = DatabaseManager()
    document_id = register(db, tmp_path)
    run_id = db.create_generation_run(document_id, str(tmp_path), 2)
    db.record_output_file(
        run_id, document_id, str(tmp_path / "one.pdf"), 1,
        actual_page_count=1, file_size=100, sha256="one", status="VERIFIED")
    assert not db.complete_generation_run(run_id)
    assert db.get_completion_manifest(document_id)["status"] == "FAILED"

    run_id = db.create_generation_run(document_id, str(tmp_path), 1)
    output_id = db.record_output_file(
        run_id, document_id, str(tmp_path / "two.pdf"), 2,
        actual_page_count=2, file_size=200, sha256="two", status="VERIFIED")
    assert output_id
    assert db.complete_generation_run(run_id)
    manifest = db.get_completion_manifest(document_id)
    assert manifest["status"] == "COMPLETE"
    assert manifest["verified_outputs"] == 1
    assert manifest["outputs"][0]["sha256"] == "two"


def test_dashboard_and_viewer_include_workflow_tables(tmp_path):
    db = DatabaseManager()
    document_id = register(db, tmp_path)
    job_id = db.create_background_job(document_id, "OCR", 3)
    db.claim_next_job()
    summary = db.get_dashboard_summary()
    assert summary["OCR_RUNNING"] == 1
    assert db.list_dashboard_documents("OCR_RUNNING")[0]["id"] == document_id
    for table in ("background_jobs", "generation_runs", "output_files"):
        assert table in db.list_database_tables()
        assert db.query_table(table)["table"] == table
    assert db.get_job(job_id)["status"] == "RUNNING"


def test_version_two_database_migrates_without_data_loss(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE source_documents (
          id INTEGER PRIMARY KEY, filepath TEXT NOT NULL UNIQUE,
          employee_id TEXT NOT NULL, page_count INTEGER NOT NULL DEFAULT 0,
          file_size INTEGER NOT NULL DEFAULT 0, file_mtime_ns INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'IN_PROGRESS', created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        PRAGMA user_version=2;
        INSERT INTO source_documents(filepath,employee_id) VALUES('/tmp/a.pdf','A');
    """)
    conn.close()
    db = DatabaseManager(str(path))
    assert db.get_source_document(1)["ocr_status"] == "NOT_STARTED"
    with db.get_connection() as migrated:
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
