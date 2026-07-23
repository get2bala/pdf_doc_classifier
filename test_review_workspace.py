"""Contract tests for the cross-document, fixed-batch review workspace.

These tests intentionally describe the UI before ``review_workspace.py`` is
implemented.  They use an in-memory fake rather than depending on the SQLite
schema, which keeps the interaction contract explicit and fast.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from main_window import ClassifierWindow
from review_workspace import (
    REVIEW_PRESETS,
    ReviewCard,
    ReviewWorkspace,
)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def review_item(item_id, *, category="PAN Card", strength="STRONG",
                status="SUGGESTED", eligible=True, document_id=None):
    """Return lightweight metadata; page images are supplied independently."""
    return {
        "id": item_id,
        "document_id": document_id or item_id,
        "source_name": "EMP{:03d}.pdf".format(item_id),
        "employee_id": "E-{:03d}".format(item_id),
        "start_page": item_id,
        "end_page": item_id + 1,
        "suggested_category_id": 1 if category else None,
        "suggested_category": category,
        "current_category": None,
        "evidence": "permanent account number; income tax department",
        "status": status,
        "strength": strength,
        "eligible": eligible,
    }


class FakeReviewDatabase:
    """Fake implementation of the intended ReviewWorkspace data seam."""

    def __init__(self, items=None):
        self.items = list(items or [])
        self.queries = []
        self.approved = []
        self.assigned = []
        self.deferred = []

    def list_review_categories(self):
        return [
            {"id": 1, "name": "PAN Card"},
            {"id": 2, "name": "Aadhaar Card"},
        ]

    def query_review_items(self, filters, limit):
        assert limit == 50
        self.queries.append(dict(filters))
        rows = list(self.items)
        category_id = filters.get("category_id")
        if category_id == "UNASSIGNED":
            rows = [
                row for row in rows
                if not row["suggested_category"] and not row["current_category"]
            ]
        elif isinstance(category_id, int):
            rows = [
                row for row in rows
                if row["suggested_category_id"] == category_id
            ]
        status = filters.get("status")
        if status and status != "ALL":
            rows = [row for row in rows if row["status"] == status]
        search = filters.get("search", "").casefold()
        if search:
            rows = [row for row in rows if search in row["evidence"].casefold()]
        return {"items": rows[:limit], "remaining": len(rows)}

    def approve_review_items(self, item_ids):
        self.approved.append(list(item_ids))
        self.items = [row for row in self.items if row["id"] not in item_ids]

    def assign_review_items(self, item_ids, category_id):
        self.assigned.append((list(item_ids), category_id))
        self.items = [row for row in self.items if row["id"] not in item_ids]

    def defer_review_items(self, item_ids):
        self.deferred.append(list(item_ids))
        self.items = [row for row in self.items if row["id"] not in item_ids]


class FakeThumbnailService:
    def __init__(self):
        self.requests = []

    def request_thumbnail(self, document_id, page_number, receiver):
        self.requests.append((document_id, page_number, receiver))


class ConfirmationSpy:
    def __init__(self, answer=True):
        self.answer = answer
        self.calls = []

    def __call__(self, action, item_count, source_count):
        self.calls.append((action, item_count, source_count))
        return self.answer


def make_workspace(items, confirmation=None):
    database = FakeReviewDatabase(items)
    thumbnails = FakeThumbnailService()
    workspace = ReviewWorkspace(
        database,
        thumbnail_service=thumbnails,
        confirm=confirmation or ConfirmationSpy(),
    )
    return workspace, database, thumbnails


def test_workspace_has_four_simple_presets_and_unified_filters(app):
    workspace, database, _ = make_workspace([review_item(1)])

    assert REVIEW_PRESETS == (
        "Strong Suggestions",
        "Needs Review",
        "Unassigned",
        "Extraction Failures",
    )
    assert tuple(
        workspace.view_selector.itemData(index)
        for index in range(workspace.view_selector.count())
    ) == ("SUGGESTED", "NEEDS_REVIEW", "UNASSIGNED", "FAILED")
    assert workspace.category_filter.findText("All categories") >= 0
    assert workspace.category_filter.findText("Unassigned") < 0
    assert "document text" in workspace.search_input.placeholderText().lower()
    assert database.queries


def test_query_is_strictly_capped_at_50_cards_and_only_requests_visible_thumbnails(app):
    workspace, _, thumbnails = make_workspace(
        [review_item(number) for number in range(1, 76)]
    )

    assert len(workspace.cards) == 50
    assert len(thumbnails.requests) == 50
    assert "50" in workspace.showing_label.text()
    assert "75" in workspace.remaining_label.text()


def test_card_presents_operational_context_and_toggles_by_click(app):
    item = review_item(7)
    card = ReviewCard(item, FakeThumbnailService())

    displayed = " ".join(
        (
            card.source_label.text(),
            card.employee_label.text(),
            card.page_label.text(),
            card.category_label.text(),
            card.evidence_label.text(),
            card.status_label.text(),
        )
    )
    assert "EMP007.pdf" in displayed
    assert "E-007" in displayed
    assert "7" in displayed and "8" in displayed
    assert "PAN Card" in displayed
    assert "permanent account number" in displayed
    assert "SUGGESTED" in displayed

    before = card.is_selected()
    card.mousePressEvent(None)
    assert card.is_selected() is not before


def test_only_strong_unambiguous_eligible_cards_start_selected(app):
    items = [
        review_item(1),
        review_item(2, strength="WEAK"),
        review_item(3, status="NEEDS_REVIEW"),
        review_item(4, eligible=False),
    ]
    workspace, _, _ = make_workspace(items)

    assert [card.item_id for card in workspace.cards if card.is_selected()] == [1]
    workspace.clear_selection()
    assert not any(card.is_selected() for card in workspace.cards)
    workspace.select_all_strong()
    assert [card.item_id for card in workspace.cards if card.is_selected()] == [1]


def test_approve_uses_confirmation_seam_commits_then_loads_next_batch(app):
    confirmation = ConfirmationSpy()
    items = [review_item(number) for number in range(1, 53)]
    workspace, database, thumbnails = make_workspace(items, confirmation)
    first_ids = [card.item_id for card in workspace.cards]

    workspace.approve_selected()

    assert confirmation.calls == [("Approve", 50, 50)]
    assert database.approved == [first_ids]
    assert [card.item_id for card in workspace.cards] == [51, 52]
    assert len(thumbnails.requests) == 52
    assert "50" in workspace.session_progress_label.text()
    assert "2" in workspace.remaining_label.text()


def test_cancelled_confirmation_does_not_mutate_or_advance(app):
    confirmation = ConfirmationSpy(answer=False)
    workspace, database, _ = make_workspace(
        [review_item(number) for number in range(1, 4)], confirmation
    )

    workspace.approve_selected()

    assert database.approved == []
    assert [card.item_id for card in workspace.cards] == [1, 2, 3]


def test_assign_selected_to_category_and_defer_are_batch_operations(app):
    confirmation = ConfirmationSpy()
    workspace, database, _ = make_workspace(
        [review_item(1, category=None), review_item(2, category=None)],
        confirmation,
    )
    for card in workspace.cards:
        card.set_selected(True)

    workspace.assign_selected(category_id=2)

    assert database.assigned == [([1, 2], 2)]
    assert confirmation.calls[-1] == ("Assign", 2, 2)

    workspace, database, _ = make_workspace(
        [review_item(3), review_item(4)], confirmation
    )
    workspace.cards[0].set_selected(True)
    workspace.cards[1].set_selected(False)
    workspace.defer_selected()
    assert database.deferred == [[3]]


def test_filters_and_presets_refresh_the_same_workspace(app):
    items = [
        review_item(1),
        review_item(2, category=None, status="UNASSIGNED", eligible=False),
    ]
    workspace, database, _ = make_workspace(items)

    workspace.activate_preset("Unassigned")
    assert database.queries[-1]["view"] == "UNASSIGNED"
    workspace.search_input.setText("income tax")
    workspace.apply_filters()
    assert database.queries[-1]["search"] == "income tax"


def test_open_source_signal_preserves_document_and_start_page(app):
    workspace, _, _ = make_workspace([review_item(8, document_id=88)])
    opened = []
    workspace.source_open_requested.connect(
        lambda document_id, page_number: opened.append((document_id, page_number))
    )
    workspace.cards[0].set_selected(True)
    workspace.open_selected_source()
    assert opened == [(88, 8)]


def test_main_window_adds_review_navigation_without_replacing_existing_workspace(
        app, tmp_path):
    window = ClassifierWindow(tmp_path / "review-navigation.sqlite3")
    try:
        assert window.review_workspace is not None
        assert window.review_workspace_action.text().replace("&", "") == (
            "Review Workspace")
        assert window.stack.indexOf(window.dashboard) >= 0
        assert window.stack.indexOf(window.review_workspace) >= 0
        assert window.pages is not None  # existing detailed editor remains intact

        window.review_workspace_action.trigger()
        assert window.stack.currentWidget() is window.review_workspace
        window.review_workspace.source_open_requested.emit(123, 4)
        assert window.stack.currentIndex() == window.stack.indexOf(
            window.workspace_widget
        )
    finally:
        window.close()
