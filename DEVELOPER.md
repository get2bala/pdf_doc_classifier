# Developer guide

## Source map

- `main_window.py`: application startup and page-review workspace
- `dashboard.py`: queue and progress dashboard
- `review_workspace.py`: cross-document review cards, filters, and bulk actions
- `thumbnail_service.py`: bounded memory/disk thumbnail cache
- `thumbnail_adapter.py`: asynchronous Qt/database thumbnail integration
- `appearance.py`: accessible semantic palettes and system appearance tracking
- `help_dialogs.py`: searchable offline user guide, shortcuts, and About/privacy
- `dialogs.py`: editable configuration and read-only database viewer
- `workflow.py`: Inbox scanning and persistent job coordinator
- `classifier.py`: embedded-text extraction and keyword matching
- `pdf_engine.py`: rendering, output planning, verification and publication
- `database.py`: schema migrations and all persistent workflow state

## Safe modification workflow

Create a virtual environment, install dependencies, make a focused change, and
run the complete suite:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
QT_QPA_PLATFORM=offscreen python3 -m pytest -q
python run_app.py
```

Application configuration belongs in `application_settings`, not constants.
Category matching rules belong in `categories` and should be changed through the
Configuration dialog. Workflow records must be changed through `DatabaseManager`
APIs rather than raw UI SQL.

## Important extension points

- Add matching behavior in `suggest_category()` while preserving deterministic
  explanations and tests.
- Add settings in `SettingsDialog`, persist them through `DatabaseSettings`, and
  reload them in `WorkflowCoordinator.reload_settings()`.
- Add schema changes additively in `DatabaseManager._migrate()` and increment
  `SCHEMA_VERSION`; never rebuild a user's database.
- Add dashboard state by deriving it from persistent records rather than storing
  a second conflicting overall status.
- Keep `list_dashboard_documents()` as the canonical status projection;
  `get_dashboard_summary()` must aggregate those exact rows so tile and filter
  counts cannot diverge.
- Dashboard summary buttons are the only dashboard filter surface. Keep the six
  tiles on one row and expose row actions through double-click or its context
  menu instead of adding another toolbar.
- Add review filters through `query_review_batch()` and preserve its hard
  50-item cap, snapshot token, and transactional action semantics.
- Use semantic `QPalette` roles and the shared appearance stylesheet; avoid
  literal colors or widget-level light-only styles.

Schema version 4 adds the FTS5 page-text index and deferred-review state.
Triggers keep the index synchronized after the one-time migration backfill.

The internal `ocr_*` names are retained for compatibility with existing schema
version 3 databases. Renaming them would require a carefully tested migration and
provides little operational value.

## Packaging

The PyInstaller recipe is `PdfDocumentClassifier.spec`. Because embedded PDF text
is used, no Tesseract binaries or trained-data files need to be bundled.

Build each operating system on that operating system:

```bash
pip install '.[package]'
pyinstaller PdfDocumentClassifier.spec
```
