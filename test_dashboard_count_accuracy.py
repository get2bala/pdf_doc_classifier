"""Regression specifications for dashboard counters.

These tests intentionally describe user-visible facts rather than mirroring the
implementation's workflow flags.  A dashboard count is only useful when it
matches the rows and progress shown to the operator.
"""

from database import DatabaseManager


def _document(db, name, pages=1):
    return db.add_source_document("/tmp/{}".format(name), "EMP", pages)


def _verified_run(db, document_id, suffix):
    run_id = db.create_generation_run(document_id, "/tmp/output", 1)
    db.record_output_file(
        run_id,
        document_id,
        "/tmp/output/{}.pdf".format(suffix),
        1,
        actual_page_count=1,
        file_size=100,
        sha256=suffix,
        status="VERIFIED",
    )
    assert db.complete_generation_run(run_id)
    return run_id


def test_completed_today_is_reported_separately_from_all_completed_documents():
    db = DatabaseManager(":memory:")
    today = _document(db, "today.pdf")
    old = _document(db, "old.pdf")
    _verified_run(db, today, "today")
    old_run = _verified_run(db, old, "old")
    with db.transaction() as conn:
        conn.execute(
            "UPDATE generation_runs "
            "SET completed_at=datetime('now','-1 day') WHERE id=?",
            (old_run,),
        )

    summary = db.get_dashboard_summary()

    assert summary["COMPLETED"] == 2
    assert summary["COMPLETED_TODAY"] == 1


def test_text_progress_counts_successful_pages_without_hiding_failures():
    db = DatabaseManager(":memory:")
    document_id = _document(db, "partial-text.pdf", pages=3)
    job_id = db.create_background_job(document_id, "OCR", 3)
    db.claim_next_job(["OCR"])
    db.save_analysis(document_id, 1, "one", None, 0, "No match", "NO_MATCH")
    db.save_analysis(document_id, 2, "two", None, 0, "No match", "NO_MATCH")
    db.save_analysis(
        document_id, 3, "", None, None, "Text extraction failed", "FAILED", "bad page"
    )
    db.update_job_progress(job_id, items_completed=2, items_failed=1)
    db.complete_job(job_id)
    db.update_document_workflow(document_id, ocr_status="COMPLETE_WITH_ERRORS")

    row = db.list_dashboard_documents()[0]

    assert row["ocr_completed"] == 2
    assert row["ocr_failed"] == 1


def test_output_progress_only_counts_the_latest_generation_attempt():
    db = DatabaseManager(":memory:")
    document_id = _document(db, "regenerated.pdf")
    _verified_run(db, document_id, "first-attempt")
    _verified_run(db, document_id, "second-attempt")

    row = db.list_dashboard_documents()[0]

    assert row["outputs_verified"] == 1
    assert row["outputs_total"] == 1


def test_queued_work_is_counted_as_processing_not_new_or_ready():
    db = DatabaseManager(":memory:")
    text_document = _document(db, "waiting-for-text.pdf")
    generation_document = _document(db, "waiting-for-output.pdf")
    db.create_background_job(text_document, "OCR", 1)
    db.set_page_status(generation_document, [1], "EXCLUDED")
    db.create_background_job(generation_document, "GENERATE", 0)

    summary = db.get_dashboard_summary()

    assert summary["OCR_RUNNING"] == 1
    assert summary["GENERATION_RUNNING"] == 1
    assert summary["NEW"] == 0
    assert summary["READY_TO_GENERATE"] == 0
    assert db.list_dashboard_documents("OCR_RUNNING")[0]["id"] == text_document
    assert (
        db.list_dashboard_documents("GENERATION_RUNNING")[0]["id"]
        == generation_document
    )
