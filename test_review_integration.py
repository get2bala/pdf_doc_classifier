import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

from PIL import Image
from PySide6.QtWidgets import QApplication

from database import DatabaseManager
from main_window import ClassifierWindow
from review_workspace import ReviewWorkspace
from thumbnail_adapter import QtThumbnailAdapter
from thumbnail_service import ThumbnailService


def test_failed_extraction_view_excludes_ordinary_unassigned_pages():
    database = DatabaseManager(":memory:")
    failed_doc = database.add_source_document("/tmp/failed.pdf", "FAILED", 1)
    normal_doc = database.add_source_document("/tmp/normal.pdf", "NORMAL", 1)
    database.save_analysis(
        failed_doc, 1, "", None, None, "Extraction failed", "FAILED", "bad text")
    database.save_analysis(
        normal_doc, 1, "ordinary text", None, 0, "No match", "NO_MATCH")

    result = database.query_review_batch(view="FAILED", limit=50)

    assert len(result["items"]) == 1
    assert result["items"][0]["source_document_id"] == failed_doc


def test_real_workspace_respects_configured_strength_thresholds():
    app = QApplication.instance() or QApplication([])
    database = DatabaseManager(":memory:")
    category = database.add_category("PAN", "alpha,beta")
    document = database.add_source_document("/tmp/source.pdf", "E1", 2)
    database.save_analysis(
        document, 1, "alpha beta", category, 90, "Matched: alpha, beta", "PENDING")
    database.save_analysis(
        document, 2, "alpha", category, 100, "Matched: alpha", "PENDING")
    database.set_setting("classification/minimum_score", 95)
    database.set_setting("classification/minimum_matches", 2)

    workspace = ReviewWorkspace(database)

    assert len(workspace.cards) == 2
    assert not any(card.is_selected() for card in workspace.cards)


def test_qt_thumbnail_adapter_batches_requests_and_delivers_cached_results(tmp_path):
    app = QApplication.instance() or QApplication([])
    source = tmp_path / "source.pdf"
    source.write_bytes(b"identity only")
    database = DatabaseManager(":memory:")
    document = database.add_source_document(str(source), "E1", 2)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE source_documents SET file_sha256='source-hash' WHERE id=?",
            (document,))
    calls = []

    def renderer(path, pages, dimensions):
        calls.append((Path(path), tuple(pages), dimensions))
        return {page: Image.new("RGB", dimensions) for page in pages}

    service = ThumbnailService(tmp_path / "cache", renderer=renderer)
    adapter = QtThumbnailAdapter(database, service, synchronous=True)
    delivered = []
    adapter.request_thumbnail(document, 1, delivered.append)
    adapter.request_thumbnail(document, 2, delivered.append)
    adapter.flush()

    assert calls == [(source.resolve(), (1, 2), (200, 280))]
    assert len(delivered) == 2
    assert all(result.path.is_file() for result in delivered)


def test_main_window_wires_production_thumbnail_adapter(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = ClassifierWindow(tmp_path / "app.sqlite3")
    try:
        assert isinstance(
            window.review_workspace.thumbnail_service, QtThumbnailAdapter)
    finally:
        window.close()
