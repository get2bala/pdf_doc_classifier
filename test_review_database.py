"""Database contract for the cross-document Review Workspace.

These tests intentionally describe the public API before its implementation.
The UI must not assemble review state with ad-hoc SQL: search, batching, range
grouping, concurrency protection, and bulk mutations belong in the database
service so they can be tested atomically.
"""

import pytest

from database import DatabaseManager


def _document(db, name, page_count):
    return db.add_source_document(
        "/review-fixtures/{}.pdf".format(name), name.upper(), page_count=page_count
    )


def _analysis(db, document_id, page, text, category_id=None, score=0,
              explanation="No configured keywords matched", status=None):
    db.save_analysis(
        document_id,
        page,
        text,
        category_id,
        score,
        explanation,
        status or ("PENDING" if category_id else "NO_MATCH"),
    )


def test_review_schema_provides_fts5_search_index():
    db = DatabaseManager()

    with db.get_connection() as connection:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='page_text_search'"
        ).fetchone()

    assert row is not None
    assert "VIRTUAL TABLE" in row["sql"].upper()
    assert "FTS5" in row["sql"].upper()


def test_review_search_supports_exact_all_and_any_words():
    db = DatabaseManager()
    document = _document(db, "searchable", 3)
    _analysis(db, document, 1, "Permanent Account Number issued by Income Tax")
    _analysis(db, document, 2, "Account details followed by a permanent address")
    _analysis(db, document, 3, "Monthly gross pay and deductions")

    exact = db.query_review_batch(
        view="UNASSIGNED", search='"permanent account number"',
        search_mode="EXACT", limit=50
    )
    all_words = db.query_review_batch(
        view="UNASSIGNED", search="account income", search_mode="ALL", limit=50
    )
    any_word = db.query_review_batch(
        view="UNASSIGNED", search="income gross", search_mode="ANY", limit=50
    )

    assert [item["page_numbers"] for item in exact["items"]] == [[1]]
    assert [item["page_numbers"] for item in all_words["items"]] == [[1]]
    assert [item["page_numbers"] for item in any_word["items"]] == [[1], [3]]


def test_category_filter_groups_only_adjacent_pages_within_each_source():
    db = DatabaseManager()
    pan = db.add_category("PAN Card", "permanent account number,income tax")
    first = _document(db, "employee_a", 5)
    second = _document(db, "employee_b", 2)
    for page in (1, 2, 4):
        _analysis(
            db, first, page, "PAN evidence", pan, 100,
            "Matched: permanent account number, income tax"
        )
    for page in (1, 2):
        _analysis(
            db, second, page, "PAN evidence", pan, 100,
            "Matched: permanent account number, income tax"
        )

    batch = db.query_review_batch(
        view="SUGGESTED", category_id=pan, limit=50
    )

    assert [
        (item["source_document_id"], item["page_numbers"])
        for item in batch["items"]
    ] == [(first, [1, 2]), (first, [4]), (second, [1, 2])]
    assert all(item["suggested_category_id"] == pan for item in batch["items"])


def test_needs_review_view_is_distinct_from_unassigned_suggestions():
    db = DatabaseManager()
    pan = db.add_category("PAN Card", "income tax")
    document = _document(db, "review_states", 2)
    for page in (1, 2):
        _analysis(db, document, page, "income tax", pan, 100,
                  "Matched: income tax")
    db.assign_pages(document, [2], pan, "NEEDS_REVIEW")

    suggestions = db.query_review_batch(view="SUGGESTED")
    needs_review = db.query_review_batch(view="NEEDS_REVIEW")

    assert [item["page_numbers"] for item in suggestions["items"]] == [[1]]
    assert [item["page_numbers"] for item in needs_review["items"]] == [[2]]


def test_review_batches_are_capped_at_50_and_have_stable_deterministic_order():
    db = DatabaseManager()
    category = db.add_category("Tax", "tax")
    # Insert in deliberately non-lexical filename order. Database identity and
    # page order are the stable tie-breakers, independent of query-plan order.
    documents = [_document(db, "doc_{:02d}".format(index), 1)
                 for index in range(55, 0, -1)]
    for document in documents:
        _analysis(db, document, 1, "tax", category, 100, "Matched: tax")

    first = db.query_review_batch(view="SUGGESTED", category_id=category, limit=500)
    repeated = db.query_review_batch(view="SUGGESTED", category_id=category, limit=50)

    assert first["limit"] == 50
    assert first["total"] == 55
    assert first["remaining"] == 55
    assert len(first["items"]) == 50
    assert [item["item_id"] for item in first["items"]] == [
        item["item_id"] for item in repeated["items"]
    ]
    assert first["batch_token"] == repeated["batch_token"]


