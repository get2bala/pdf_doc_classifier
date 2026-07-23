from classifier import AnalysisPipeline, normalize_text, suggest_category
from database import DatabaseManager


def test_normalize_and_deterministic_suggestion():
    categories = [
        {"id": 1, "name": "PAN", "keywords": "permanent account number,income tax"},
        {"id": 2, "name": "Offer", "keywords": "offer of employment,date of joining"},
    ]
    assert normalize_text("  PERMANENT\nAccount\tNumber  ") == "permanent account number"
    category_id, score, reason = suggest_category("INCOME TAX — Permanent Account Number", categories)
    assert category_id == 1
    assert score == 100
    assert "permanent account number" in reason


def test_suggestions_do_not_assign_until_accepted(tmp_path, monkeypatch):
    db = DatabaseManager(":memory:")
    category_id = db.add_category("Offer", "offer of employment", "COMBINE", "{employee_id}_offer.pdf")
    source = tmp_path / "source.pdf"
    source.write_bytes(b"placeholder")
    document_id = db.add_source_document(str(source), "E01", 1)

    class FakeEngine:
        def recognize(self, image):
            return "Offer of Employment"

    monkeypatch.setattr("classifier.render_page", lambda *args, **kwargs: object())
    pipeline = AnalysisPipeline(db, FakeEngine())
    result = pipeline.analyze_page(document_id, 1)
    assert result["suggested_category_id"] == category_id
    assert db.get_page_assignments(document_id)[0]["status"] == "UNCLASSIFIED"

    pipeline.accept(document_id, [1])
    assert db.get_page_assignments(document_id)[0]["status"] == "ASSIGNED"
    assert db.get_analysis(document_id, 1)["status"] == "ACCEPTED"
