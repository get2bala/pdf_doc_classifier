"""Offscreen UI coverage for configuration and safe data browsing."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QAbstractItemView, QDialog

from database import DatabaseManager
from dialogs import ALLOWED_TABLES, DatabaseViewerDialog, SettingsDialog
from main_window import ClassifierWindow


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def db():
    database = DatabaseManager(":memory:")
    database.add_category("Identity", "identity,card", "COMBINE",
                          "{employee_id}_{category}.pdf")
    yield database
    database.close()


def test_settings_dialog_loads_and_persists_general_and_category_settings(app, db, tmp_path):
    dialog = SettingsDialog(db)
    dialog.input_directory.setText(str(tmp_path))
    dialog.output_directory.setText(str(tmp_path / "out"))
    dialog.auto_extraction.setChecked(True)
    dialog.category_table.item(0, 2).setText("identity,government id")

    dialog.save()

    assert dialog.result() == QDialog.Accepted
    assert db.get_setting("processing/automatic_text_extraction") is True
    assert db.get_setting("paths/input") == str(tmp_path)
    assert db.list_categories()[0]["keywords"] == "identity,government id"


def test_settings_validation_rejects_unsafe_or_incomplete_patterns(app, db):
    dialog = SettingsDialog(db)
    dialog.category_table.item(0, 4).setText("../{employee_id}_{category}.pdf")
    assert "cannot contain" in dialog.validation_error()
    dialog.category_table.item(0, 4).setText("{category}.pdf")
    assert "must end" in dialog.validation_error()


def test_separate_category_requires_instance_placeholder(app, db):
    dialog = SettingsDialog(db)
    dialog.category_table.cellWidget(0, 3).setCurrentText("SEPARATE")
    assert "{instance}" in dialog.validation_error()


def test_database_viewer_is_allowlisted_read_only_and_refreshes(app, db):
    db.add_source_document("/does/not/need/to/exist.pdf", "E-1", 2)
    db.set_setting("ocr/language", "eng")
    dialog = DatabaseViewerDialog(db)

    assert tuple(dialog.table_selector.itemText(i) for i in range(dialog.table_selector.count())) == ALLOWED_TABLES
    assert dialog.table.editTriggers() == QAbstractItemView.NoEditTriggers
    dialog.table_selector.setCurrentText("source_documents")
    dialog.refresh()
    assert dialog.table.rowCount() == 1
    assert "1 row shown" in dialog.status.text()


def test_main_window_exposes_configuration_and_database_actions(app, tmp_path):
    window = ClassifierWindow(tmp_path / "app.sqlite3")
    try:
        assert window.configuration_action.text() == "Configuration…"
        assert window.database_action.text() == "View database…"
    finally:
        window.close()
