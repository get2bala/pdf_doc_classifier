import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from pypdf import PdfReader, PdfWriter

from database import DatabaseManager
from main_window import ClassifierWindow, main
from pdf_engine import PdfExporter


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def make_pdf(path, pages=3):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=300)
    with path.open("wb") as handle:
        writer.write(handle)


def test_complete_manual_workflow_persists_reopens_and_exports(tmp_path):
    source, db_path = tmp_path / "bundle.pdf", tmp_path / "classifier.sqlite3"
    make_pdf(source)
    db = DatabaseManager(str(db_path))
    identity = db.add_category("Identity", "passport", "COMBINE",
                               "{employee_id}_{category}.pdf")
    letters = db.add_category("Letters", "offer", "SEPARATE",
                              "{employee_id}_{category}_{instance}.pdf")
    document = db.add_source_document(str(source), "EMP-7", 3)
    db.assign_pages(document, [1], identity)
    db.assign_pages(document, [2], letters)
    db.set_page_status(document, [3], "EXCLUDED")
    db.close()

    reopened = DatabaseManager(str(db_path))
    files = PdfExporter(reopened).export(document, str(tmp_path / "output"))
    assert sorted(len(PdfReader(path).pages) for path in files) == [1, 1]
    assert reopened.get_source_document(document)["status"] == "EXPORTED"


def test_category_editor_apply_is_atomic_when_removal_is_unsafe(tmp_path):
    source = tmp_path / "source.pdf"
    make_pdf(source, 1)
    db = DatabaseManager()
    first = db.add_category("First", "old", "COMBINE", "{employee_id}_{category}.pdf")
    second = db.add_category("Second", "", "COMBINE", "{employee_id}_{category}.pdf")
    document = db.add_source_document(str(source), "E", 1)
    db.assign_pages(document, [1], second)
    with pytest.raises(ValueError, match="in use"):
        db.replace_categories([{
            "id": first, "name": "Changed", "keywords": "new",
            "output_policy": "COMBINE", "filename_pattern": "{employee_id}_{category}.pdf",
        }])
    assert [row["name"] for row in db.list_categories()] == ["First", "Second"]


def test_main_window_exposes_complete_lifecycle_controls(app, tmp_path):
    window = ClassifierWindow(tmp_path / "ui.sqlite3")
    try:
        assert callable(main)
        assert window.reset_button.text() == "Reset selected pages"
        assert window.ocr_all_button.text() == "Extract text from all pages"
        assert window.reject_button.text() == "Reject suggestion"
        assert window.delete_document_action.text().startswith("Remove selected")
        assert window.database_action.text() == "View database…"
    finally:
        window.close()
