"""Queue-first dashboard widgets for the persistent document workflow."""

from pathlib import Path

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)


STATUS_FILTERS = (
    "All", "Needs attention", "OCR running", "Needs review",
    "Ready to generate", "Completed", "Errors",
)
FILTER_VALUES = {
    "All": "ALL", "Needs attention": "NEEDS_ATTENTION",
    "OCR running": "OCR_RUNNING", "Needs review": "NEEDS_REVIEW",
    "Ready to generate": "READY_TO_GENERATE", "Completed": "COMPLETED",
    "Errors": "ERRORS",
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
        ("new", "New"), ("ocr_running", "Text extraction"),
        ("needs_review", "Needs review"), ("ready_to_generate", "Ready to generate"),
        ("completed_today", "Completed today"), ("errors", "Errors"),
    )

    def __init__(self, database, coordinator=None, parent=None):
        super().__init__(parent)
        self.database = database
        self.coordinator = coordinator
        self._rows = []
        layout = QVBoxLayout(self)
        title = QHBoxLayout()
        heading = QLabel("<h2>Document processing dashboard</h2>")
        title.addWidget(heading)
        title.addStretch()
        self.refresh_button = QPushButton("Refresh")
        title.addWidget(self.refresh_button)
        layout.addLayout(title)

        cards = QGridLayout()
        self.summary_labels = {}
        for index, (key, caption) in enumerate(self.SUMMARY_KEYS):
            card = QLabel()
            card.setAlignment(Qt.AlignCenter)
            card.setMinimumHeight(66)
            card.setStyleSheet(
                "QLabel { background:#f4f6f8; border:1px solid #d5d9dd;"
                " border-radius:6px; padding:8px; }")
            self.summary_labels[key] = card
            cards.addWidget(card, 0, index)
            card.setText("<b>0</b><br>{}".format(caption))
        layout.addLayout(cards)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Show"))
        self.status_filter = QComboBox()
        self.status_filter.addItems(STATUS_FILTERS)
        bar.addWidget(self.status_filter)
        bar.addStretch()
        self.open_button = QPushButton("Open for review")
        self.retry_button = QPushButton("Retry failed job")
        self.generate_button = QPushButton("Queue generation")
        self.manifest_button = QPushButton("View completion manifest")
        for button in (self.open_button, self.retry_button, self.generate_button,
                       self.manifest_button):
            bar.addWidget(button)
        layout.addLayout(bar)

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)
        self.status = QLabel()
        layout.addWidget(self.status)

        self.refresh_button.clicked.connect(self.refresh)
        self.status_filter.currentTextChanged.connect(self.refresh)
        self.open_button.clicked.connect(self.open_selected)
        self.retry_button.clicked.connect(self.retry_selected)
        self.generate_button.clicked.connect(self.generate_selected)
        self.manifest_button.clicked.connect(self.show_manifest)
        self.table.doubleClicked.connect(lambda _index: self.open_selected())
        self.table.itemSelectionChanged.connect(self._update_actions)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(2000)
        self.refresh_timer.timeout.connect(self.refresh)
        self.refresh_timer.start()
        self.refresh()

    def _summary(self):
        if hasattr(self.database, "get_dashboard_summary"):
            result = self.database.get_dashboard_summary()
            return {str(key).lower(): value for key, value in result.items()}
        rows = self._documents("All")
        statuses = [str(_value(row, "overall_status", "status", default="")).upper()
                    for row in rows]
        return {
            "new": sum(status in ("NEW", "IN_PROGRESS") for status in statuses),
            "ocr_running": sum(status == "OCR_RUNNING" for status in statuses),
            "needs_review": sum(status == "NEEDS_REVIEW" for status in statuses),
            "ready_to_generate": sum(status == "READY_TO_GENERATE" for status in statuses),
            "completed_today": sum(status in ("COMPLETED", "EXPORTED") for status in statuses),
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
        for key, caption in self.SUMMARY_KEYS:
            value = summary.get(key, summary.get(key.upper(), 0))
            if key == "completed_today" and not value:
                value = summary.get("COMPLETED", 0)
            self.summary_labels[key].setText(
                "<b style='font-size:20px'>{}</b><br>{}".format(value, caption))
        selected_id = self.selected_document_id()
        requested = self.status_filter.currentText()
        rows = list(self._documents(requested))
        if requested != "All" and not hasattr(self.database, "list_dashboard_documents"):
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
            values = (
                Path(_value(row, "filepath", "filename", default="")).name,
                str(_value(row, "employee_id", default="—")),
                str(total),
                "{}/{}".format(ocr_done, total),
                "{}/{}".format(review_done, total),
                ("{}/{}".format(output_done, output_total) if output_total
                 else ("{} verified".format(output_done) if output_done else "—")),
                str(_value(row, "overall_status", "status", default="New")).replace("_", " ").title(),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.UserRole, int(row["id"]))
                self.table.setItem(row_index, column, item)
            if int(row["id"]) == selected_id:
                self.table.selectRow(row_index)
        self.status.setText("{} document{} shown".format(
            len(rows), "" if len(rows) == 1 else "s"))
        self._update_actions()

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
        self.open_button.setEnabled(enabled)
        status = str(_value(row or {}, "overall_status", "status", default="")).upper()
        self.retry_button.setEnabled(enabled and ("ERROR" in status or "FAILED" in status))
        self.generate_button.setEnabled(enabled and status in
                                        ("READY", "APPROVED", "READY_TO_GENERATE"))
        self.manifest_button.setEnabled(enabled and status in
                                        ("COMPLETE", "COMPLETED", "EXPORTED"))

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