def test_strong_eligibility_requires_score_match_count_and_no_conflict():
    db = DatabaseManager()
    pan = db.add_category("PAN", "permanent account number,income tax")
    document = _document(db, "strength", 4)
    _analysis(
        db, document, 1, "strong", pan, 100,
        "Matched: permanent account number, income tax"
    )
    _analysis(db, document, 2, "one indicator", pan, 100, "Matched: income tax")
    _analysis(
        db, document, 3, "low score", pan, 60,
        "Matched: permanent account number, income tax"
    )
    _analysis(
        db, document, 4, "tie", pan, 100,
        "Matched: permanent account number, income tax; Conflict: Aadhaar"
    )

    batch = db.query_review_batch(
        view="SUGGESTED", category_id=pan, limit=50,
        minimum_score=80, minimum_matches=2
    )

    by_page = {item["page_numbers"][0]: item for item in batch["items"]}
    assert by_page[1]["strong_eligible"] is True
    assert by_page[2]["strong_eligible"] is False
    assert by_page[3]["strong_eligible"] is False
    assert by_page[4]["strong_eligible"] is False


def test_bulk_approval_is_atomic_and_never_creates_cross_source_groups():
    db = DatabaseManager()
    pan = db.add_category("PAN", "income tax")
    first = _document(db, "bulk_a", 2)
    second = _document(db, "bulk_b", 1)
    for document, pages in ((first, (1, 2)), (second, (1,))):
        for page in pages:
            _analysis(db, document, page, "income tax", pan, 100,
                      "Matched: income tax")
    batch = db.query_review_batch(view="SUGGESTED", category_id=pan, limit=50)

    result = db.apply_review_action(
        batch_token=batch["batch_token"],
        item_ids=[item["item_id"] for item in batch["items"]],
        action="APPROVE_SUGGESTION",
    )

    assert result == {
        "items_changed": 2,
        "pages_changed": 3,
        "documents_changed": 2,
    }
    assert all(row["status"] == "ASSIGNED"
               for row in db.get_page_assignments(first))
    assert all(row["status"] == "ASSIGNED"
               for row in db.get_page_assignments(second))
    with db.get_connection() as connection:
        groups = connection.execute(
            "SELECT source_document_id, COUNT(*) count "
            "FROM document_groups GROUP BY source_document_id ORDER BY source_document_id"
        ).fetchall()
    assert [(row["source_document_id"], row["count"]) for row in groups] == [
        (first, 1), (second, 1)
    ]


def test_assigning_unassigned_results_recalculates_each_source_readiness():
    db = DatabaseManager()
    category = db.add_category("Salary Slip", "gross pay")
    first = _document(db, "ready_a", 2)
    second = _document(db, "ready_b", 2)
    _analysis(db, first, 1, "gross pay")
    _analysis(db, first, 2, "gross pay")
    _analysis(db, second, 1, "gross pay")
    _analysis(db, second, 2, "continuation page without matching terms")
    # The second source retains one unresolved page outside the selected result.
    batch = db.query_review_batch(
        view="UNASSIGNED", search='"gross pay"', search_mode="EXACT", limit=50
    )
    first_item = next(
        item for item in batch["items"] if item["source_document_id"] == first
    )
    second_item = next(
        item for item in batch["items"] if item["source_document_id"] == second
    )

    db.apply_review_action(
        batch["batch_token"], [first_item["item_id"]], "ASSIGN",
        category_id=category
    )
    # Assign the second source's sole search result, leaving page 2 unresolved.
    second_page_batch = db.query_review_batch(
        view="UNASSIGNED", source_document_id=second, search="gross",
        search_mode="ALL", limit=1
    )
    db.apply_review_action(
        second_page_batch["batch_token"],
        [second_page_batch["items"][0]["item_id"]],
        "ASSIGN", category_id=category
    )

    assert db.get_source_document(first)["review_status"] == "READY"
    assert db.get_source_document(second)["review_status"] == "IN_PROGRESS"
    assert second_item["source_document_id"] == second


def test_deferred_items_leave_assignments_untouched_and_exit_default_queue():
    db = DatabaseManager()
    category = db.add_category("Offer", "employment offer")
    document = _document(db, "defer", 1)
    _analysis(db, document, 1, "employment offer", category, 100,
              "Matched: employment offer")
    batch = db.query_review_batch(view="SUGGESTED", category_id=category, limit=50)

    db.apply_review_action(
        batch["batch_token"], [batch["items"][0]["item_id"]], "DEFER"
    )

    assert db.query_review_batch(
        view="SUGGESTED", category_id=category, limit=50
    )["items"] == []
    deferred = db.query_review_batch(
        view="SUGGESTED", category_id=category, include_deferred=True, limit=50
    )
    assert deferred["items"][0]["deferred"] is True
    assert db.get_page_assignments(document)[0]["status"] == "UNCLASSIFIED"


def test_bulk_action_rejects_stale_batch_without_partial_changes():
    db = DatabaseManager()
    category = db.add_category("PAN", "income tax")
    document = _document(db, "concurrent", 2)
    for page in (1, 2):
        _analysis(db, document, page, "income tax", category, 100,
                  "Matched: income tax")
    batch = db.query_review_batch(view="SUGGESTED", category_id=category, limit=50)
    # Simulate a second workspace changing one page after this batch was loaded.
    db.assign_pages(document, [1], category)

    with pytest.raises(RuntimeError, match="(?i)stale|changed|refresh"):
        db.apply_review_action(
            batch["batch_token"],
            [item["item_id"] for item in batch["items"]],
            "APPROVE_SUGGESTION",
        )

    assignments = db.get_page_assignments(document)
    assert assignments[0]["status"] == "ASSIGNED"
    assert assignments[1]["status"] == "UNCLASSIFIED"
