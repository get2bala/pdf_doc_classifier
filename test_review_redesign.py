"""Interaction contracts for the simplified review surface."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from test_review_workspace import make_workspace, review_item


def test_review_uses_one_view_selector_without_duplicate_filter_buttons():
    app = QApplication.instance() or QApplication([])
    workspace, _, _ = make_workspace([review_item(1)])
    workspace.show()
    app.processEvents()

    assert tuple(
        workspace.view_selector.itemData(index)
        for index in range(workspace.view_selector.count())
    ) == ("SUGGESTED", "NEEDS_REVIEW", "UNASSIGNED", "FAILED")
    assert not hasattr(workspace, "search_button")
    assert not hasattr(workspace, "select_strong_button")
    assert not hasattr(workspace, "clear_button")
    assert not hasattr(workspace, "open_source_button")
    assert workspace.search_input.isClearButtonEnabled()
    workspace.close()


def test_action_bar_is_contextual_and_uses_a_compact_more_menu():
    app = QApplication.instance() or QApplication([])
    workspace, _, _ = make_workspace([review_item(1)])
    workspace.show()
    app.processEvents()

    assert workspace.approve_button.isVisible()
    assert workspace.approve_button.text().startswith("Confirm")
    assert not workspace.change_category_button.isVisible()
    assert workspace.change_category_action.isVisible()
    assert not hasattr(workspace, "assign_category")
    assert workspace.more_button.isVisible()
    assert "selected" in workspace.selection_count_label.text().lower()

    workspace.activate_preset("Extraction Failures")
    app.processEvents()
    assert not workspace.approve_button.isVisible()
    assert not workspace.change_category_button.isVisible()
    assert not workspace.change_category_action.isVisible()
    assert workspace.more_button.isVisible()
    workspace.close()


def test_unassigned_has_one_unambiguous_assignment_action():
    app = QApplication.instance() or QApplication([])
    workspace, _, _ = make_workspace([
        review_item(1, category=None, status="UNASSIGNED", eligible=False)])
    workspace.activate_preset("Unassigned")
    workspace.show()
    app.processEvents()

    assert not workspace.approve_button.isVisible()
    assert workspace.change_category_button.isVisible()
    assert workspace.change_category_button.text() == "Assign Category…"
    assert "Defer" not in [
        action.text() for action in workspace.more_button.menu().actions()]
    assert "Open in Document" in [
        action.text() for action in workspace.more_button.menu().actions()]
    workspace.close()


def test_progress_is_presented_as_one_quiet_sentence():
    app = QApplication.instance() or QApplication([])
    workspace, _, _ = make_workspace(
        [review_item(number) for number in range(1, 4)])
    assert "Showing 3 of 3" in workspace.progress_label.text()
    assert "resolved this session" in workspace.progress_label.text()


def test_gallery_reflows_without_requerying_or_losing_selection():
    app = QApplication.instance() or QApplication([])
    workspace, database, thumbnails = make_workspace(
        [review_item(number) for number in range(1, 9)])
    workspace.resize(1200, 700)
    workspace.show()
    app.processEvents()
    wide_columns = workspace._columns
    queries = len(database.queries)
    requests = len(thumbnails.requests)
    workspace.cards[2].set_selected(True)

    workspace.resize(620, 700)
    app.processEvents()

    assert workspace._columns < wide_columns
    assert len(database.queries) == queries
    assert len(thumbnails.requests) == requests
    assert workspace.cards[2].is_selected()
    workspace.close()


def test_changed_filter_does_not_keep_a_blue_focus_state():
    app = QApplication.instance() or QApplication([])
    workspace, _, _ = make_workspace([review_item(1)])
    workspace.show()
    workspace.view_selector.setFocus()
    workspace.view_selector.setCurrentIndex(1)
    app.processEvents()

    assert not workspace.view_selector.hasFocus()
    workspace.close()
