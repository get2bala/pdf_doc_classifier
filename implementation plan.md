# Implemented end-to-end plan

The earlier verbose design was reduced to a tested vertical architecture. See
`ARCHITECTURE.md` for the design decision and `README.md` for operating details.

## Completed

- Versioned SQLite persistence, settings, category rules, assignments, analysis
- PDF validation, preview rendering, output planning, atomic verified export
- Embedded searchable-PDF text extraction and deterministic keyword suggestions
- Manual assignment, exclusion/reset, suggestion acceptance/rejection
- Whole-document text extraction on a background worker
- Stable Inbox discovery with SHA-256 duplicate detection
- Persistent OCR and generation jobs with progress, retry and restart recovery
- Queue-first dashboard with aggregate and per-document progress
- Cross-document category, unassigned, and extraction-failure review queues
- Full-text page search, cached thumbnails, and fixed 50-item review batches
- Transactional bulk approve, assign, and defer actions with stale-batch checks
- One-click generation of every ready document with per-document failure isolation
- Canonical dashboard counts, six clickable one-row tiles, and latest-attempt progress
- Contextual review decisions without adjacent approve/assign controls
- Standard File/View/Tools/Help menus and searchable offline help
- Branded macOS/Windows application icons and accessible selection states
- Automatic classification policies and configurable thresholds
- Verified output manifests and Completed/Error folder handling
- Configuration UI for folders, extraction policy, categories and filenames
- Read-only database table viewer for every application-owned table
- Document removal that preserves source files
- Export confirmation and strict unresolved-page validation
- Installable Python metadata and PyInstaller desktop recipe
- Unit, integration, real-PDF, persistence, failure-path, and offscreen UI tests

## Deferred until representative documents are available

- Automatic document-boundary inference
- OCR layout-block persistence and audit-grade OCR versioning
- Unattended assignment approval

These features should be justified by anonymized production samples before they
are added; none is required for the robust operator-approved workflow.
