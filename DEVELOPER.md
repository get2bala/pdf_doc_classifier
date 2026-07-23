# Developer guide

## Source map

- `main_window.py`: application startup and page-review workspace
- `dashboard.py`: queue and progress dashboard
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
