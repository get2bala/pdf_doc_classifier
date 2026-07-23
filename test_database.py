import pytest
import sqlite3
from database import DatabaseManager

# --- Fixtures ---
# A fixture automatically runs before each test to provide a clean state.
@pytest.fixture
def db():
    # Use an in-memory database so tests are fast and leave no trace
    return DatabaseManager(":memory:")

@pytest.fixture
def seeded_db(db):
    """Provides a database pre-loaded with a document and category for testing."""
    cat_id = db.add_category("Offer Letter", "offer, contract", "COMBINE", "EMP_Offer.pdf")
    doc_id = db.add_source_document("/scans/EMP001.pdf", "EMP001")
    return db, cat_id, doc_id

# --- Tests ---

def test_schema_creation(db):
    """Ensure all tables are created successfully on initialization."""
    with db.get_connection() as conn:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()
        table_names = [t["name"] for t in tables]
        
        assert "categories" in table_names
        assert "source_documents" in table_names
        assert "document_groups" in table_names
        assert "page_assignments" in table_names

def test_add_source_document(db):
    """Ensure we can add a document and retrieve its ID."""
    doc_id = db.add_source_document("/scans/EMP002.pdf", "EMP002")
    assert doc_id == 1

def test_unique_document_constraint(db):
    """Ensure duplicate filepaths throw a SQLite Integrity Error."""
    db.add_source_document("/scans/EMP001.pdf", "EMP001")
    with pytest.raises(sqlite3.IntegrityError):
        db.add_source_document("/scans/EMP001.pdf", "EMP001")

def test_assign_page_upsert(seeded_db):
    """Ensure assigning a page creates a record, and re-assigning it updates it."""
    db, cat_id, doc_id = seeded_db
    
    group_id = db.create_document_group(doc_id, cat_id)
    
    # 1. Assign page 1 to the group
    db.assign_page(doc_id, page_number=1, document_group_id=group_id, status="ASSIGNED")
    
    assignments = db.get_page_assignments(doc_id)
    assert len(assignments) == 1
    assert assignments[0]["page_number"] == 1
    assert assignments[0]["status"] == "ASSIGNED"

    # 2. Re-assign the same page (Simulate User changing their mind)
    # Status changes to NEEDS_REVIEW
    db.assign_page(doc_id, page_number=1, document_group_id=group_id, status="NEEDS_REVIEW")
    
    assignments = db.get_page_assignments(doc_id)
    assert len(assignments) == 1  # Should still be 1 record, not 2
    assert assignments[0]["status"] == "NEEDS_REVIEW"

def test_foreign_key_constraints(seeded_db):
    """Ensure we cannot assign a page to a document group that doesn't exist."""
    db, _, doc_id = seeded_db
    
    # Try to assign to group ID 999 (which does not exist)
    with pytest.raises(sqlite3.IntegrityError):
        db.assign_page(doc_id, page_number=1, document_group_id=999, status="ASSIGNED")


def test_document_group_cannot_be_used_for_another_document(seeded_db):
    db, cat_id, first_doc = seeded_db
    second_doc = db.add_source_document("/scans/EMP002.pdf", "EMP002")
    group_id = db.create_document_group(first_doc, cat_id)
    with pytest.raises(sqlite3.IntegrityError):
        db.assign_page(second_doc, 1, group_id, "ASSIGNED")


def test_assignment_rejects_page_outside_document(tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"x")
    db = DatabaseManager(":memory:")
    category = db.add_category("ID", "", "COMBINE", "id.pdf")
    document = db.add_source_document(str(source), "E1", page_count=2)
    with pytest.raises(ValueError, match="outside"):
        db.assign_pages(document, [3], category)
