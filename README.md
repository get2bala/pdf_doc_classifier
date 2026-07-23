# PDF Document Classifier

An offline desktop application for PDFs that already contain searchable text. It
discovers documents, extracts each page's embedded text in the background,
matches editable category keywords, supports operator review, and records
verified PDF output manifests. It does not require Tesseract or an OCR engine.

## Install and run

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run_app.py
```

The SQLite database is stored at
`~/.pdf_doc_classifier/classifier.sqlite3`.

## Configuration

Open **Tools → Configuration**.

### General

- Inbox: stable PDFs are discovered automatically.
- Output: verified generated PDFs are published here.
- Completed: successfully processed source PDFs move here.
- Error: invalid PDFs can be moved here.
- Automatic extraction: extract searchable PDF text after discovery.
- Assignment mode:
  - Suggestions only
  - Preassign for review
  - Automatic assignment
- Minimum rule score and minimum keyword matches.

### Categories

Every category has an editable name, comma-separated matching phrases, output
policy, and filename pattern. For example:

```text
PAN Card
permanent account number,income tax department
COMBINE
{employee_id}_{category}.pdf
```

Configuration is stored in SQLite and applied to the running background
coordinator immediately. Use **Tools → View Database** for a safe read-only
view of settings, documents, extracted text, jobs, assignments, and outputs.

## Automated workflow

```text
Stable searchable PDF in Inbox
  → SHA-256 duplicate check
  → persistent page-text extraction job
  → editable keyword rules
  → configured assignment policy
  → operator resolves remaining pages
  → queued generation job
  → verify page counts, sizes and hashes
  → completion manifest
  → source moves to Completed
```

The dashboard shows document counts and per-document extraction, review, and
generation progress. Opening a document is not required for extraction to run.

## Fast cross-document review

Open **Review Workspace** to work across all source PDFs instead of opening them
one at a time.

- **Suggestions** starts with high-confidence matches selected. Scan the
  thumbnails, deselect exceptions, and approve the batch.
- **Needs Review**, **Unassigned**, and **Extraction Failures** provide focused
  queues for the remaining work.
- Category and status filters can be combined with full-text search over the
  extracted PDF text.
- At most 50 page ranges are displayed at once. After an action, the next batch
  is loaded automatically.
- **Open in Document** jumps back to the original PDF and page for close
  inspection.

The gallery supports standard desktop selection: arrow keys move focus,
**Space** toggles a card, **Shift+Arrow** extends a range, **Command+A** on macOS
or **Ctrl+A** on Windows selects the visible actionable batch, **Escape** clears
the selection, and **Return** opens the focused source page. The gallery
automatically reflows when the window is resized.

Choose **System**, **Light**, or **Dark** under
**Tools → Configuration → General → Appearance**. System mode follows the
operating-system appearance, including changes while the application is open.

Thumbnails are generated asynchronously and cached on disk beside the database,
so revisiting a page does not render it again. Once documents are fully assigned,
use **Generate all ready** on the dashboard to queue them together. A failure in
one document does not stop the others.

The dashboard uses one row of clickable count tiles: All Documents, New, Needs
Review, Ready to Generate, Completed, and Errors. Clicking a tile filters the
table; clicking All Documents clears the filter. Completed includes every
successfully generated document, not only today's work. Processing progress
remains visible in the table without consuming separate summary tiles. Open
**Help → User Guide** for the searchable offline guide or
**Help → Keyboard Shortcuts** for a quick reference.

The database retains historical column/job names such as `ocr_status` and `OCR`
for migration compatibility. In this version they mean embedded-text extraction;
no image OCR executable is invoked.

If a page has no searchable text, it produces a controlled `NO_MATCH`. The user
can still classify that page manually. Scanned image-only PDFs must first be made
searchable by the upstream document provider.

## Completion tracking

Generation is complete only when every planned PDF is written in staging,
reopened, page-counted, hashed, published without overwriting, and recorded as
`VERIFIED` in `output_files`.

## Tests

```bash
QT_QPA_PLATFORM=offscreen python3 -m pytest -q
```

The suite includes real searchable-PDF extraction and thumbnails, cross-document
search/review, 50-item batching, atomic bulk actions, persistent jobs, restart
recovery, category policies, dashboard behavior, bulk PDF generation, manifests,
source movement, corrupt files, migrations, and packaging.

## Modify the program

See [DEVELOPER.md](DEVELOPER.md) for the source layout, safe extension points,
tests, schema migration rules, and packaging commands.
