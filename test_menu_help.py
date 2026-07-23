"""Production contracts for the application menu and built-in guidance.

These tests intentionally describe the public UX rather than implementation
details.  They run offscreen so the same behavior is covered on macOS and
Windows CI.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QApplication, QLabel, QPlainTextEdit, QTextBrowser

import main_window
from main_window import ClassifierWindow


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path):
    application_window = ClassifierWindow(tmp_path / "menu-test.sqlite3")
    application_window.menuBar().setNativeMenuBar(False)
    yield application_window
    application_window.close()


def _menu_titles(window):
    return tuple(
        action.text().replace("&", "")
        for action in window.menuBar().actions()
    )


def _dialog_text(dialog):
    """Collect visible user-facing copy without coupling to one text widget."""
    chunks = [label.text() for label in dialog.findChildren(QLabel)]
    chunks.extend(
        editor.toPlainText() for editor in dialog.findChildren(QPlainTextEdit)
    )
    chunks.extend(
        browser.toPlainText() for browser in dialog.findChildren(QTextBrowser)
    )
    return "\n".join(chunks)


def test_menu_bar_uses_standard_production_information_architecture(window):
    assert _menu_titles(window) == ("File", "View", "Tools", "Help")

    assert window.import_pdf_action.text().replace("&", "") == "Import PDF…"
    assert window.configuration_action.text().replace("&", "") == "Configuration…"
    assert window.database_action.text().replace("&", "") == "View Database…"
    assert window.help_action.text().replace("&", "") == "User Guide"
    assert window.shortcuts_action.text().replace("&", "") == "Keyboard Shortcuts"
    assert window.about_action.text().replace("&", "") == (
        "About PDF Document Classifier"
    )


def test_menu_actions_have_discoverable_platform_appropriate_shortcuts(window):
    assert window.import_pdf_action.shortcut().matches(
        QKeySequence(QKeySequence.StandardKey.Open)
    ) == QKeySequence.SequenceMatch.ExactMatch
    assert window.help_action.shortcut().matches(
        QKeySequence(QKeySequence.StandardKey.HelpContents)
    ) == QKeySequence.SequenceMatch.ExactMatch
    assert window.dashboard_action.shortcut() == QKeySequence("Ctrl+1")
    assert window.review_workspace_action.shortcut() == QKeySequence("Ctrl+2")
    assert window.shortcuts_action.shortcut() == QKeySequence("Ctrl+/")


def test_view_menu_is_the_single_checked_navigation_source(window, app):
    assert window.dashboard_action.isCheckable()
    assert window.review_workspace_action.isCheckable()

    window.review_workspace_action.trigger()
    app.processEvents()
    assert window.stack.currentWidget() is window.review_workspace
    assert window.review_workspace_action.isChecked()
    assert not window.dashboard_action.isChecked()

    window.dashboard_action.trigger()
    app.processEvents()
    assert window.stack.currentWidget() is window.dashboard
    assert window.dashboard_action.isChecked()
    assert not window.review_workspace_action.isChecked()


def test_user_guide_explains_workflow_and_ambiguous_review_actions(app):
    from help_dialogs import UserGuideDialog

    dialog = UserGuideDialog()
    text = _dialog_text(dialog).lower()

    assert "dashboard" in text
    assert "review workspace" in text
    assert "approve" in text and "suggested category" in text
    assert "assign" in text and "different category" in text
    assert "defer" in text and ("later" in text or "skip" in text)
    assert "open source" in text and ("original" in text or "source pdf" in text)
    assert "search" in text and ("extracted text" in text or "document text" in text)
    assert "space" in text
    assert "shift" in text
    assert "command" in text or "ctrl" in text
    assert "delete" in text or "unchanged" in text
    assert "user guide" in dialog.windowTitle().lower()
    dialog.close()


def test_keyboard_shortcuts_help_is_a_focused_quick_reference(app):
    from help_dialogs import KeyboardShortcutsDialog

    dialog = KeyboardShortcutsDialog()
    text = _dialog_text(dialog).lower()

    for expected in (
        "arrow",
        "space",
        "shift",
        "select all",
        "escape",
        "return",
        "dashboard",
        "review workspace",
    ):
        assert expected in text
    assert "keyboard shortcuts" in dialog.windowTitle().lower()
    dialog.close()


def test_about_identifies_version_and_local_offline_privacy(app):
    from help_dialogs import AboutDialog

    dialog = AboutDialog()
    text = _dialog_text(dialog).lower()

    assert "pdf document classifier" in text
    assert "version 1.0.0" in text
    assert "offline" in text
    assert "local" in text or "this computer" in text
    assert "not sent" in text or "never sent" in text or "no document content" in text
    assert "about" in dialog.windowTitle().lower()
    dialog.close()


def test_help_menu_actions_open_their_respective_dialogs(window, monkeypatch):
    opened = []

    monkeypatch.setattr(
        main_window.UserGuideDialog,
        "exec",
        lambda self: opened.append("guide"),
    )
    monkeypatch.setattr(
        main_window.KeyboardShortcutsDialog,
        "exec",
        lambda self: opened.append("shortcuts"),
    )
    monkeypatch.setattr(
        main_window.AboutDialog,
        "exec",
        lambda self: opened.append("about"),
    )

    window.help_action.trigger()
    window.shortcuts_action.trigger()
    window.about_action.trigger()

    assert opened == ["guide", "shortcuts", "about"]
