"""Offline, searchable help and application information."""

from pathlib import Path
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTextBrowser, QVBoxLayout,
)


ASSET_ROOT = Path(__file__).resolve().parent / "assets"


def asset_path(filename):
    candidates = (
        ASSET_ROOT / filename,
        Path(getattr(sys, "_MEIPASS", "")) / "assets" / filename,
        Path(sys.prefix) / "share" / "pdf-document-classifier" /
        "assets" / filename,
    )
    return next((path for path in candidates if path.is_file()), candidates[0])


class _HelpDialog(QDialog):
    def __init__(self, title, content, searchable=True, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 640)
        layout = QVBoxLayout(self)
        if searchable:
            search_row = QHBoxLayout()
            self.search = QLineEdit()
            self.search.setPlaceholderText("Search help")
            self.search.setClearButtonEnabled(True)
            next_button = QPushButton("Find Next")
            search_row.addWidget(self.search, 1)
            search_row.addWidget(next_button)
            layout.addLayout(search_row)
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(False)
        self.browser.document().setDefaultStyleSheet(
            "body { line-height: 145%; }"
            "p, li { line-height: 145%; margin-bottom: 10px; }"
            "h1 { margin-top: 4px; margin-bottom: 18px; }"
            "h2 { margin-top: 22px; margin-bottom: 10px; }")
        self.browser.setHtml(content)
        layout.addWidget(self.browser, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if searchable:
            self.search.returnPressed.connect(self._find)
            next_button.clicked.connect(self._find)

    def _find(self):
        needle = self.search.text().strip()
        if not needle:
            return
        if not self.browser.find(needle):
            cursor = self.browser.textCursor()
            cursor.movePosition(cursor.Start)
            self.browser.setTextCursor(cursor)
            self.browser.find(needle)


class UserGuideDialog(_HelpDialog):
    def __init__(self, parent=None):
        super().__init__(
            "PDF Document Classifier — User Guide",
            """
            <h1>User Guide</h1>
            <h2>The three-step workflow</h2>
            <ol>
              <li><b>Dashboard:</b> monitor automatic document-text extraction,
              review progress, generation, and errors.</li>
              <li><b>Review Workspace:</b> review suggestions across documents,
              search extracted document text, and resolve unassigned pages.</li>
              <li><b>Generate files:</b> create and verify output PDFs after
              every page is assigned or excluded.</li>
            </ol>
            <h2>Review decisions</h2>
            <p><b>Approve / Confirm suggestion</b> accepts the suggested
            category shown on each selected card.</p>
            <p><b>Assign / Change category</b> places selected pages in a
            different category that you choose.</p>
            <p><b>Defer</b> was the former name for skipping work until later.
            The current interface simply leaves unselected pages unchanged, so
            they remain in their queue and cannot disappear.</p>
            <p><b>Open Source</b>, now called <b>Open in Document</b>, opens the
            original source PDF at that page for detailed inspection. It does
            not alter or delete the PDF.</p>
            <h2>Review views</h2>
            <p><b>Suggestions</b> contains unconfirmed matches.
            <b>Needs Review</b> contains preassigned pages requiring approval.
            <b>Unassigned</b> contains pages without a usable match.
            <b>Extraction Issues</b> contains pages whose searchable text could
            not be read.</p>
            <h2>Selection and search</h2>
            <p>Search uses extracted document text. Use Arrow keys to move,
            Space to toggle, Shift+Arrow for a range, Command+A on macOS or
            Ctrl+A on Windows to select all visible cards, Escape to clear, and
            Return to open the focused page.</p>
            <h2>Privacy and troubleshooting</h2>
            <p>Processing is offline. PDFs, extracted text, configuration, and
            database records stay local on this computer and are never sent to
            a service. Image-only scanned PDFs need a searchable text layer
            before they can be matched automatically.</p>
            """,
            parent=parent,
        )


class KeyboardShortcutsDialog(_HelpDialog):
    def __init__(self, parent=None):
        super().__init__(
            "Keyboard Shortcuts",
            """
            <h1>Keyboard Shortcuts</h1>
            <p><b>Ctrl/Command+1</b> — Dashboard<br>
            <b>Ctrl/Command+2</b> — Review Workspace<br>
            <b>Arrow keys</b> — Move card focus<br>
            <b>Space</b> — Toggle selection<br>
            <b>Shift + Arrow</b> — Extend a selection range<br>
            <b>Ctrl/Command+A</b> — Select all visible actionable cards<br>
            <b>Escape</b> — Clear selection or search<br>
            <b>Return</b> — Open focused page in Document view</p>
            """,
            searchable=False,
            parent=parent,
        )


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About PDF Document Classifier")
        self.resize(460, 390)
        layout = QVBoxLayout(self)
        icon = QLabel()
        pixmap = QPixmap(str(asset_path("pdf-classifier-icon-256.png")))
        icon.setPixmap(pixmap.scaled(
            112, 112, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        icon.setAlignment(Qt.AlignCenter)
        layout.addWidget(icon)
        text = QLabel(
            "<h2>PDF Document Classifier</h2>"
            "<p>Version 1.0.0</p>"
            "<p>An offline, local desktop workflow for reviewing and "
            "classifying searchable PDFs.</p>"
            "<p>No document content is sent from this computer.</p>")
        text.setAlignment(Qt.AlignCenter)
        text.setWordWrap(True)
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
