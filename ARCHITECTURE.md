# Simplified architecture decision

## The workflow

```text
Stable Inbox PDF -> persistent text-extraction job -> suggestion/automation policy
                                      -> operator review -> generation job
                                      -> verified manifest -> Completed
```

Only `page_assignments` controls output. Extraction writes `page_analysis`; accepting a
suggestion uses the same assignment operation as manual classification.

## What was simplified

The earlier plan proposed repositories for every table, multiple domain layers,
OCR runs and blocks, a job queue, boundary scoring, a folder watcher, and several
worker pools. Those components make recovery and packaging harder before the real
workflow has been validated. This version uses:

- one SQLite manager with a serialized, thread-safe connection;
- one PDF module for validation, preview, and staged export;
- one deterministic keyword suggestion module using embedded PDF text;
- one queue-first PySide6 dashboard and classification workspace;
- one persistent SQLite job coordinator running outside the UI thread.

## Safety invariants

- Source PDFs are read-only and their size/mtime must still match the import.
- Every page must be assigned or explicitly excluded before export.
- A group can only receive pages from its own source document.
- Page numbers must exist in the imported document.
- Output is generated and reopened in a staging directory before publication.
- Existing output is never overwritten; partial publication is rolled back.
- Missing/failed text extraction never blocks manual classification.
- A document is complete only when every planned output has a verified manifest.
- Interrupted jobs are recovered from persisted heartbeat and attempt state.

## Intentionally deferred

- Layout-block persistence: page text and an explanation are sufficient for
  keyword suggestions. Add layout data only if future rules require it.
- Automatic boundary detection: `SEPARATE` already splits contiguous runs and is
  deterministic. Learned/heuristic boundaries need a representative corpus.
- Automatic unattended approval: suggestions remain advisory by design.

Configuration, a safe read-only database viewer, schema migration, document
lifecycle controls, stable Inbox polling, background jobs, dashboard progress,
output manifests, restart recovery, and a PyInstaller recipe are included.
