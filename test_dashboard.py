"""Offscreen tests for the queue-first operational dashboard."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from dashboard import DashboardWidget, STATUS_FILTERS
from database import DatabaseManager
from dialogs import SettingsDialog
from main_window import ClassifierWindow


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class DashboardDatabase:
    def get_dashboard_summary(self):
        return {
            "new": 1, "ocr_running": 2, "needs_review": 3,
            "ready_to_generate": 1, "completed_today": 4, "errors": 1,
        }

    def list_dashboard_documents(self, status_filter="All"):
        rows = [
            {"id": 1, "filepath": "/in/a.pdf", "employee_id": "E1",
             "page_count": 8, "ocr_completed": 8, "review_completed": 8,
             "outputs_verified": 0, "outputs_total": 0,
             "overall_status": "READY_TO_GENERATE"},
            {"id": 2, "filepath": "/in/b.pdf", "employee_id": "E2",
             "page_count": 5, "ocr_completed": 2, "review_completed": 0,
             "outputs_verified": 0, "outputs_total": 0,
             "overall_status": "OCR_RUNNING"},
        ]
        if status_filter.upper() == "ALL":
            return rows
        wanted = status_filter.replace(" ", "_").upper()
        return [row for row in rows if row["overall_status"] == wanted]


class Coordinator:
    def __init__(self):
        self.generated = []
        self.retried = []

    def queue_generation(self, document_id):
        self.generated.append(document_id)

    def retry_failed(self, document_id):
        self.retried.append(document_id)


def test_dashboard_renders_summary_queue_and_filters(app):
    widget = DashboardWidget(DashboardDatabase())
    assert widget.table.rowCount() == 2
    assert "2" in widget.summary_labels["ocr_running"].text()
    assert tuple(widget.status_filter.itemText(index)
                 for index in range(widget.status_filter.count())) == STATUS_FILTERS
    assert widget.table.item(0, 0).text() == "a.pdf"
    assert widget.table.item(0, 3).text() == "8/8"
    widget.status_filter.setCurrentText("OCR running")
    assert widget.table.rowCount() == 1
    assert widget.table.item(0, 0).text() == "b.pdf"


def test_dashboard_opens_review_and_queues_generation(app):
    coordinator = Coordinator()
    widget = DashboardWidget(DashboardDatabase(), coordinator)
    opened = []
    widget.document_open_requested.connect(opened.append)
    widget.table.selectRow(0)
    widget.open_selected()
    widget.generate_selected()
    assert opened == [1]
    assert coordinator.generated == [1]


def test_configuration_persists_inbox_and_automation(app, tmp_path):
    database = DatabaseManager(":memory:")
    database.add_category("Identity", "card", "COMBINE",
                          "{employee_id}_{category}.pdf")
    dialog = SettingsDialog(database)
    dialog.input_directory.setText(str(tmp_path / "inbox"))
    dialog.completed_directory.setText(str(tmp_path / "completed"))
    dialog.error_directory.setText(str(tmp_path / "errors"))
    dialog.auto_ocr.setChecked(True)
    dialog.automation_mode.setCurrentIndex(
        dialog.automation_mode.findData("AUTOMATIC"))
    dialog.minimum_score.setValue(91)
    dialog.minimum_matches.setValue(3)
    dialog.save()
    assert database.get_setting("paths/input") == str(tmp_path / "inbox")
    assert database.get_setting("paths/completed") == str(tmp_path / "completed")
    assert database.get_setting("paths/error") == str(tmp_path / "errors")
    assert database.get_setting(
        "processing/automatic_text_extraction") is True
    assert database.get_setting("classification/automation_mode") == "AUTOMATIC"
    assert database.get_setting("classification/minimum_score") == 91
    assert database.get_setting("classification/minimum_matches") == 3
    database.close()


def test_main_window_starts_on_dashboard_and_preserves_workspace(app, tmp_path):
    window = ClassifierWindow(tmp_path / "dashboard.sqlite3")
    try:
        assert window.stack.currentWidget() is window.dashboard
        assert window.pages is not None
        assert window.categories is not None
        window.stack.setCurrentIndex(1)
        window.show_dashboard()
        assert window.stack.currentWidget() is window.dashboard
    finally:
        window.close()
