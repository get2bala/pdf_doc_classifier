"""PySide6 desktop UI for the offline PDF classifier."""

import sys
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QSplitter,
    QStackedWidget, QVBoxLayout, QWidget,
)

from classifier import AnalysisPipeline, EmbeddedPdfTextEngine
from database import DatabaseManager
from pdf_engine import PdfExporter, page_count, render_page
from dialogs import DatabaseViewerDialog, SettingsDialog, app_settings
from dashboard import DashboardWidget


DEFAULT_CATEGORIES = [
    ("Aadhaar Card", "aadhaar,unique identification authority", "COMBINE"),
    ("PAN Card", "permanent account number,income tax department", "COMBINE"),
    ("Offer Letter", "offer of employment,date of joining", "COMBINE"),
    ("Salary Slip", "gross pay,net pay,basic salary", "SEPARATE"),
]


class WorkerSignals(QObject):
    finished = Signal(object)
    failed = Signal(str)


class Task(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.function = function
        self.signals = WorkerSignals()

    def run(self):
        try:
            self.signals.finished.emit(self.function())
        except Exception as exc:
            self.signals.failed.emit(str(exc))


class ClassifierWindow(QMainWindow):
    def __init__(self, db_path=None, coordinator=None, database=None):
        super().__init__()
        data_dir = Path.home() / ".pdf_doc_classifier"
        self._owns_database = database is None
        self.db = database or DatabaseManager(str(db_path or data_dir / "classifier.sqlite3"))
        self.coordinator = coordinator
        self.settings = app_settings(self.db)
        self._seed_categories()
        self.document_id = None
        self.thread_pool = QThreadPool.globalInstance()
        self.setWindowTitle("PDF Document Classifier — Local Offline")
        self.resize(1250, 820)
        self._build_ui()
        self._reload_documents()

    def _seed_categories(self):
        if not self.db.list_categories():
            for name, keywords, policy in DEFAULT_CATEGORIES:
                pattern = "{employee_id}_{category}_{instance}.pdf" if policy == "SEPARATE" else "{employee_id}_{category}.pdf"
                self.db.add_category(name, keywords, policy, pattern)

    def _build_ui(self):
        application_menu = self.menuBar().addMenu("Application")
        self.configuration_action = application_menu.addAction("Configuration…")
        self.database_action = application_menu.addAction("View database…")
        self.delete_document_action = application_menu.addAction("Remove selected document…")
        workspace = QWidget()
        layout = QVBoxLayout(workspace)
        toolbar = QHBoxLayout()
        self.dashboard_button = QPushButton("← Dashboard")
        self.import_button = QPushButton("Import PDF")
        self.employee_input = QLineEdit()
        self.employee_input.setPlaceholderText("Employee ID")
        self.document_list = QListWidget()
        self.document_list.setMaximumHeight(90)
        toolbar.addWidget(self.dashboard_button)
        toolbar.addWidget(self.import_button)
        toolbar.addWidget(self.employee_input)
        toolbar.addWidget(QLabel("Imported files:"))
        toolbar.addWidget(self.document_list, 1)
        layout.addLayout(toolbar)

        splitter = QSplitter(Qt.Horizontal)
        self.pages = QListWidget()
        self.pages.setSelectionMode(QListWidget.ExtendedSelection)
        splitter.addWidget(self.pages)

        center = QWidget()
        center_layout = QVBoxLayout(center)
        self.header = QLabel("Import or select a PDF")
        self.header.setAlignment(Qt.AlignCenter)
        self.preview = QLabel("PDF preview")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(420, 540)
        self.preview.setStyleSheet("background:#ddd; border:1px solid #aaa")
        center_layout.addWidget(self.header)
        center_layout.addWidget(self.preview, 1)
        splitter.addWidget(center)

        actions = QWidget()
        action_layout = QVBoxLayout(actions)
        self.selection_label = QLabel("No page selected")
        self.category_filter = QLineEdit()
        self.category_filter.setPlaceholderText("Filter categories")
        self.categories = QListWidget()
        self.assign_button = QPushButton("Assign selected pages (Enter)")
        self.exclude_button = QPushButton("Exclude selected pages")
        self.reset_button = QPushButton("Reset selected pages")
        self.ocr_button = QPushButton("Extract text from current page")
        self.ocr_all_button = QPushButton("Extract text from all pages")
        self.suggestion = QLabel("No text-based suggestion")
        self.suggestion.setWordWrap(True)
        self.accept_button = QPushButton("Accept suggestion")
        self.reject_button = QPushButton("Reject suggestion")
        self.export_button = QPushButton("Validate & Generate PDFs")
        self.export_button.setStyleSheet("padding:10px;font-weight:bold")
        for widget in (self.selection_label, self.category_filter, self.categories,
                       self.assign_button, self.exclude_button, self.reset_button,
                       self.ocr_button, self.ocr_all_button, self.suggestion,
                       self.accept_button, self.reject_button):
            action_layout.addWidget(widget)
        action_layout.addStretch()
        action_layout.addWidget(self.export_button)
        splitter.addWidget(actions)
        splitter.setSizes([260, 700, 290])
        layout.addWidget(splitter, 1)
        self.dashboard = DashboardWidget(self.db, self.coordinator)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.dashboard)
        self.stack.addWidget(workspace)
        self.setCentralWidget(self.stack)

        self.import_button.clicked.connect(self.import_pdf)
        self.dashboard_button.clicked.connect(self.show_dashboard)
        self.dashboard.document_open_requested.connect(self.open_document_for_review)
        self.document_list.currentItemChanged.connect(self.open_document)
        self.pages.currentItemChanged.connect(self.show_page)
        self.pages.itemSelectionChanged.connect(self.update_selection)
        self.category_filter.textChanged.connect(self.reload_categories)
        self.category_filter.returnPressed.connect(self.assign_selected)
        self.assign_button.clicked.connect(self.assign_selected)
        self.exclude_button.clicked.connect(self.exclude_selected)
        self.reset_button.clicked.connect(self.reset_selected)
        self.ocr_button.clicked.connect(self.ocr_current)
        self.ocr_all_button.clicked.connect(self.ocr_all)
        self.accept_button.clicked.connect(self.accept_suggestion)
        self.reject_button.clicked.connect(self.reject_suggestion)
        self.export_button.clicked.connect(self.export_document)
        self.configuration_action.triggered.connect(self.open_configuration)
        self.database_action.triggered.connect(self.open_database_viewer)
        self.delete_document_action.triggered.connect(self.delete_document)
        self.reload_categories()

    def show_dashboard(self):
        self.dashboard.refresh()
        self.stack.setCurrentWidget(self.dashboard)

    def open_document_for_review(self, document_id):
        self.document_id = int(document_id)
        self._reload_documents()
        for row in range(self.document_list.count()):
            item = self.document_list.item(row)
            if item.data(Qt.UserRole) == self.document_id:
                self.document_list.setCurrentItem(item)
                break
        self.reload_pages()
        if self.pages.count():
            self.pages.setCurrentRow(0)
        self.stack.setCurrentIndex(1)

    def open_configuration(self):
        dialog = SettingsDialog(self.db, self.settings, self)
        if dialog.exec():
            self.reload_categories()
            if self.coordinator and hasattr(self.coordinator, "reload_settings"):
                self.coordinator.reload_settings()
            self.dashboard.refresh()

    def open_database_viewer(self):
        DatabaseViewerDialog(self.db, self).exec()

    def _reload_documents(self):
        current = self.document_id
        self.document_list.clear()
        for doc in self.db.list_source_documents():
            item = QListWidgetItem("{} — {}".format(doc["employee_id"], Path(doc["filepath"]).name))
            item.setData(Qt.UserRole, doc["id"])
            self.document_list.addItem(item)
            if doc["id"] == current:
                self.document_list.setCurrentItem(item)

    def reload_categories(self):
        needle = self.category_filter.text().lower().strip()
        self.categories.clear()
        for category in self.db.list_categories():
            if needle in category["name"].lower():
                item = QListWidgetItem(category["name"])
                item.setData(Qt.UserRole, category["id"])
                self.categories.addItem(item)
        if self.categories.count():
            self.categories.setCurrentRow(0)

    def import_pdf(self):
        start = self.settings.value("paths/input", "", str)
        path, _ = QFileDialog.getOpenFileName(self, "Import PDF", start, "PDF files (*.pdf)")
        if not path:
            return
        employee = self.employee_input.text().strip() or Path(path).stem
        try:
            if self.coordinator and hasattr(self.coordinator, "ingest_file"):
                doc_id = self.coordinator.ingest_file(path, employee_id=employee)
            else:
                doc_id = self.db.add_source_document(path, employee, page_count(path))
        except Exception as exc:
            QMessageBox.critical(self, "Import failed", str(exc))
            return
        self.document_id = doc_id
        self._reload_documents()

    def open_document(self, item):
        if not item:
            return
        self.document_id = item.data(Qt.UserRole)
        self.reload_pages()
        if self.pages.count():
            self.pages.setCurrentRow(0)

    def reload_pages(self):
        selected = {item.data(Qt.UserRole) for item in self.pages.selectedItems()}
        self.pages.clear()
        for assignment in self.db.get_page_assignments(self.document_id):
            detail = assignment["category_name"] or assignment["status"].replace("_", " ").title()
            item = QListWidgetItem("Page {:03d}  [{}]".format(assignment["page_number"], detail))
            item.setData(Qt.UserRole, assignment["page_number"])
            self.pages.addItem(item)
            item.setSelected(assignment["page_number"] in selected)

    def selected_pages(self):
        return [item.data(Qt.UserRole) for item in self.pages.selectedItems()]

    def update_selection(self):
        pages = self.selected_pages()
        self.selection_label.setText("Selected: {}".format(", ".join(map(str, pages)) if pages else "none"))

    def show_page(self, item):
        if not item or not self.document_id:
            return
        page = item.data(Qt.UserRole)
        document = self.db.get_source_document(self.document_id)
        self.header.setText("{} — Page {} / {}".format(Path(document["filepath"]).name, page, document["page_count"]))
        try:
            image = render_page(document["filepath"], page, 1.5).convert("RGB")
            qimage = QImage(image.tobytes(), image.width, image.height, image.width * 3, QImage.Format_RGB888).copy()
            pixmap = QPixmap.fromImage(qimage).scaled(self.preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.preview.setPixmap(pixmap)
        except Exception as exc:
            self.preview.setText(str(exc))
        analysis = self.db.get_analysis(self.document_id, page)
        self._show_analysis(analysis)

    def _show_analysis(self, analysis):
        if not analysis:
            self.suggestion.setText("No text-based suggestion")
        elif analysis["status"] == "FAILED":
            self.suggestion.setText(
                "Text extraction failed: {}".format(analysis["error_message"]))
        else:
            excerpt = " ".join((analysis.get("ocr_text") or "").split())
            if len(excerpt) > 300:
                excerpt = excerpt[:297] + "..."
            self.suggestion.setText(
                "Suggested: {}\nScore: {:.0f}/100\n{}\n\nExtracted text: {}".format(
                    analysis["suggested_category"] or "No match",
                    analysis["score"] or 0,
                    analysis["explanation"] or "",
                    excerpt or "[No embedded text found]"))

    def assign_selected(self):
        pages, category = self.selected_pages(), self.categories.currentItem()
        if not self.document_id or not pages or not category:
            return
        self.db.assign_pages(self.document_id, pages, category.data(Qt.UserRole))
        self.reload_pages()
        self._advance_after(pages)

    def exclude_selected(self):
        pages = self.selected_pages()
        if pages:
            self.db.set_page_status(self.document_id, pages, "EXCLUDED")
            self.reload_pages()
            self._advance_after(pages)

    def reset_selected(self):
        pages = self.selected_pages()
        if pages:
            self.db.reset_pages(self.document_id, pages)
            self.reload_pages()

    def delete_document(self):
        if not self.document_id:
            return
        document = self.db.get_source_document(self.document_id)
        answer = QMessageBox.question(
            self, "Remove imported document",
            "Remove {} and all of its classifications from the database?\n\n"
            "The source PDF will not be deleted.".format(Path(document["filepath"]).name))
        if answer != QMessageBox.Yes:
            return
        self.db.delete_source_document(self.document_id)
        self.document_id = None
        self.pages.clear()
        self.preview.clear()
        self.preview.setText("PDF preview")
        self.header.setText("Import or select a PDF")
        self._reload_documents()

    def _advance_after(self, pages):
        next_index = min(max(pages), self.pages.count() - 1)
        self.pages.setCurrentRow(next_index)
        self.category_filter.clear()
        self.category_filter.setFocus()

    def ocr_current(self):
        item = self.pages.currentItem()
        if not item:
            return
        page = item.data(Qt.UserRole)
        document_id = self.document_id
        self.ocr_button.setEnabled(False)
        task = Task(lambda: AnalysisPipeline(
            self.db, EmbeddedPdfTextEngine()).analyze_page(document_id, page))
        task.signals.finished.connect(lambda result: (self.ocr_button.setEnabled(True), self._show_analysis(result)))
        task.signals.failed.connect(lambda error: (self.ocr_button.setEnabled(True), QMessageBox.warning(self, "OCR failed", error)))
        self.thread_pool.start(task)

    def ocr_all(self):
        if not self.document_id:
            return
        document_id = self.document_id
        self.ocr_all_button.setEnabled(False)
        self.statusBar().showMessage("Extracting PDF text for all pages…")

        def analyze():
            results = list(AnalysisPipeline(
                self.db, EmbeddedPdfTextEngine()).analyze_document(document_id))
            failures = sum(1 for item in results if item and item["status"] == "FAILED")
            return len(results), failures

        task = Task(analyze)
        task.signals.finished.connect(self._ocr_all_finished)
        task.signals.failed.connect(self._ocr_all_failed)
        self.thread_pool.start(task)

    def _ocr_all_finished(self, result):
        self.ocr_all_button.setEnabled(True)
        total, failures = result
        self.statusBar().showMessage(
            "Text extraction complete: {} pages, {} failures".format(
                total, failures), 10000)
        if self.pages.currentItem():
            self.show_page(self.pages.currentItem())

    def _ocr_all_failed(self, error):
        self.ocr_all_button.setEnabled(True)
        self.statusBar().showMessage("Text extraction stopped", 5000)
        QMessageBox.warning(self, "Text extraction failed", error)

    def accept_suggestion(self):
        pages = self.selected_pages()
        if not pages and self.pages.currentItem():
            pages = [self.pages.currentItem().data(Qt.UserRole)]
        try:
            AnalysisPipeline(
                self.db, EmbeddedPdfTextEngine()).accept(self.document_id, pages)
            self.reload_pages()
        except Exception as exc:
            QMessageBox.warning(self, "Cannot accept", str(exc))

    def reject_suggestion(self):
        pages = self.selected_pages()
        if not pages and self.pages.currentItem():
            pages = [self.pages.currentItem().data(Qt.UserRole)]
        if pages:
            self.db.mark_analysis(self.document_id, pages, "REJECTED")
            if self.pages.currentItem():
                self.show_page(self.pages.currentItem())

    def export_document(self):
        if not self.document_id:
            return
        start = self.settings.value("paths/output", "", str)
        if self.coordinator:
            directory = self.coordinator.output
            if not directory:
                QMessageBox.warning(
                    self, "Output folder required",
                    "Configure an output folder before generating.")
                return
        else:
            directory = QFileDialog.getExistingDirectory(
                self, "Choose output folder", start)
        if not directory:
            return
        try:
            plan = PdfExporter(self.db).build_plan(self.document_id, directory)
            summary = "\n".join("{}  (pages {})".format(
                Path(item["path"]).name, ", ".join(map(str, item["pages"]))) for item in plan)
            if QMessageBox.question(self, "Confirm export", "Generate these files?\n\n" + summary) != QMessageBox.Yes:
                return
            if self.coordinator:
                self.coordinator.queue_generation(self.document_id)
                QMessageBox.information(
                    self, "Generation queued",
                    "PDF generation is running in the background. Progress and "
                    "verified outputs appear on the dashboard.")
                self.show_dashboard()
            else:
                files = PdfExporter(self.db).export(self.document_id, directory)
                QMessageBox.information(
                    self, "Export complete", "Created:\n" + "\n".join(files))
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))

    def closeEvent(self, event):
        if self.coordinator and hasattr(self.coordinator, "stop"):
            self.coordinator.stop()
        self.thread_pool.waitForDone(3000)
        if self._owns_database:
            self.db.close()
        super().closeEvent(event)


ClassifierMockupUI = ClassifierWindow


def main():
    from workflow import WorkflowCoordinator

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    data_dir = Path.home() / ".pdf_doc_classifier"
    database = DatabaseManager(str(data_dir / "classifier.sqlite3"))
    settings = app_settings(database)
    analyzer = AnalysisPipeline(database, EmbeddedPdfTextEngine())
    coordinator = WorkflowCoordinator.from_settings(
        database, analyzer=analyzer, exporter=PdfExporter(database))
    window = ClassifierWindow(database=database, coordinator=coordinator)
    coordinator.start()
    window.show()
    exit_code = app.exec()
    coordinator.stop()
    database.close()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
