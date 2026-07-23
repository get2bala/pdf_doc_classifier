"""Queue-first dashboard widgets for the persistent document workflow."""

from pathlib import Path

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QDialog, QDialogButtonBox, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)


FILTER_VALUES = {
    "All": "ALL", "Needs attention": "NEEDS_ATTENTION",
    "Extracting text": "OCR_RUNNING", "Needs review": "NEEDS_REVIEW",
    "Ready to generate": "READY_TO_GENERATE", "Completed": "COMPLETED",
    "Generating": "GENERATION_RUNNING", "Errors": "ERRORS",
}
DISPLAY_STATUS = {
    "NEW": "New",
    "OCR_RUNNING": "Extracting Text",
    "NEEDS_REVIEW": "Needs Review",
    "READY_TO_GENERATE": "Ready to Generate",
    "GENERATION_RUNNING": "Generating",
    "COMPLETED": "Completed",
    "ERRORS": "Error",
}


def _value(row, *names, default=None):
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return default


class CompletionManifestDialog(QDialog):
    """Readable completion details backed by the recorded output manifest."""

    def __init__(self, document, outputs, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Completion manifest")
        self.resize(760, 440)
        layout = QVBoxLayout(self)
        filename = Path(_value(document, "filepath", default="Document")).name
        layout.addWidget(QLabel("<b>{}</b><br>Employee: {}<br>Status: {}".format(
            filename, _value(document, "employee_id", default="—"),
            _value(document, "overall_status", "status", default="Completed"))))
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(("Output", "Pages", "Size", "SHA-256", "Verified"))
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for output in outputs:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = (
                Path(_value(output, "output_path", "path", default="")).name,
                str(_value(output, "actual_page_count", "page_count",
                           "expected_page_count", default="—")),
                str(_value(output, "file_size", default="—")),
                str(_value(output, "sha256", default="—")),
                "Yes" if _value(output, "verified_at", default=False) or
                _value(output, "status", default="") in ("VERIFIED", "COMPLETE") else "No",
            )
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))
        layout.addWidget(self.table)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class DashboardWidget(QWidget):
    """Operational overview; database state remains the source of truth."""

    document_open_requested = Signal(int)

    HEADERS = ("File", "Employee", "Pages", "Text", "Review", "Output", "Status")
    SUMMARY_KEYS = (
        ("all", "All Documents", "ALL"),
        ("new", "New", "NEW"),
        ("needs_review", "Needs Review", "NEEDS_REVIEW"),
        ("ready_to_generate", "Ready to Generate", "READY_TO_GENERATE"),
        ("completed", "Completed", "COMPLETED"),
        ("errors", "Errors", "ERRORS"),
    )

    def __init__(self, database, coordinator=None, bulk_confirm=None, parent=None):
        super().__init__(parent)
        self.database = database
        self.coordinator = coordinator
        self.bulk_confirm = bulk_confirm or self._confirm_bulk_generation
        self._rows = []
        self._active_filter = "ALL"
        layout = QVBoxLayout(self)
        title = QHBoxLayout()
        heading = QLabel("<h2>Dashboard</h2>")
        title.addWidget(heading)
        title.addStretch()
        layout.addLayout(title)

        self.summary_layout = QGridLayout()
        self.summary_layout.setHorizontalSpacing(12)
        self.summary_buttons = {}
        self.summary_group = QButtonGroup(self)
        self.summary_group.setExclusive(True)
        for index, (key, caption, status_filter) in enumerate(self.SUMMARY_KEYS):
            card = QPushButton("0\n{}".format(caption))
            card.setCheckable(True)
            card.setMinimumHeight(66)
            card.setObjectName("SummaryCard")
            card.setAccessibleName(
                "{} documents; filter dashboard".format(caption))
            card.clicked.connect(
                lambda _checked=False, value=status_filter:
                self.set_summary_filter(value))
            self.summary_buttons[key] = card
            self.summary_group.addButton(card)
            self.summary_layout.addWidget(card, 0, index)
        self.summary_buttons["all"].setChecked(True)
        layout.addLayout(self.summary_layout)

        from PySide6.QtWidgets import QMenu
        self.document_menu = QMenu(self)
        self.retry_action = QAction("Retry Processing", self)
        self.manifest_action = QAction("View Results", self)
        self.document_menu.addAction(self.retry_action)
        self.document_menu.addAction(self.manifest_action)

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.setToolTip(
            "Double-click a document for its next action. "
            "Right-click for processing options.")
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)
        self.status = QLabel()
        layout.addWidget(self.status)

        self.retry_action.triggered.connect(self.retry_selected)
        self.manifest_action.triggered.connect(self.show_manifest)
        self.table.doubleClicked.connect(lambda _index: self.run_primary_action())
        self.table.customContextMenuRequested.connect(
            self.show_document_menu)
        self.table.itemSelectionChanged.connect(self._update_actions)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(2000)
        self.refresh_timer.timeout.connect(self.refresh)
        self.refresh_timer.start()
        self.refresh()

    def _summary(self):
        if hasattr(self.database, "get_dashboard_summary"):
            result = self.database.get_dashboard_summary()
            summary = {
                str(key).lower(): value for key, value in result.items()}
            summary.setdefault("all", summary.get("total", sum(
                int(summary.get(key, 0)) for key in (
                    "new", "ocr_running", "needs_review",
                    "ready_to_generate", "generation_running",
                    "completed", "errors"))))
            return summary
        rows = self._documents("All")
        statuses = [str(_value(row, "overall_status", "status", default="")).upper()
                    for row in rows]
        return {
            "all": len(statuses),
            "new": sum(status in ("NEW", "IN_PROGRESS") for status in statuses),
            "ocr_running": sum(status == "OCR_RUNNING" for status in statuses),
            "needs_review": sum(status == "NEEDS_REVIEW" for status in statuses),
            "ready_to_generate": sum(status == "READY_TO_GENERATE" for status in statuses),
            "completed": sum(status in ("COMPLETED", "EXPORTED") for status in statuses),
            "errors": sum("ERROR" in status or "FAILED" in status for status in statuses),
        }

    def _documents(self, status_filter):
        if hasattr(self.database, "list_dashboard_documents"):
            canonical = FILTER_VALUES.get(status_filter, status_filter)
            try:
                if canonical == "NEEDS_ATTENTION":
                    rows = self.database.list_dashboard_documents()
                    return [row for row in rows if row.get("overall_status") not in
                            ("COMPLETED", "OCR_RUNNING", "NEW")]
                return self.database.list_dashboard_documents(canonical)
            except TypeError:
                return self.database.list_dashboard_documents()
        return self.database.list_source_documents()

    def refresh(self):
        summary = self._summary()
        for key, caption, _status_filter in self.SUMMARY_KEYS:
            value = summary.get(key, summary.get(key.upper(), 0))
            self.summary_buttons[key].setText(
                "{}\n{}".format(value, caption))
            self.summary_buttons[key].setAccessibleName(
                "{}: {} documents; filter dashboard".format(caption, value))
        selected_id = self.selected_document_id()
        requested = self._active_filter
        rows = list(self._documents(requested))
        if requested != "ALL" and not hasattr(
                self.database, "list_dashboard_documents"):
            wanted = requested.replace(" ", "_").upper()
            rows = [row for row in rows if wanted in
                    str(_value(row, "overall_status", "status", default="")).upper()]
        self._rows = rows
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            total = int(_value(row, "page_count", "pages_total", default=0))
            ocr_done = _value(row, "ocr_completed", "ocr_pages_completed", default=0)
            review_done = _value(row, "review_completed", "reviewed_pages", default=0)
            output_done = _value(row, "outputs_verified", "verified_outputs",
                                 "output_completed", default=0)
            output_total = _value(row, "outputs_total", "output_total", default=0)
            text_progress = "{}/{}".format(ocr_done, total)
            failed = int(_value(row, "ocr_failed", default=0))
            if failed:
                text_progress += " · {} failed".format(failed)
            values = (
                Path(_value(row, "filepath", "filename", default="")).name,
                str(_value(row, "employee_id", default="—")),
                str(total),
                text_progress,
                "{}/{}".format(review_done, total),
                ("{}/{}".format(output_done, output_total) if output_total
                 else ("{} verified".format(output_done) if output_done else "—")),
                DISPLAY_STATUS.get(
                    str(_value(
                        row, "overall_status", "status",
                        default="NEW")).upper(),
                    str(_value(
                        row, "overall_status", "status",
                        default="New")).replace("_", " ").title()),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.UserRole, int(row["id"]))
                self.table.setItem(row_index, column, item)
            if int(row["id"]) == selected_id:
                self.table.selectRow(row_index)
        message = "{} document{} shown".format(
            len(rows), "" if len(rows) == 1 else "s")
        notice = getattr(self.coordinator, "last_notice", None)
        error = getattr(self.coordinator, "last_error", None)
        if error:
            message += " — Background error: {}".format(error)
        elif notice:
            message += " — {}".format(notice)
        self.status.setText(message)
        self._update_actions()

    def set_summary_filter(self, status_filter):
        self._active_filter = status_filter
        for _key, _caption, value in self.SUMMARY_KEYS:
            if value == status_filter:
                matching_key = next(
                    key for key, _caption2, value2 in self.SUMMARY_KEYS
                    if value2 == value)
                self.summary_buttons[matching_key].setChecked(True)
                break
        self.refresh()

    def selected_document_id(self):
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return item.data(Qt.UserRole) if item else None

    def selected_document(self):
        identifier = self.selected_document_id()
        return next((row for row in self._rows if int(row["id"]) == identifier), None)

    def _update_actions(self):
        row = self.selected_document()
        enabled = row is not None
        status = str(_value(row or {}, "overall_status", "status", default="")).upper()
        self.retry_action.setEnabled(
            enabled and (
                "ERROR" in status or "FAILED" in status
                or int(_value(row or {}, "ocr_failed", default=0)) > 0
            ))
        self.manifest_action.setEnabled(enabled and status in
                                        ("COMPLETE", "COMPLETED", "EXPORTED"))

    def run_primary_action(self):
        row = self.selected_document()
        if not row:
            return
        status = str(_value(
            row, "overall_status", "status", default="")).upper()
        if status in ("READY", "APPROVED", "READY_TO_GENERATE"):
            self.generate_selected()
        elif status in ("COMPLETE", "COMPLETED", "EXPORTED"):
            self.show_manifest()
        else:
            self.open_selected()

    def show_document_menu(self, position):
        index = self.table.indexAt(position)
        if index.isValid():
            self.table.selectRow(index.row())
        if self.selected_document() is not None:
            self.document_menu.exec(
                self.table.viewport().mapToGlobal(position))

    def open_selected(self):
        identifier = self.selected_document_id()
        if identifier is not None:
            self.document_open_requested.emit(identifier)

    def _coordinator_call(self, names, document_id):
        if self.coordinator is None:
            QMessageBox.information(
                self, "Background processing unavailable",
                "The background job coordinator is not running.")
            return
        for name in names:
            method = getattr(self.coordinator, name, None)
            if method:
                try:
                    method(document_id)
                    self.refresh()
                except Exception as exc:
                    QMessageBox.warning(self, "Action failed", str(exc))
                return
        QMessageBox.warning(self, "Action unavailable", "This coordinator does not support that action.")

    def retry_selected(self):
        identifier = self.selected_document_id()
        if identifier is not None:
            self._coordinator_call(("retry_failed", "retry_document", "retry_job"), identifier)

    def generate_selected(self):
        identifier = self.selected_document_id()
        if identifier is not None:
            self._coordinator_call(("queue_generation", "enqueue_generation",
                                    "generate_document"), identifier)

    def _confirm_bulk_generation(self, count):
        answer = QMessageBox.question(
            self, "Generate ready documents",
            "Queue output generation for {} ready document{}?".format(
                count, "" if count == 1 else "s"),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        return answer == QMessageBox.Yes

    def generate_all_ready(self):
        """Queue every currently ready source while isolating per-file errors."""
        ready = list(self._documents("Ready to generate"))
        if not ready:
            self.status.setText("No documents are ready to generate.")
            return
        if self.coordinator is None:
            self.status.setText("Background processing is unavailable.")
            return
        method = next((
            getattr(self.coordinator, name, None)
            for name in ("queue_generation", "enqueue_generation",
                         "generate_document")
            if getattr(self.coordinator, name, None)
        ), None)
        if method is None:
            self.status.setText("Generation is unavailable.")
            return
        if not self.bulk_confirm(len(ready)):
            return
        queued = 0
        failed = []
        for document in ready:
            try:
                method(int(document["id"]))
                queued += 1
            except Exception as exc:
                failed.append("{}: {}".format(
                    Path(_value(document, "filepath", default="Document")).name,
                    exc))
        self.refresh()
        message = "Queued {} document{} for generation.".format(
            queued, "" if queued == 1 else "s")
        if failed:
            message += " {} failed: {}".format(len(failed), "; ".join(failed))
        self.status.setText(message)

    def show_manifest(self):
        document = self.selected_document()
        if not document:
            return
        identifier = int(document["id"])
        if hasattr(self.database, "get_completion_manifest"):
            manifest = self.database.get_completion_manifest(identifier)
            if isinstance(manifest, dict):
                outputs = manifest.get("outputs", [])
                document = manifest.get("document", document)
            else:
                outputs = manifest
        elif hasattr(self.database, "list_output_files"):
            outputs = self.database.list_output_files(identifier)
        else:
            outputs = []
        CompletionManifestDialog(document, outputs, self).exec()
