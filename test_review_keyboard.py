"""Keyboard and accessibility contract for the thumbnail review grid.

The review workspace is a gallery, so it should behave like a familiar desktop
selection surface.  These tests are intentionally written before the keyboard
implementation: cards are focusable, Space toggles one card, Shift+Arrow grows
a range, Command/Ctrl+A selects the visible actionable batch, and Escape clears
selection.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from review_workspace import ReviewWorkspace


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _item(item_id, *, selectable=True):
    return {
        "id": item_id,
        "document_id": item_id,
        "source_name": "Document {:02d}.pdf".format(item_id),
        "employee_id": "E-{:02d}".format(item_id),
        "start_page": item_id,
        "end_page": item_id,
        "suggested_category_id": 1,
        "suggested_category": "PAN Card",
        "evidence": "Permanent account number",
        "status": "SUGGESTED",
        # Do not begin selected: each test should prove the keyboard action.
        "strength": "CHECK",
        "eligible": False,
        "selectable": selectable,
    }


class _Database:
    def __init__(self, items):
        self.items = list(items)

    def list_review_categories(self):
        return [{"id": 1, "name": "PAN Card"}]

    def query_review_items(self, filters, limit):
        return {
            "items": self.items[:limit],
            "remaining": len(self.items),
        }


def _workspace(app, items):
    workspace = ReviewWorkspace(
        _Database(items),
        thumbnail_service=None,
        confirm=lambda *_args: True,
    )
    workspace.resize(1100, 700)
    workspace.show()
    app.processEvents()
    return workspace


def _selected_ids(workspace):
    return [
        card.item_id for card in workspace.cards if card.is_selected()
    ]


def test_cards_expose_obvious_keyboard_focus_and_selection_semantics(app):
    workspace = _workspace(app, [_item(1)])
    try:
        card = workspace.cards[0]

        assert card.focusPolicy() == Qt.StrongFocus
        assert "Document 01.pdf" in card.accessibleName()
        assert "Page 1" in card.accessibleName()
        assert card.property("selected") is False

        card.setFocus()
        app.processEvents()
        assert card.hasFocus()
        assert card.property("keyboardFocus") is True

        QTest.keyClick(card, Qt.Key_Space)
        app.processEvents()
        assert card.property("selected") is True
    finally:
        workspace.close()


def test_space_toggles_only_the_focused_card(app):
    workspace = _workspace(app, [_item(1), _item(2), _item(3)])
    try:
        workspace.cards[1].setFocus()
        QTest.keyClick(workspace.cards[1], Qt.Key_Space)
        assert _selected_ids(workspace) == [2]

        QTest.keyClick(workspace.cards[1], Qt.Key_Space)
        assert _selected_ids(workspace) == []
    finally:
        workspace.close()


def test_shift_arrow_extends_a_contiguous_selection_range(app):
    workspace = _workspace(app, [_item(number) for number in range(1, 7)])
    try:
        workspace.cards[1].setFocus()
        QTest.keyClick(workspace.cards[1], Qt.Key_Space)

        QTest.keyClick(
            workspace.cards[1], Qt.Key_Right, Qt.ShiftModifier
        )
        focused = QApplication.focusWidget()
        QTest.keyClick(focused, Qt.Key_Right, Qt.ShiftModifier)
        focused = QApplication.focusWidget()
        QTest.keyClick(focused, Qt.Key_Right, Qt.ShiftModifier)

        assert _selected_ids(workspace) == [2, 3, 4, 5]
        assert QApplication.focusWidget() is workspace.cards[4]
    finally:
        workspace.close()


@pytest.mark.parametrize(
    "modifier", [Qt.ControlModifier, Qt.MetaModifier],
    ids=["control-a", "command-a"],
)
def test_command_or_control_a_selects_visible_actionable_cards(
        app, modifier):
    workspace = _workspace(
        app,
        [_item(1), _item(2, selectable=False), _item(3), _item(4)],
    )
    try:
        workspace.cards[0].setFocus()
        QTest.keyClick(workspace.cards[0], Qt.Key_A, modifier)

        assert _selected_ids(workspace) == [1, 3, 4]
    finally:
        workspace.close()


def test_escape_clears_selection_without_moving_keyboard_focus(app):
    workspace = _workspace(app, [_item(1), _item(2), _item(3)])
    try:
        workspace.cards[0].set_selected(True)
        workspace.cards[1].set_selected(True)
        workspace.cards[1].setFocus()

        QTest.keyClick(workspace.cards[1], Qt.Key_Escape)

        assert _selected_ids(workspace) == []
        assert QApplication.focusWidget() is workspace.cards[1]
    finally:
        workspace.close()
