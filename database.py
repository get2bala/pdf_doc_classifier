"""SQLite persistence for the document classifier.

The manager deliberately owns one connection.  This makes ``:memory:`` databases
behave correctly and serializes access from the UI and its background worker.
"""

import json
import hashlib
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    keywords TEXT NOT NULL DEFAULT '',
    output_policy TEXT NOT NULL DEFAULT 'COMBINE'
        CHECK(output_policy IN ('COMBINE', 'SEPARATE')),
    filename_pattern TEXT NOT NULL DEFAULT '{employee_id}_{category}.pdf'
);
CREATE TABLE IF NOT EXISTS source_documents (
    id INTEGER PRIMARY KEY,
    filepath TEXT NOT NULL UNIQUE,
    employee_id TEXT NOT NULL,
    page_count INTEGER NOT NULL DEFAULT 0 CHECK(page_count >= 0),
    file_size INTEGER NOT NULL DEFAULT 0,
    file_mtime_ns INTEGER NOT NULL DEFAULT 0,
    file_sha256 TEXT,
    discovery_status TEXT NOT NULL DEFAULT 'IMPORTED',
    ingestion_status TEXT NOT NULL DEFAULT 'IMPORTED',
    ocr_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
    review_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
    generation_status TEXT NOT NULL DEFAULT 'NOT_READY',
    status TEXT NOT NULL DEFAULT 'IN_PROGRESS'
        CHECK(status IN ('IN_PROGRESS', 'READY', 'EXPORTED', 'ERROR')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS document_groups (
    id INTEGER PRIMARY KEY,
    source_document_id INTEGER NOT NULL REFERENCES source_documents(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    instance_number INTEGER NOT NULL DEFAULT 1 CHECK(instance_number > 0),
    UNIQUE(source_document_id, category_id, instance_number)
);
CREATE TABLE IF NOT EXISTS page_assignments (
    source_document_id INTEGER NOT NULL REFERENCES source_documents(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL CHECK(page_number > 0),
    document_group_id INTEGER REFERENCES document_groups(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'UNCLASSIFIED'
        CHECK(status IN ('UNCLASSIFIED', 'ASSIGNED', 'NEEDS_REVIEW', 'EXCLUDED')),
    PRIMARY KEY (source_document_id, page_number)
);
CREATE TABLE IF NOT EXISTS page_analysis (
    source_document_id INTEGER NOT NULL REFERENCES source_documents(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL,
    ocr_text TEXT NOT NULL DEFAULT '',
    suggested_category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    score REAL,
    explanation TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK(status IN ('PENDING', 'ACCEPTED', 'REJECTED', 'NO_MATCH', 'FAILED')),
    error_message TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(source_document_id, page_number)
);
CREATE TABLE IF NOT EXISTS review_deferred_pages (
    source_document_id INTEGER NOT NULL REFERENCES source_documents(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL,
    deferred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(source_document_id, page_number),
    FOREIGN KEY(source_document_id, page_number)
        REFERENCES page_analysis(source_document_id, page_number) ON DELETE CASCADE
);
CREATE VIRTUAL TABLE IF NOT EXISTS page_text_search USING fts5(
    source_document_id UNINDEXED,
    page_number UNINDEXED,
    ocr_text,
    tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS page_analysis_fts_insert
AFTER INSERT ON page_analysis BEGIN
    INSERT INTO page_text_search(source_document_id,page_number,ocr_text)
    VALUES(new.source_document_id,new.page_number,new.ocr_text);
END;
CREATE TRIGGER IF NOT EXISTS page_analysis_fts_update
AFTER UPDATE OF ocr_text ON page_analysis BEGIN
    DELETE FROM page_text_search
    WHERE source_document_id=old.source_document_id AND page_number=old.page_number;
    INSERT INTO page_text_search(source_document_id,page_number,ocr_text)
    VALUES(new.source_document_id,new.page_number,new.ocr_text);
END;
CREATE TRIGGER IF NOT EXISTS page_analysis_fts_delete
AFTER DELETE ON page_analysis BEGIN
    DELETE FROM page_text_search
    WHERE source_document_id=old.source_document_id AND page_number=old.page_number;
END;
CREATE TABLE IF NOT EXISTS application_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS background_jobs (
    id INTEGER PRIMARY KEY,
    source_document_id INTEGER NOT NULL REFERENCES source_documents(id) ON DELETE CASCADE,
    job_type TEXT NOT NULL CHECK(job_type IN ('INGEST','OCR','GENERATE')),
    status TEXT NOT NULL DEFAULT 'QUEUED'
        CHECK(status IN ('QUEUED','RUNNING','COMPLETE','FAILED','CANCELLED')),
    items_total INTEGER NOT NULL DEFAULT 0 CHECK(items_total >= 0),
    items_completed INTEGER NOT NULL DEFAULT 0 CHECK(items_completed >= 0),
    items_failed INTEGER NOT NULL DEFAULT 0 CHECK(items_failed >= 0),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts > 0),
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    completed_at TEXT,
    heartbeat_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_background_jobs_claim
    ON background_jobs(status, job_type, created_at, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_background_jobs_one_active
    ON background_jobs(source_document_id, job_type)
    WHERE status IN ('QUEUED','RUNNING');
CREATE TABLE IF NOT EXISTS generation_runs (
    id INTEGER PRIMARY KEY,
    source_document_id INTEGER NOT NULL REFERENCES source_documents(id) ON DELETE CASCADE,
    job_id INTEGER REFERENCES background_jobs(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'RUNNING'
        CHECK(status IN ('RUNNING','COMPLETE','FAILED')),
    output_directory TEXT NOT NULL,
    expected_outputs INTEGER NOT NULL DEFAULT 0,
    verified_outputs INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS output_files (
    id INTEGER PRIMARY KEY,
    generation_run_id INTEGER NOT NULL REFERENCES generation_runs(id) ON DELETE CASCADE,
    source_document_id INTEGER NOT NULL REFERENCES source_documents(id) ON DELETE CASCADE,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    output_path TEXT NOT NULL,
    expected_page_count INTEGER NOT NULL CHECK(expected_page_count >= 0),
    actual_page_count INTEGER,
    file_size INTEGER,
    sha256 TEXT,
    status TEXT NOT NULL DEFAULT 'PLANNED'
        CHECK(status IN ('PLANNED','WRITTEN','VERIFIED','FAILED')),
    error_message TEXT,
    generated_at TEXT,
    verified_at TEXT,
    UNIQUE(generation_run_id, output_path)
);
"""

SCHEMA_VERSION = 4
VIEWABLE_TABLES = (
    "application_settings", "categories", "source_documents", "document_groups",
    "page_assignments", "page_analysis", "background_jobs", "generation_runs",
    "output_files",
)
OUTPUT_POLICIES = {"COMBINE", "SEPARATE"}


class DatabaseManager:
    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        if db_path != ":memory:":
            Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._review_batches = {}
        self._conn.execute("PRAGMA foreign_keys = ON")
        if db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def _migrate(self):
        """Apply idempotent schema migrations and record the resulting version.

        ``SCHEMA`` remains intentionally idempotent so databases created by the
        pre-migration MVP are upgraded without being rebuilt or losing data.
        """
        with self._lock:
            version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    "Database schema is newer than this application "
                    "(database {}, supported {})".format(version, SCHEMA_VERSION)
                )
            self._conn.executescript(SCHEMA)
            # SQLite's CREATE TABLE IF NOT EXISTS does not add columns to an
            # existing table, so keep this upgrade deliberately additive.
            document_columns = {
                row["name"] for row in self._conn.execute(
                    "PRAGMA table_info(source_documents)"
                )
            }
            additions = (
                ("file_sha256", "TEXT"),
                ("discovery_status", "TEXT NOT NULL DEFAULT 'IMPORTED'"),
                ("ingestion_status", "TEXT NOT NULL DEFAULT 'IMPORTED'"),
                ("ocr_status", "TEXT NOT NULL DEFAULT 'NOT_STARTED'"),
                ("review_status", "TEXT NOT NULL DEFAULT 'NOT_STARTED'"),
                ("generation_status", "TEXT NOT NULL DEFAULT 'NOT_READY'"),
            )
            for name, declaration in additions:
                if name not in document_columns:
                    self._conn.execute(
                        "ALTER TABLE source_documents ADD COLUMN {} {}".format(
                            name, declaration
                        )
                    )
            if version < 4:
                # Triggers cover all future writes. Rebuilding once during the
                # migration makes text saved by older releases searchable too.
                self._conn.execute("DELETE FROM page_text_search")
                self._conn.execute(
                    """INSERT INTO page_text_search
                       (source_document_id,page_number,ocr_text)
                       SELECT source_document_id,page_number,ocr_text
                       FROM page_analysis"""
                )
            self._conn.execute("PRAGMA user_version = {}".format(SCHEMA_VERSION))
            self._conn.commit()

    @contextmanager
    def get_connection(self):
        """Compatibility API; the connection remains owned by this manager."""
        with self._lock:
            yield self._conn

    @contextmanager
    def transaction(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self):
        with self._lock:
            self._conn.close()

    def add_category(self, name: str, keywords: str = "", output_policy: str = "COMBINE",
                     filename_pattern: str = "{employee_id}_{category}.pdf") -> int:
        name, keywords, output_policy, filename_pattern = self._validate_category(
            name, keywords, output_policy, filename_pattern
        )
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO categories(name, keywords, output_policy, filename_pattern) VALUES(?,?,?,?)",
                (name, keywords, output_policy, filename_pattern),
            )
            return int(cur.lastrowid)

    @staticmethod
    def _validate_category(name: str, keywords: str, output_policy: str,
                           filename_pattern: str):
        name = name.strip()
        keywords = keywords.strip()
        output_policy = output_policy.strip().upper()
        filename_pattern = filename_pattern.strip()
        if not name:
            raise ValueError("Category name is required")
        if output_policy not in OUTPUT_POLICIES:
            raise ValueError("Output policy must be COMBINE or SEPARATE")
        if not filename_pattern:
            raise ValueError("Filename pattern is required")
        return name, keywords, output_policy, filename_pattern

    def update_category(self, category_id: int, name: str, keywords: str = "",
                        output_policy: str = "COMBINE",
                        filename_pattern: str = "{employee_id}_{category}.pdf") -> bool:
        values = self._validate_category(name, keywords, output_policy, filename_pattern)
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE categories SET name=?,keywords=?,output_policy=?,filename_pattern=? WHERE id=?",
                values + (category_id,),
            )
            return cur.rowcount == 1

    def delete_category(self, category_id: int, force: bool = False) -> bool:
        """Delete an unused category, or safely unassign its data when forced."""
        with self.transaction() as conn:
            exists = conn.execute("SELECT 1 FROM categories WHERE id=?", (category_id,)).fetchone()
            if not exists:
                return False
            used = conn.execute(
                "SELECT COUNT(*) FROM document_groups WHERE category_id=?", (category_id,)
            ).fetchone()[0]
            suggestions = conn.execute(
                "SELECT COUNT(*) FROM page_analysis WHERE suggested_category_id=?", (category_id,)
            ).fetchone()[0]
            if (used or suggestions) and not force:
                raise ValueError("Category is in use; remove assignments or confirm forced deletion")
            if force:
                conn.execute(
                    "UPDATE page_assignments SET document_group_id=NULL,status='UNCLASSIFIED' "
                    "WHERE document_group_id IN (SELECT id FROM document_groups WHERE category_id=?)",
                    (category_id,),
                )
                conn.execute("DELETE FROM document_groups WHERE category_id=?", (category_id,))
                conn.execute(
                    "UPDATE page_analysis SET suggested_category_id=NULL,status='NO_MATCH' "
                    "WHERE suggested_category_id=?", (category_id,),
                )
            conn.execute("DELETE FROM categories WHERE id=?", (category_id,))
            return True

    def replace_categories(self, categories: Iterable[Dict]):
        """Atomically apply the complete category editor contents.

        Deleting a category that has assignments is intentionally rejected so a
        configuration edit can never silently unclassify existing work.
        """
        rows = []
        for category in categories:
            values = self._validate_category(
                category["name"], category.get("keywords", ""),
                category.get("output_policy", "COMBINE"), category["filename_pattern"])
            rows.append((category.get("id"),) + values)
        if not rows:
            raise ValueError("Configure at least one category")
        identifiers = [row[0] for row in rows if row[0] is not None]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Duplicate category identifier")
        with self.transaction() as conn:
            existing = {int(row["id"]) for row in conn.execute("SELECT id FROM categories")}
            if any(identifier not in existing for identifier in identifiers):
                raise ValueError("Category no longer exists; reopen configuration")
            removed = existing - set(identifiers)
            for identifier in removed:
                used = conn.execute(
                    "SELECT 1 FROM document_groups WHERE category_id=? LIMIT 1", (identifier,)
                ).fetchone()
                suggested = conn.execute(
                    "SELECT 1 FROM page_analysis WHERE suggested_category_id=? LIMIT 1", (identifier,)
                ).fetchone()
                if used or suggested:
                    raise ValueError("Category is in use and cannot be removed")
            for identifier, name, keywords, policy, pattern in rows:
                if identifier is None:
                    conn.execute(
                        "INSERT INTO categories(name,keywords,output_policy,filename_pattern) VALUES(?,?,?,?)",
                        (name, keywords, policy, pattern))
                else:
                    conn.execute(
                        "UPDATE categories SET name=?,keywords=?,output_policy=?,filename_pattern=? WHERE id=?",
                        (name, keywords, policy, pattern, identifier))
            if removed:
                conn.executemany("DELETE FROM categories WHERE id=?", ((value,) for value in removed))

    def list_categories(self) -> List[Dict]:
        with self.get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM categories ORDER BY name")]

    def add_source_document(self, filepath: str, employee_id: str, page_count: int = 0) -> int:
        path = Path(filepath).expanduser().resolve()
        stat = path.stat() if path.exists() else None
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO source_documents(filepath, employee_id, page_count, file_size, file_mtime_ns) "
                "VALUES(?,?,?,?,?)",
                (str(path), employee_id.strip(), page_count,
                 stat.st_size if stat else 0, stat.st_mtime_ns if stat else 0),
            )
            doc_id = int(cur.lastrowid)
            conn.executemany(
                "INSERT INTO page_assignments(source_document_id,page_number) VALUES(?,?)",
                ((doc_id, page) for page in range(1, page_count + 1)),
            )
            return doc_id

    def get_source_document(self, document_id: int) -> Optional[Dict]:
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM source_documents WHERE id=?", (document_id,)).fetchone()
            return dict(row) if row else None

    def list_source_documents(self) -> List[Dict]:
        with self.get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM source_documents ORDER BY id DESC")]

    def find_document_by_sha256(self, file_sha256: str) -> Optional[Dict]:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM source_documents WHERE file_sha256=? ORDER BY id DESC LIMIT 1",
                (file_sha256,),
            ).fetchone()
            return dict(row) if row else None

    def register_discovered_document(self, filepath: str, employee_id: str,
                                     page_count: int, file_sha256: str) -> int:
        """Idempotently register a stable file and initialize all page rows."""
        if not file_sha256 or not file_sha256.strip():
            raise ValueError("A source hash is required")
        path = Path(filepath).expanduser().resolve()
        stat = path.stat() if path.exists() else None
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM source_documents WHERE file_sha256=? ORDER BY id DESC LIMIT 1",
                (file_sha256.strip(),),
            ).fetchone()
            if existing:
                return int(existing["id"])
            cur = conn.execute(
                """INSERT INTO source_documents
                   (filepath,employee_id,page_count,file_size,file_mtime_ns,file_sha256,
                    discovery_status,ingestion_status,ocr_status,review_status,generation_status)
                   VALUES(?,?,?,?,?,?,'IMPORTED','IMPORTED','QUEUED','NOT_STARTED','NOT_READY')""",
                (str(path), employee_id.strip(), page_count,
                 stat.st_size if stat else 0, stat.st_mtime_ns if stat else 0,
                 file_sha256.strip()),
            )
            document_id = int(cur.lastrowid)
            conn.executemany(
                "INSERT INTO page_assignments(source_document_id,page_number) VALUES(?,?)",
                ((document_id, page) for page in range(1, page_count + 1)),
            )
            return document_id

    def update_document_workflow(self, document_id: int, **statuses: str) -> bool:
        allowed = {
            "discovery_status", "ingestion_status", "ocr_status",
            "review_status", "generation_status",
        }
        if not statuses or set(statuses) - allowed:
            raise ValueError("Unknown or missing workflow status")
        assignments = ", ".join("{}=?".format(key) for key in statuses)
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE source_documents SET {} WHERE id=?".format(assignments),
                list(statuses.values()) + [document_id],
            )
            return cur.rowcount == 1

    def delete_source_document(self, document_id: int) -> bool:
        """Delete a document and its dependent DB records, never its source PDF."""
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM source_documents WHERE id=?", (document_id,))
            return cur.rowcount == 1

    def set_setting(self, key: str, value: Any):
        key = key.strip()
        if not key:
            raise ValueError("Setting key is required")
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO application_settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",
                (key, encoded),
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.get_connection() as conn:
            row = conn.execute("SELECT value FROM application_settings WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return row["value"]

    def list_settings(self) -> Dict[str, Any]:
        with self.get_connection() as conn:
            rows = conn.execute("SELECT key,value FROM application_settings ORDER BY key").fetchall()
        result = {}
        for row in rows:
            try:
                result[row["key"]] = json.loads(row["value"])
            except (TypeError, ValueError):
                result[row["key"]] = row["value"]
        return result

    def delete_setting(self, key: str) -> bool:
        with self.transaction() as conn:
            return conn.execute("DELETE FROM application_settings WHERE key=?", (key,)).rowcount == 1

    def list_database_tables(self) -> List[str]:
        """Return the application tables allowed in the read-only UI viewer."""
        with self.get_connection() as conn:
            present = {row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )}
        return [name for name in VIEWABLE_TABLES if name in present]

    def get_table_columns(self, table_name: str) -> List[Dict]:
        self._require_viewable_table(table_name)
        with self.get_connection() as conn:
            return [dict(row) for row in conn.execute("PRAGMA table_info({})".format(table_name))]

    def query_table(self, table_name: str, limit: int = 200, offset: int = 0,
                    search: str = "") -> Dict[str, Any]:
        """Return one safe, paginated page for the generic database viewer."""
        self._require_viewable_table(table_name)
        limit = max(1, min(int(limit), 1000))
        offset = max(0, int(offset))
        columns = [column["name"] for column in self.get_table_columns(table_name)]
        where, params = "", []
        if search:
            where = " WHERE " + " OR ".join(
                "CAST({} AS TEXT) LIKE ?".format(column) for column in columns
            )
            params = ["%{}%".format(search)] * len(columns)
        with self.get_connection() as conn:
            total = int(conn.execute(
                "SELECT COUNT(*) FROM {}{}".format(table_name, where), params
            ).fetchone()[0])
            rows = [dict(row) for row in conn.execute(
                "SELECT * FROM {}{} ORDER BY rowid DESC LIMIT ? OFFSET ?".format(table_name, where),
                params + [limit, offset],
            )]
        return {"table": table_name, "columns": columns, "rows": rows,
                "total": total, "limit": limit, "offset": offset}

    @staticmethod
    def _require_viewable_table(table_name: str):
        if table_name not in VIEWABLE_TABLES:
            raise ValueError("Unknown or restricted database table")

    def create_document_group(self, source_document_id: int, category_id: int,
                              instance_number: int = 1) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO document_groups(source_document_id,category_id,instance_number) VALUES(?,?,?)",
                (source_document_id, category_id, instance_number),
            )
            return int(cur.lastrowid)

    def get_or_create_group(self, source_document_id: int, category_id: int,
                            instance_number: int = 1) -> int:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM document_groups WHERE source_document_id=? AND category_id=? AND instance_number=?",
                (source_document_id, category_id, instance_number),
            ).fetchone()
            if row:
                return int(row["id"])
            return int(conn.execute(
                "INSERT INTO document_groups(source_document_id,category_id,instance_number) VALUES(?,?,?)",
                (source_document_id, category_id, instance_number),
            ).lastrowid)

    def assign_pages(self, source_document_id: int, page_numbers: Iterable[int],
                     category_id: int, status: str = "ASSIGNED", instance_number: int = 1):
        pages = sorted(set(page_numbers))
        if not pages:
            return
        document = self.get_source_document(source_document_id)
        if not document:
            raise ValueError("Source document not found")
        if any(page < 1 or page > document["page_count"] for page in pages):
            raise ValueError("Page number is outside the source document")
        group_id = self.get_or_create_group(source_document_id, category_id, instance_number)
        with self.transaction() as conn:
            group = conn.execute("SELECT source_document_id FROM document_groups WHERE id=?", (group_id,)).fetchone()
            if not group or group["source_document_id"] != source_document_id:
                raise ValueError("Document group belongs to another source document")
            conn.executemany(
                "INSERT INTO page_assignments(source_document_id,page_number,document_group_id,status) VALUES(?,?,?,?) "
                "ON CONFLICT(source_document_id,page_number) DO UPDATE SET document_group_id=excluded.document_group_id,status=excluded.status",
                ((source_document_id, p, group_id, status) for p in pages),
            )
            self._refresh_review_status(conn, source_document_id)

    def assign_page(self, source_document_id: int, page_number: int,
                    document_group_id: int, status: str):
        with self.transaction() as conn:
            document = conn.execute("SELECT page_count FROM source_documents WHERE id=?", (source_document_id,)).fetchone()
            if document and document["page_count"] and not 1 <= page_number <= document["page_count"]:
                raise ValueError("Page number is outside the source document")
            group = conn.execute("SELECT source_document_id FROM document_groups WHERE id=?", (document_group_id,)).fetchone()
            if group and group["source_document_id"] != source_document_id:
                raise sqlite3.IntegrityError("document group belongs to another document")
            conn.execute(
                "INSERT INTO page_assignments(source_document_id,page_number,document_group_id,status) VALUES(?,?,?,?) "
                "ON CONFLICT(source_document_id,page_number) DO UPDATE SET document_group_id=excluded.document_group_id,status=excluded.status",
                (source_document_id, page_number, document_group_id, status),
            )
            self._refresh_review_status(conn, source_document_id)

    def set_page_status(self, source_document_id: int, page_numbers: Iterable[int], status: str):
        with self.transaction() as conn:
            conn.executemany(
                "UPDATE page_assignments SET document_group_id=NULL,status=? WHERE source_document_id=? AND page_number=?",
                ((status, source_document_id, p) for p in set(page_numbers)),
            )
            self._refresh_review_status(conn, source_document_id)

    @staticmethod
    def _refresh_review_status(conn, source_document_id: int):
        counts = conn.execute(
            """SELECT
                 SUM(CASE WHEN status IN ('UNCLASSIFIED','NEEDS_REVIEW') THEN 1 ELSE 0 END) unresolved,
                 COUNT(*) total
               FROM page_assignments WHERE source_document_id=?""",
            (source_document_id,),
        ).fetchone()
        if not counts or not counts["total"]:
            review_status = "NOT_STARTED"
        elif not counts["unresolved"]:
            review_status = "READY"
        else:
            review_status = "IN_PROGRESS"
        conn.execute(
            "UPDATE source_documents SET review_status=? WHERE id=?",
            (review_status, source_document_id),
        )

    def reset_pages(self, source_document_id: int, page_numbers: Iterable[int]):
        self.set_page_status(source_document_id, page_numbers, "UNCLASSIFIED")

    def get_page_assignments(self, source_document_id: int) -> List[Dict]:
        sql = """SELECT pa.*, c.id category_id, c.name category_name, dg.instance_number
                 FROM page_assignments pa
                 LEFT JOIN document_groups dg ON dg.id=pa.document_group_id
                 LEFT JOIN categories c ON c.id=dg.category_id
                 WHERE pa.source_document_id=? ORDER BY pa.page_number"""
        with self.get_connection() as conn:
            return [dict(r) for r in conn.execute(sql, (source_document_id,))]

    def save_analysis(self, source_document_id: int, page_number: int, ocr_text: str,
                      category_id: Optional[int], score: Optional[float], explanation: str,
                      status: str = "PENDING", error_message: Optional[str] = None):
        with self.transaction() as conn:
            conn.execute("""INSERT INTO page_analysis
                (source_document_id,page_number,ocr_text,suggested_category_id,score,explanation,status,error_message)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source_document_id,page_number) DO UPDATE SET
                ocr_text=excluded.ocr_text,suggested_category_id=excluded.suggested_category_id,
                score=excluded.score,explanation=excluded.explanation,status=excluded.status,
                error_message=excluded.error_message,updated_at=CURRENT_TIMESTAMP""",
                (source_document_id, page_number, ocr_text, category_id, score, explanation, status, error_message))

    def get_analysis(self, source_document_id: int, page_number: int) -> Optional[Dict]:
        with self.get_connection() as conn:
            row = conn.execute("""SELECT pa.*, c.name suggested_category
                FROM page_analysis pa LEFT JOIN categories c ON c.id=pa.suggested_category_id
                WHERE source_document_id=? AND page_number=?""", (source_document_id, page_number)).fetchone()
            return dict(row) if row else None

    def mark_analysis(self, source_document_id: int, page_numbers: Iterable[int], status: str):
        with self.transaction() as conn:
            conn.executemany("UPDATE page_analysis SET status=? WHERE source_document_id=? AND page_number=?",
                             ((status, source_document_id, p) for p in set(page_numbers)))

    @staticmethod
    def _review_search_expression(search: str, mode: str) -> str:
        """Translate the deliberately small search UI into a safe FTS query."""
        mode = (mode or "ALL").strip().upper()
        if mode not in {"EXACT", "ALL", "ANY"}:
            raise ValueError("Search mode must be EXACT, ALL, or ANY")
        value = (search or "").strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        words = [word for word in value.replace('"', " ").split() if word]
        if not words:
            return ""
        quoted = ['"{}"'.format(word.replace('"', '""')) for word in words]
        if mode == "EXACT":
            return '"{}"'.format(" ".join(words).replace('"', '""'))
        return (" AND " if mode == "ALL" else " OR ").join(quoted)

    @staticmethod
    def _review_page_fingerprint(row):
        return (
            int(row["source_document_id"]), int(row["page_number"]),
            row["assignment_status"], row["document_group_id"],
            row["analysis_status"], row["suggested_category_id"],
            row["score"], row["explanation"], bool(row["deferred"]),
        )

    def query_review_batch(self, view: str = "SUGGESTED",
                           category_id: Optional[int] = None,
                           source_document_id: Optional[int] = None,
                           search: str = "", search_mode: str = "ALL",
                           limit: int = 50, minimum_score: float = 80,
                           minimum_matches: int = 2,
                           include_deferred: bool = False) -> Dict[str, Any]:
        """Return a stable, bounded cross-document review snapshot.

        Results are grouped only when adjacent pages in the same source have
        equivalent review state. The returned token is required for mutations,
        preventing a user from applying decisions to rows changed since display.
        """
        view = (view or "SUGGESTED").strip().upper()
        if view not in {"SUGGESTED", "NEEDS_REVIEW", "UNASSIGNED", "FAILED"}:
            raise ValueError(
                "Review view must be SUGGESTED, NEEDS_REVIEW, UNASSIGNED, "
                "or FAILED")
        limit = max(1, min(int(limit), 50))
        minimum_matches = max(1, int(minimum_matches))
        where, params = [], []
        if view == "SUGGESTED":
            where.extend(("a.status='PENDING'", "a.suggested_category_id IS NOT NULL",
                          "pa.status='UNCLASSIFIED'"))
            if category_id is not None:
                where.append("a.suggested_category_id=?")
                params.append(int(category_id))
        elif view == "NEEDS_REVIEW":
            where.extend(("a.status='PENDING'", "a.suggested_category_id IS NOT NULL",
                          "pa.status='NEEDS_REVIEW'"))
            if category_id is not None:
                where.append("a.suggested_category_id=?")
                params.append(int(category_id))
        elif view == "UNASSIGNED":
            where.extend(("pa.status='UNCLASSIFIED'",
                          "(a.status IS NULL OR a.status IN ('NO_MATCH','REJECTED','FAILED'))"))
        else:
            where.extend(("pa.status='UNCLASSIFIED'", "a.status='FAILED'"))
        if source_document_id is not None:
            where.append("pa.source_document_id=?")
            params.append(int(source_document_id))
        if not include_deferred:
            where.append("rd.source_document_id IS NULL")
        expression = self._review_search_expression(search, search_mode)
        if expression:
            where.append(
                """EXISTS (
                    SELECT 1 FROM page_text_search f
                    WHERE CAST(f.source_document_id AS INTEGER)=pa.source_document_id
                      AND CAST(f.page_number AS INTEGER)=pa.page_number
                      AND page_text_search MATCH ?
                )"""
            )
            params.append(expression)
        sql = """SELECT pa.source_document_id,pa.page_number,
                        pa.status assignment_status,pa.document_group_id,
                        d.filepath,d.employee_id,
                        a.suggested_category_id,a.score,a.explanation,
                        a.status analysis_status,c.name suggested_category_name,
                        CASE WHEN rd.source_document_id IS NULL THEN 0 ELSE 1 END deferred
                 FROM page_assignments pa
                 JOIN source_documents d ON d.id=pa.source_document_id
                 LEFT JOIN page_analysis a
                   ON a.source_document_id=pa.source_document_id
                  AND a.page_number=pa.page_number
                 LEFT JOIN categories c ON c.id=a.suggested_category_id
                 LEFT JOIN review_deferred_pages rd
                   ON rd.source_document_id=pa.source_document_id
                  AND rd.page_number=pa.page_number
                 WHERE {}
                 ORDER BY pa.source_document_id,pa.page_number""".format(
                     " AND ".join(where))
        with self.get_connection() as conn:
            rows = [dict(row) for row in conn.execute(sql, params)]

        def match_count(explanation):
            text = explanation or ""
            if not text.lower().startswith("matched:"):
                return 0
            matched = text.split(":", 1)[1].split(";", 1)[0]
            return len([part for part in matched.split(",") if part.strip()])

        groups = []
        for row in rows:
            row["deferred"] = bool(row["deferred"])
            eligible = (
                row["score"] is not None
                and float(row["score"]) >= float(minimum_score)
                and match_count(row["explanation"]) >= minimum_matches
                and "conflict:" not in (row["explanation"] or "").lower()
            )
            key = (
                row["source_document_id"], row["suggested_category_id"],
                row["analysis_status"], row["assignment_status"],
                row["deferred"], eligible, row["score"], row["explanation"],
            )
            if (groups and groups[-1]["_key"] == key
                    and groups[-1]["page_numbers"][-1] + 1 == row["page_number"]):
                groups[-1]["page_numbers"].append(row["page_number"])
                groups[-1]["_rows"].append(row)
                groups[-1]["score"] = min(
                    value for value in (groups[-1]["score"], row["score"])
                    if value is not None
                ) if row["score"] is not None else groups[-1]["score"]
                continue
            groups.append({
                "_key": key, "_rows": [row],
                "item_id": "{}:{}-{}".format(
                    row["source_document_id"], row["page_number"], row["page_number"]),
                "source_document_id": row["source_document_id"],
                "employee_id": row["employee_id"],
                "filepath": row["filepath"],
                "page_numbers": [row["page_number"]],
                "suggested_category_id": row["suggested_category_id"],
                "suggested_category_name": row["suggested_category_name"],
                "review_status": row["analysis_status"] or row["assignment_status"],
                "score": row["score"],
                "explanation": row["explanation"],
                "strong_eligible": eligible,
                "deferred": row["deferred"],
            })
        for group in groups:
            group["item_id"] = "{}:{}-{}".format(
                group["source_document_id"], group["page_numbers"][0],
                group["page_numbers"][-1])

        visible = groups[:limit]
        snapshot = {
            group["item_id"]: [self._review_page_fingerprint(row)
                               for row in group["_rows"]]
            for group in visible
        }
        token_payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        token = hashlib.sha256(token_payload.encode("utf-8")).hexdigest()
        self._review_batches[token] = snapshot
        items = []
        for group in visible:
            item = {key: value for key, value in group.items()
                    if not key.startswith("_")}
            items.append(item)
        return {
            "items": items, "total": len(groups), "remaining": len(groups),
            "limit": limit, "batch_token": token,
        }

    def _current_review_fingerprints(self, conn, entries):
        result = []
        for source_document_id, page_number, *_unused in entries:
            row = conn.execute(
                """SELECT pa.source_document_id,pa.page_number,
                          pa.status assignment_status,pa.document_group_id,
                          a.status analysis_status,a.suggested_category_id,
                          a.score,a.explanation,
                          CASE WHEN rd.source_document_id IS NULL THEN 0 ELSE 1 END deferred
                   FROM page_assignments pa
                   LEFT JOIN page_analysis a
                     ON a.source_document_id=pa.source_document_id
                    AND a.page_number=pa.page_number
                   LEFT JOIN review_deferred_pages rd
                     ON rd.source_document_id=pa.source_document_id
                    AND rd.page_number=pa.page_number
                   WHERE pa.source_document_id=? AND pa.page_number=?""",
                (source_document_id, page_number),
            ).fetchone()
            if not row:
                return None
            result.append(self._review_page_fingerprint(row))
        return result

    def apply_review_action(self, batch_token: str, item_ids: Iterable[str],
                            action: str,
                            category_id: Optional[int] = None) -> Dict[str, int]:
        """Atomically apply a decision to items from an unchanged snapshot."""
        identifiers = list(dict.fromkeys(item_ids))
        action = (action or "").strip().upper()
        if action not in {"APPROVE_SUGGESTION", "ASSIGN", "DEFER"}:
            raise ValueError("Unknown review action")
        if action == "ASSIGN" and category_id is None:
            raise ValueError("A category is required for assignment")
        snapshot = self._review_batches.get(batch_token)
        if snapshot is None or any(identifier not in snapshot for identifier in identifiers):
            raise RuntimeError("Review batch is stale; refresh before applying changes")
        selected = {identifier: snapshot[identifier] for identifier in identifiers}
        changed_documents = set()
        pages_changed = 0
        with self.transaction() as conn:
            for identifier, expected in selected.items():
                current = self._current_review_fingerprints(conn, expected)
                if current != expected:
                    raise RuntimeError(
                        "Review data changed; refresh the stale batch before continuing")
            if action == "ASSIGN":
                exists = conn.execute(
                    "SELECT 1 FROM categories WHERE id=?", (category_id,)
                ).fetchone()
                if not exists:
                    raise ValueError("Category no longer exists")
            for expected in selected.values():
                source_id = expected[0][0]
                changed_documents.add(source_id)
                pages = [entry[1] for entry in expected]
                if action == "DEFER":
                    conn.executemany(
                        """INSERT INTO review_deferred_pages(source_document_id,page_number)
                           VALUES(?,?) ON CONFLICT(source_document_id,page_number)
                           DO UPDATE SET deferred_at=CURRENT_TIMESTAMP""",
                        ((source_id, page) for page in pages),
                    )
                    continue
                target_category = category_id
                if action == "APPROVE_SUGGESTION":
                    categories = {entry[5] for entry in expected}
                    if len(categories) != 1 or None in categories:
                        raise RuntimeError("Suggestion changed; refresh before approval")
                    target_category = categories.pop()
                group = conn.execute(
                    """SELECT id FROM document_groups
                       WHERE source_document_id=? AND category_id=?
                       ORDER BY instance_number LIMIT 1""",
                    (source_id, target_category),
                ).fetchone()
                if group:
                    group_id = int(group["id"])
                else:
                    group_id = int(conn.execute(
                        """INSERT INTO document_groups
                           (source_document_id,category_id,instance_number)
                           VALUES(?,?,1)""",
                        (source_id, target_category),
                    ).lastrowid)
                conn.executemany(
                    """UPDATE page_assignments SET document_group_id=?,status='ASSIGNED'
                       WHERE source_document_id=? AND page_number=?""",
                    ((group_id, source_id, page) for page in pages),
                )
                if action == "APPROVE_SUGGESTION":
                    conn.executemany(
                        """UPDATE page_analysis SET status='ACCEPTED'
                           WHERE source_document_id=? AND page_number=?""",
                        ((source_id, page) for page in pages),
                    )
                conn.executemany(
                    """DELETE FROM review_deferred_pages
                       WHERE source_document_id=? AND page_number=?""",
                    ((source_id, page) for page in pages),
                )
                pages_changed += len(pages)
            for source_id in changed_documents:
                self._refresh_review_status(conn, source_id)
        return {
            "items_changed": len(selected),
            "pages_changed": pages_changed,
            "documents_changed": len(changed_documents),
        }

    # Thin UI-oriented adapters retain the snapshot token requirement.
    def list_review_categories(self) -> List[Dict]:
        return self.list_categories()

    def query_review_items(self, filters=None, limit: int = 50) -> Dict[str, Any]:
        values = dict(filters or {})
        values["limit"] = limit
        return self.query_review_batch(**values)

    def approve_review_items(self, item_ids, batch_token):
        return self.apply_review_action(
            batch_token, item_ids, "APPROVE_SUGGESTION")

    def assign_review_items(self, item_ids, category_id, batch_token):
        return self.apply_review_action(
            batch_token, item_ids, "ASSIGN", category_id=category_id)

    def defer_review_items(self, item_ids, batch_token):
        return self.apply_review_action(batch_token, item_ids, "DEFER")

    def export_groups(self, source_document_id: int) -> List[Dict]:
        sql = """SELECT dg.id, dg.instance_number, c.id category_id,
                        c.name category_name, c.output_policy,
                        c.filename_pattern, GROUP_CONCAT(pa.page_number) pages
                 FROM document_groups dg JOIN categories c ON c.id=dg.category_id
                 JOIN page_assignments pa ON pa.document_group_id=dg.id AND pa.status='ASSIGNED'
                 WHERE dg.source_document_id=? GROUP BY dg.id ORDER BY c.name,dg.instance_number"""
        with self.get_connection() as conn:
            rows = []
            for row in conn.execute(sql, (source_document_id,)):
                item = dict(row)
                item["pages"] = sorted(int(p) for p in item["pages"].split(","))
                rows.append(item)
            return rows

    def create_background_job(self, source_document_id: int, job_type: str,
                              items_total: int = 0, max_attempts: int = 3) -> int:
        job_type = job_type.strip().upper()
        if job_type not in {"INGEST", "OCR", "GENERATE"}:
            raise ValueError("Unknown job type")
        if items_total < 0 or max_attempts < 1:
            raise ValueError("Invalid job limits")
        with self.transaction() as conn:
            active = conn.execute(
                """SELECT id FROM background_jobs
                   WHERE source_document_id=? AND job_type=?
                     AND status IN ('QUEUED','RUNNING')""",
                (source_document_id, job_type),
            ).fetchone()
            if active:
                return int(active["id"])
            cur = conn.execute(
                """INSERT INTO background_jobs
                   (source_document_id,job_type,items_total,max_attempts)
                   VALUES(?,?,?,?)""",
                (source_document_id, job_type, items_total, max_attempts),
            )
            if job_type == "OCR":
                conn.execute(
                    "UPDATE source_documents SET ocr_status='QUEUED' WHERE id=?",
                    (source_document_id,),
                )
            elif job_type == "GENERATE":
                conn.execute(
                    "UPDATE source_documents SET generation_status='QUEUED' WHERE id=?",
                    (source_document_id,),
                )
            return int(cur.lastrowid)

    # Coordinator-friendly alias.
    enqueue_job = create_background_job

    def claim_next_job(self, job_types: Optional[Iterable[str]] = None) -> Optional[Dict]:
        """Atomically claim the oldest eligible job.

        One manager serializes local workers, while the conditional UPDATE also
        keeps this correct if a future process uses its own SQLite connection.
        """
        types = [value.strip().upper() for value in (job_types or [])]
        where, params = "status='QUEUED' AND attempt_count < max_attempts", []
        if types:
            where += " AND job_type IN ({})".format(",".join("?" for _ in types))
            params.extend(types)
        with self.transaction() as conn:
            row = conn.execute(
                """SELECT * FROM background_jobs WHERE {}
                   ORDER BY created_at,id LIMIT 1""".format(where), params
            ).fetchone()
            if not row:
                return None
            changed = conn.execute(
                """UPDATE background_jobs SET status='RUNNING',
                   attempt_count=attempt_count+1,started_at=CURRENT_TIMESTAMP,
                   heartbeat_at=CURRENT_TIMESTAMP,error_message=NULL
                   WHERE id=? AND status='QUEUED'""", (row["id"],)
            ).rowcount
            if not changed:
                return None
            job = conn.execute(
                "SELECT * FROM background_jobs WHERE id=?", (row["id"],)
            ).fetchone()
            column = {"INGEST": "ingestion_status", "OCR": "ocr_status",
                      "GENERATE": "generation_status"}[job["job_type"]]
            conn.execute(
                "UPDATE source_documents SET {}='RUNNING' WHERE id=?".format(column),
                (job["source_document_id"],),
            )
            return dict(job)

    def get_job(self, job_id: int) -> Optional[Dict]:
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM background_jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def update_job_progress(self, job_id: int, items_completed: Optional[int] = None,
                            items_failed: Optional[int] = None,
                            items_total: Optional[int] = None) -> bool:
        values = {"items_completed": items_completed, "items_failed": items_failed,
                  "items_total": items_total}
        values = {key: value for key, value in values.items() if value is not None}
        if any(value < 0 for value in values.values()):
            raise ValueError("Job progress cannot be negative")
        assignments = ["heartbeat_at=CURRENT_TIMESTAMP"]
        params = []
        for key, value in values.items():
            assignments.append("{}=?".format(key))
            params.append(value)
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE background_jobs SET {} WHERE id=? AND status='RUNNING'".format(
                    ",".join(assignments)
                ), params + [job_id],
            )
            return cur.rowcount == 1

    def complete_job(self, job_id: int) -> bool:
        with self.transaction() as conn:
            job = conn.execute(
                "SELECT * FROM background_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not job or job["status"] not in ("RUNNING", "QUEUED"):
                return False
            conn.execute(
                """UPDATE background_jobs SET status='COMPLETE',
                   completed_at=CURRENT_TIMESTAMP,heartbeat_at=CURRENT_TIMESTAMP,
                   items_completed=CASE WHEN items_completed < items_total
                                        THEN items_total ELSE items_completed END
                   WHERE id=?""", (job_id,),
            )
            column = {"INGEST": "ingestion_status", "OCR": "ocr_status",
                      "GENERATE": "generation_status"}[job["job_type"]]
            conn.execute(
                "UPDATE source_documents SET {}='COMPLETE' WHERE id=?".format(column),
                (job["source_document_id"],),
            )
            return True

    def fail_job(self, job_id: int, error_message: str, retry: bool = True) -> bool:
        with self.transaction() as conn:
            job = conn.execute(
                "SELECT * FROM background_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not job or job["status"] not in ("RUNNING", "QUEUED"):
                return False
            can_retry = retry and job["attempt_count"] < job["max_attempts"]
            status = "QUEUED" if can_retry else "FAILED"
            conn.execute(
                """UPDATE background_jobs SET status=?,error_message=?,
                   completed_at=CASE WHEN ?='FAILED' THEN CURRENT_TIMESTAMP ELSE NULL END,
                   heartbeat_at=CURRENT_TIMESTAMP WHERE id=?""",
                (status, str(error_message), status, job_id),
            )
            column = {"INGEST": "ingestion_status", "OCR": "ocr_status",
                      "GENERATE": "generation_status"}[job["job_type"]]
            conn.execute(
                "UPDATE source_documents SET {}=? WHERE id=?".format(column),
                (status, job["source_document_id"]),
            )
            return True

    def recover_stale_jobs(self, stale_after_seconds: int = 300) -> int:
        if stale_after_seconds < 0:
            raise ValueError("Stale interval cannot be negative")
        modifier = "-{} seconds".format(stale_after_seconds)
        with self.transaction() as conn:
            rows = conn.execute(
                """SELECT id,source_document_id,job_type,attempt_count,max_attempts
                   FROM background_jobs WHERE status='RUNNING'
                   AND COALESCE(heartbeat_at,started_at,created_at)
                       <= datetime('now', ?)""", (modifier,)
            ).fetchall()
            for job in rows:
                exhausted = job["attempt_count"] >= job["max_attempts"]
                status = "FAILED" if exhausted else "QUEUED"
                conn.execute(
                    """UPDATE background_jobs SET status=?,error_message='Worker interrupted',
                       completed_at=CASE WHEN ?='FAILED' THEN CURRENT_TIMESTAMP ELSE NULL END
                       WHERE id=?""", (status, status, job["id"])
                )
                column = {"INGEST": "ingestion_status", "OCR": "ocr_status",
                          "GENERATE": "generation_status"}[job["job_type"]]
                conn.execute(
                    "UPDATE source_documents SET {}=? WHERE id=?".format(column),
                    (status, job["source_document_id"]),
                )
            return len(rows)

    def list_pending_ocr_pages(self, source_document_id: int) -> List[int]:
        with self.get_connection() as conn:
            return [int(row["page_number"]) for row in conn.execute(
                """SELECT pa.page_number FROM page_assignments pa
                   LEFT JOIN page_analysis an
                     ON an.source_document_id=pa.source_document_id
                    AND an.page_number=pa.page_number
                   WHERE pa.source_document_id=?
                     AND (an.page_number IS NULL OR an.status='FAILED')
                   ORDER BY pa.page_number""", (source_document_id,)
            )]

    def create_generation_run(self, source_document_id: int, output_directory: str,
                              expected_outputs: int, job_id: Optional[int] = None) -> int:
        with self.transaction() as conn:
            return int(conn.execute(
                """INSERT INTO generation_runs
                   (source_document_id,job_id,output_directory,expected_outputs)
                   VALUES(?,?,?,?)""",
                (source_document_id, job_id, str(output_directory), expected_outputs),
            ).lastrowid)

    def record_output_file(self, generation_run_id: int, source_document_id: int,
                           output_path: str, expected_page_count: int,
                           category_id: Optional[int] = None,
                           actual_page_count: Optional[int] = None,
                           file_size: Optional[int] = None, sha256: Optional[str] = None,
                           status: str = "PLANNED",
                           error_message: Optional[str] = None) -> int:
        status = status.upper()
        if status not in {"PLANNED", "WRITTEN", "VERIFIED", "FAILED"}:
            raise ValueError("Unknown output status")
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO output_files
                   (generation_run_id,source_document_id,category_id,output_path,
                    expected_page_count,actual_page_count,file_size,sha256,status,
                    error_message,generated_at,verified_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,
                          CASE WHEN ?='VERIFIED' THEN CURRENT_TIMESTAMP END)
                   ON CONFLICT(generation_run_id,output_path) DO UPDATE SET
                    actual_page_count=excluded.actual_page_count,
                    file_size=excluded.file_size,sha256=excluded.sha256,
                    status=excluded.status,error_message=excluded.error_message,
                    generated_at=CURRENT_TIMESTAMP,
                    verified_at=CASE WHEN excluded.status='VERIFIED'
                                     THEN CURRENT_TIMESTAMP END""",
                (generation_run_id, source_document_id, category_id, str(output_path),
                 expected_page_count, actual_page_count, file_size, sha256, status,
                 error_message, status),
            )
            row = conn.execute(
                "SELECT id FROM output_files WHERE generation_run_id=? AND output_path=?",
                (generation_run_id, str(output_path)),
            ).fetchone()
            return int(row["id"])

    def complete_generation_run(self, generation_run_id: int,
                                error_message: Optional[str] = None) -> bool:
        with self.transaction() as conn:
            run = conn.execute(
                "SELECT * FROM generation_runs WHERE id=?", (generation_run_id,)
            ).fetchone()
            if not run:
                return False
            verified = int(conn.execute(
                "SELECT COUNT(*) FROM output_files WHERE generation_run_id=? AND status='VERIFIED'",
                (generation_run_id,),
            ).fetchone()[0])
            complete = error_message is None and verified == run["expected_outputs"]
            status = "COMPLETE" if complete else "FAILED"
            message = error_message
            if not complete and not message:
                message = "Only {} of {} outputs were verified".format(
                    verified, run["expected_outputs"])
            conn.execute(
                """UPDATE generation_runs SET status=?,verified_outputs=?,
                   error_message=?,completed_at=CURRENT_TIMESTAMP WHERE id=?""",
                (status, verified, message, generation_run_id),
            )
            conn.execute(
                "UPDATE source_documents SET generation_status=? WHERE id=?",
                (status, run["source_document_id"]),
            )
            return complete

    def get_completion_manifest(self, source_document_id: int) -> Optional[Dict]:
        with self.get_connection() as conn:
            run = conn.execute(
                """SELECT * FROM generation_runs WHERE source_document_id=?
                   ORDER BY id DESC LIMIT 1""", (source_document_id,)
            ).fetchone()
            if not run:
                return None
            result = dict(run)
            result["outputs"] = [dict(row) for row in conn.execute(
                """SELECT of.*,c.name category_name FROM output_files of
                   LEFT JOIN categories c ON c.id=of.category_id
                   WHERE generation_run_id=? ORDER BY of.id""", (run["id"],)
            )]
            return result

    def get_dashboard_summary(self) -> Dict[str, int]:
        summary = {key: 0 for key in (
            "TOTAL", "NEW", "OCR_RUNNING", "NEEDS_REVIEW", "READY_TO_GENERATE",
            "GENERATION_RUNNING", "COMPLETED", "COMPLETED_TODAY", "ERRORS")}
        for row in self.list_dashboard_documents():
            summary["TOTAL"] += 1
            summary[row["overall_status"]] += 1
            if row["overall_status"] == "COMPLETED" and row["completed_today"]:
                summary["COMPLETED_TODAY"] += 1
        return summary

    def list_dashboard_documents(self, status_filter: Optional[str] = None) -> List[Dict]:
        sql = """SELECT sd.*,
                    (SELECT COUNT(*) FROM page_analysis an
                     WHERE an.source_document_id=sd.id
                       AND an.status<>'FAILED') ocr_completed,
                    (SELECT COUNT(*) FROM page_analysis an
                     WHERE an.source_document_id=sd.id
                       AND an.status='FAILED') ocr_failed,
                    (SELECT COUNT(*) FROM page_assignments pa
                     WHERE pa.source_document_id=sd.id
                       AND pa.status IN ('ASSIGNED','EXCLUDED')) review_completed,
                    (SELECT COUNT(*) FROM output_files of
                     WHERE of.generation_run_id=(
                         SELECT gr.id FROM generation_runs gr
                         WHERE gr.source_document_id=sd.id
                         ORDER BY gr.id DESC LIMIT 1)
                       AND of.status='VERIFIED') outputs_verified,
                    COALESCE((SELECT expected_outputs FROM generation_runs gr
                              WHERE gr.source_document_id=sd.id
                              ORDER BY id DESC LIMIT 1),0) outputs_total
                    ,COALESCE((SELECT
                        CASE WHEN gr.status='COMPLETE'
                                   AND date(gr.completed_at,'localtime')
                                       =date('now','localtime')
                             THEN 1 ELSE 0 END
                        FROM generation_runs gr
                        WHERE gr.source_document_id=sd.id
                        ORDER BY gr.id DESC LIMIT 1),0) completed_today
                 FROM source_documents sd ORDER BY sd.created_at DESC,sd.id DESC"""
        with self.get_connection() as conn:
            rows = [dict(row) for row in conn.execute(sql)]
        for row in rows:
            if row["generation_status"] == "COMPLETE":
                row["overall_status"] = "COMPLETED"
            elif row["generation_status"] == "FAILED" or row["ocr_status"] == "FAILED":
                row["overall_status"] = "ERRORS"
            elif row["generation_status"] in ("QUEUED", "RUNNING"):
                row["overall_status"] = "GENERATION_RUNNING"
            elif row["review_status"] in ("READY", "APPROVED"):
                row["overall_status"] = "READY_TO_GENERATE"
            elif row["ocr_status"] in ("QUEUED", "RUNNING"):
                row["overall_status"] = "OCR_RUNNING"
            elif row["ocr_status"] in ("COMPLETE", "COMPLETE_WITH_ERRORS"):
                row["overall_status"] = "NEEDS_REVIEW"
            else:
                row["overall_status"] = "NEW"
        if status_filter and status_filter.upper() != "ALL":
            normalized = status_filter.replace(" ", "_").upper()
            if normalized == "NEEDS_ATTENTION":
                normalized = "ERRORS"
            rows = [row for row in rows
                    if row["overall_status"] == normalized]
        return rows
