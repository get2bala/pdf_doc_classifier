import sqlite3

import pytest

from database import DatabaseManager, SCHEMA_VERSION


def test_existing_mvp_database_is_migrated_without_data_loss(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(str(path))
    conn.execute("""CREATE TABLE categories (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
        keywords TEXT NOT NULL DEFAULT '', output_policy TEXT NOT NULL DEFAULT 'COMBINE',
        filename_pattern TEXT NOT NULL DEFAULT '{employee_id}_{category}.pdf')""")
    conn.execute("INSERT INTO categories(name) VALUES ('Legacy')")
    conn.commit()
    conn.close()

    db = DatabaseManager(str(path))
    with db.get_connection() as migrated:
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert migrated.execute("SELECT name FROM categories").fetchone()[0] == "Legacy"
    assert "application_settings" in db.list_database_tables()
    db.close()


def test_newer_schema_is_rejected(tmp_path):
    path = tmp_path / "future.sqlite3"
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA user_version = 999")
    conn.close()
    with pytest.raises(RuntimeError, match="newer"):
        DatabaseManager(str(path))


def test_settings_crud_preserves_common_json_types():
    db = DatabaseManager()
    db.set_setting("output.directory", "/tmp/output")
    db.set_setting("ocr.enabled", True)
    db.set_setting("ocr.languages", ["eng", "spa"])
    assert db.get_setting("output.directory") == "/tmp/output"
    assert db.get_setting("ocr.enabled") is True
    assert db.get_setting("missing", 42) == 42
    assert db.list_settings() == {
        "ocr.enabled": True,
        "ocr.languages": ["eng", "spa"],
        "output.directory": "/tmp/output",
    }
    assert db.delete_setting("ocr.enabled") is True
    assert db.delete_setting("ocr.enabled") is False


def test_category_validation_update_and_unused_delete():
    db = DatabaseManager()
    with pytest.raises(ValueError, match="name"):
        db.add_category("   ")
    with pytest.raises(ValueError, match="policy"):
        db.add_category("ID", output_policy="INVALID")
    category_id = db.add_category("ID", "passport")
    assert db.update_category(category_id, "Identity", "passport, licence", "separate", "id.pdf")
    category = db.list_categories()[0]
    assert category["name"] == "Identity"
    assert category["output_policy"] == "SEPARATE"
    assert db.delete_category(category_id) is True
    assert db.delete_category(category_id) is False


def test_category_in_use_requires_force_and_force_unassigns_pages():
    db = DatabaseManager()
    category_id = db.add_category("Tax")
    document_id = db.add_source_document("/does/not/exist.pdf", "E1", page_count=1)
    db.assign_pages(document_id, [1], category_id)
    db.save_analysis(document_id, 1, "tax", category_id, 0.9, "keyword")

    with pytest.raises(ValueError, match="in use"):
        db.delete_category(category_id)
    assert db.delete_category(category_id, force=True) is True
    assignment = db.get_page_assignments(document_id)[0]
    analysis = db.get_analysis(document_id, 1)
    assert assignment["document_group_id"] is None
    assert assignment["status"] == "UNCLASSIFIED"
    assert analysis["suggested_category_id"] is None
    assert analysis["status"] == "NO_MATCH"


def test_delete_source_document_cascades_database_records_only(tmp_path):
    source = tmp_path / "keep.pdf"
    source.write_bytes(b"pdf")
    db = DatabaseManager()
    category_id = db.add_category("ID")
    document_id = db.add_source_document(str(source), "E1", page_count=1)
    db.assign_pages(document_id, [1], category_id)
    db.save_analysis(document_id, 1, "id", category_id, 1.0, "matched")

    assert db.delete_source_document(document_id) is True
    assert source.exists()
    assert db.get_source_document(document_id) is None
    for table in ("document_groups", "page_assignments", "page_analysis"):
        assert db.query_table(table)["total"] == 0


def test_database_viewer_query_is_paginated_searchable_and_restricted():
    db = DatabaseManager()
    db.add_category("Offer Letter", "offer")
    db.add_category("Tax Form", "tax")
    page = db.query_table("categories", limit=1)
    assert page["columns"] == ["id", "name", "keywords", "output_policy", "filename_pattern"]
    assert page["total"] == 2
    assert len(page["rows"]) == 1
    assert db.query_table("categories", search="offer")["rows"][0]["name"] == "Offer Letter"
    with pytest.raises(ValueError, match="restricted"):
        db.query_table("categories; DROP TABLE categories")
    assert len(db.list_categories()) == 2
