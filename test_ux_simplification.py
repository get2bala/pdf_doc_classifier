"""Contracts for the compact, readable production shell."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QPushButton

from dashboard import DashboardWidget
from database import DatabaseManager
from help_dialogs import UserGuideDialog
from main_window import ClassifierWindow


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class DashboardRows:
    def get_dashboard_summary(self):
        return {
            "TOTAL": 6,
            "NEW": 1,
            "OCR_RUNNING": 1,
            "NEEDS_REVIEW": 1,
            "READY_TO_GENERATE": 1,
            "GENERATION_RUNNING": 1,
            "COMPLETED": 1,
            "ERRORS": 0,
        }

    def list_dashboard_documents(self, status_filter="ALL"):
        statuses = (
            "NEW", "OCR_RUNNING", "NEEDS_REVIEW", "READY_TO_GENERATE",
            "GENERATION_RUNNING", "COMPLETED",
        )
        rows = [
            {
                "id": index,
                "filepath": "/in/{}.pdf".format(status.lower()),
                "employee_id": "E{}".format(index),
                "page_count": 2,
                "ocr_completed": 2,
                "ocr_failed": 0,
                "review_completed": 0,
                "outputs_verified": 0,
                "outputs_total": 0,
                "overall_status": status,
            }
            for index, status in enumerate(statuses, 1)
        ]
        if status_filter == "ALL":
            return rows
        return [row for row in rows if row["overall_status"] == status_filter]


def test_dashboard_has_one_row_of_six_clickable_filter_tiles(app):
    widget = DashboardWidget(DashboardRows())
    assert tuple(widget.summary_buttons) == (
        "all", "new", "needs_review", "ready_to_generate",
        "completed", "errors",
    )
    assert all(
        isinstance(button, QPushButton) and button.isCheckable()
        for button in widget.summary_buttons.values()
    )
    assert {
        widget.summary_layout.getItemPosition(index)[0]
        for index in range(widget.summary_layout.count())
    } == {0}
    assert "Extracting text" not in [
        button.text() for button in widget.summary_buttons.values()]
    assert "Generating" not in [
        button.text() for button in widget.summary_buttons.values()]


def test_clicking_a_dashboard_tile_is_the_only_filter_control(app):
    widget = DashboardWidget(DashboardRows())
    assert not hasattr(widget, "status_filter")
    assert not hasattr(widget, "primary_action_button")
    widget.summary_buttons["needs_review"].click()
    assert widget.table.rowCount() == 1
    assert widget.table.item(0, 6).text() == "Needs Review"
    widget.summary_buttons["all"].click()
    assert widget.table.rowCount() == 6


def test_completed_tile_uses_all_completed_documents_not_today():
    database = DatabaseManager(":memory:")
    first = database.add_source_document("/in/one.pdf", "E1", 1)
    second = database.add_source_document("/in/two.pdf", "E2", 1)
    for document_id in (first, second):
        run = database.create_generation_run(document_id, "/out", 0)
        assert database.complete_generation_run(run)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE generation_runs SET completed_at=datetime('now','-3 day') "
            "WHERE source_document_id=?", (first,))

    summary = database.get_dashboard_summary()
    widget = DashboardWidget(database)

    assert summary["COMPLETED"] == 2
    assert "2" in widget.summary_buttons["completed"].text()


def test_help_has_readable_line_spacing_and_paragraph_separation(app):
    dialog = UserGuideDialog()
    stylesheet = dialog.browser.document().defaultStyleSheet().lower()
    assert "line-height" in stylesheet
    assert "margin-bottom" in stylesheet
    dialog.close()


def test_application_identity_is_consistent_even_when_embedded(app, tmp_path):
    window = ClassifierWindow(tmp_path / "identity.sqlite3")
    try:
        assert QApplication.applicationName() == "PDF Document Classifier"
        assert QApplication.applicationDisplayName() == "PDF Document Classifier"
        assert window.windowTitle() == "PDF Document Classifier"
    finally:
        window.close()
