"""Configuration and safe data-inspection dialogs for the desktop app."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout,
    QWidget,
)


ALLOWED_TABLES = (
    "application_settings", "categories", "source_documents", "document_groups",
    "page_assignments", "page_analysis", "background_jobs", "generation_runs",
    "output_files",
)
REQUIRED_PATTERN_FIELDS = ("{employee_id}", "{category}")


class DatabaseSettings:
    """Small QSettings-compatible facade backed by the application database."""

    def __init__(self, database):
        self.database = database

    def value(self, key, default=None, value_type=None):
        value = self.database.get_setting(key, default)
        if value_type is not None and value is not None:
            try:
                return value_type(value)
            except (TypeError, ValueError):
                return default
        return value

    def setValue(self, key, value):
        self.database.set_setting(key, value)

    def sync(self):
        return None


def app_settings(database):
    return DatabaseSettings(database)


class CategoryTable(QTableWidget):
    HEADERS = ("ID", "Name", "Keywords (comma separated)", "Output policy", "Filename pattern")

    def __init__(self, parent=None):
        super().__init__(0, len(self.HEADERS), parent)
        self.setHorizontalHeaderLabels(self.HEADERS)
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)

    def add_category(self, category=None):
        category = category or {}
        row = self.rowCount()
        self.insertRow(row)
        identifier = QTableWidgetItem(str(category.get("id", "")))
        identifier.setFlags(identifier.flags() & ~Qt.ItemIsEditable)
        self.setItem(row, 0, identifier)
        self.setItem(row, 1, QTableWidgetItem(category.get("name", "")))
        self.setItem(row, 2, QTableWidgetItem(category.get("keywords", "")))
        policy = QComboBox()
        policy.addItems(("COMBINE", "SEPARATE"))
        policy.setCurrentText(category.get("output_policy", "COMBINE"))
        self.setCellWidget(row, 3, policy)
        self.setItem(row, 4, QTableWidgetItem(category.get(
            "filename_pattern", "{employee_id}_{category}.pdf")))

    def values(self):
        result = []
        for row in range(self.rowCount()):
            result.append({
                "id": int(self.item(row, 0).text()) if self.item(row, 0).text() else None,
                "name": self.item(row, 1).text().strip(),
                "keywords": self.item(row, 2).text().strip(),
                "output_policy": self.cellWidget(row, 3).currentText(),
                "filename_pattern": self.item(row, 4).text().strip(),
            })
        return result


class SettingsDialog(QDialog):
    """Edit application defaults and category rules in one validated workflow."""

    def __init__(self, database, settings=None, parent=None):
        super().__init__(parent)
        self.database = database
        self.settings = settings or app_settings(database)
        self.setWindowTitle("Application configuration")
        self.resize(900, 560)

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        general = QWidget()
        form = QFormLayout(general)
        self.input_directory = QLineEdit(self.settings.value("paths/input", "", str))
        self.output_directory = QLineEdit(self.settings.value("paths/output", "", str))
        self.completed_directory = QLineEdit(self.settings.value("paths/completed", "", str))
        self.error_directory = QLineEdit(self.settings.value("paths/error", "", str))
        self.auto_extraction = QCheckBox(
            "Extract embedded PDF text when a stable PDF is discovered")
        self.auto_extraction.setChecked(bool(self.settings.value(
            "processing/automatic_text_extraction",
            self.settings.value("ocr/automatic", True))))
        # Compatibility attribute for older tests/extensions.
        self.auto_ocr = self.auto_extraction
        self.automation_mode = QComboBox()
        self.automation_mode.addItem("Suggestions only", "MANUAL")
        self.automation_mode.addItem("Preassign for review", "PREASSIGN")
        self.automation_mode.addItem("Automatic assignment", "AUTOMATIC")
        mode = self.settings.value("classification/automation_mode", "PREASSIGN", str)
        index = self.automation_mode.findData(mode)
        self.automation_mode.setCurrentIndex(max(index, 0))
        self.minimum_score = QSpinBox()
        self.minimum_score.setRange(0, 100)
        self.minimum_score.setValue(int(self.settings.value("classification/minimum_score", 80)))
        self.minimum_matches = QSpinBox()
        self.minimum_matches.setRange(1, 99)
        self.minimum_matches.setValue(int(
            self.settings.value("classification/minimum_matches", 2)))
        self.appearance = QComboBox()
        self.appearance.addItem("Use system setting", "SYSTEM")
        self.appearance.addItem("Light", "LIGHT")
        self.appearance.addItem("Dark", "DARK")
        appearance = str(self.settings.value(
            "appearance/mode", "SYSTEM", str)).upper()
        self.appearance.setCurrentIndex(max(
            self.appearance.findData(appearance), 0))
        form.addRow("Inbox folder", self._path_editor(self.input_directory))
        form.addRow("Output folder", self._path_editor(self.output_directory))
        form.addRow("Completed folder", self._path_editor(self.completed_directory))
        form.addRow("Error folder", self._path_editor(self.error_directory))
        form.addRow("Text source", QLabel("Existing searchable PDF text layer"))
        form.addRow("Automatic extraction", self.auto_extraction)
        form.addRow("Assignment mode", self.automation_mode)
        form.addRow("Minimum rule score", self.minimum_score)
        form.addRow("Minimum keyword matches", self.minimum_matches)
        form.addRow("Appearance", self.appearance)
        form.addRow("", QLabel(
            "Folder and automation changes are picked up by the background coordinator."))
        tabs.addTab(general, "General")

        categories = QWidget()
        category_layout = QVBoxLayout(categories)
        self.category_table = CategoryTable()
        for category in self.database.list_categories():
            self.category_table.add_category(category)
        buttons = QHBoxLayout()
        add = QPushButton("Add category")
        remove = QPushButton("Remove selected")
        add.clicked.connect(self.category_table.add_category)
        remove.clicked.connect(self._remove_selected)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch()
        category_layout.addWidget(QLabel(
            "Patterns must include {employee_id} and {category}; use {instance} for SEPARATE output."))
        category_layout.addWidget(self.category_table)
        category_layout.addLayout(buttons)
        tabs.addTab(categories, "Categories")
        layout.addWidget(tabs)

        self.button_box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.button_box.accepted.connect(self.save)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

    def _path_editor(self, field):
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        browse = QPushButton("Browse…")
        browse.clicked.connect(lambda: self._browse(field))
        layout.addWidget(field)
        layout.addWidget(browse)
        return widget

    def _browse(self, field):
        directory = QFileDialog.getExistingDirectory(self, "Choose folder", field.text())
        if directory:
            field.setText(directory)

    def _remove_selected(self):
        rows = sorted({index.row() for index in self.category_table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.category_table.removeRow(row)

    def validation_error(self):
        categories = self.category_table.values()
        if not categories:
            return "Configure at least one category."
        names = [item["name"].casefold() for item in categories]
        if any(not name for name in names):
            return "Every category needs a name."
        if len(names) != len(set(names)):
            return "Category names must be unique."
        for item in categories:
            pattern = item["filename_pattern"]
            if not pattern.lower().endswith(".pdf") or any(field not in pattern for field in REQUIRED_PATTERN_FIELDS):
                return "Each filename pattern must end in .pdf and include {employee_id} and {category}."
            if item["output_policy"] == "SEPARATE" and "{instance}" not in pattern:
                return "SEPARATE category patterns must include {instance}."
            if "/" in pattern or "\\" in pattern or ".." in pattern:
                return "Filename patterns cannot contain folders or '..'."
        return None

    def save(self):
        error = self.validation_error()
        if error:
            QMessageBox.warning(self, "Invalid configuration", error)
            return
        values = self.category_table.values()
        try:
            self.database.replace_categories(values)
        except Exception as exc:
            QMessageBox.critical(self, "Could not save configuration", str(exc))
            return
        self.settings.setValue("paths/input", self.input_directory.text().strip())
        self.settings.setValue("paths/output", self.output_directory.text().strip())
        self.settings.setValue("paths/completed", self.completed_directory.text().strip())
        self.settings.setValue("paths/error", self.error_directory.text().strip())
        self.settings.setValue(
            "processing/automatic_text_extraction",
            self.auto_extraction.isChecked())
        self.settings.setValue(
            "classification/automation_mode", self.automation_mode.currentData())
        self.settings.setValue("classification/minimum_score", self.minimum_score.value())
        self.settings.setValue("classification/minimum_matches", self.minimum_matches.value())
        self.settings.setValue("appearance/mode", self.appearance.currentData())
        self.settings.sync()
        self.accept()


class DatabaseViewerDialog(QDialog):
    """Read-only browser constrained to application-owned tables."""

    def __init__(self, database, parent=None):
        super().__init__(parent)
        self.database = database
        self.setWindowTitle("Database viewer — read only")
        self.resize(1000, 600)
        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.table_selector = QComboBox()
        self.table_selector.addItems(self.database.list_database_tables())
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        bar.addWidget(QLabel("Table"))
        bar.addWidget(self.table_selector)
        bar.addStretch()
        bar.addWidget(refresh)
        self.table = QTableWidget()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.status = QLabel()
        layout.addLayout(bar)
        layout.addWidget(self.table)
        layout.addWidget(self.status)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)
        self.table_selector.currentTextChanged.connect(self.refresh)
        self.refresh()

    def refresh(self):
        name = self.table_selector.currentText()
        if name not in self.database.list_database_tables():
            return
        result = self.database.query_table(name, limit=1000)
        columns, rows = result["columns"], result["rows"]
        self.table.clear()
        self.table.setColumnCount(len(columns))
        self.table.setHorizontalHeaderLabels(columns)
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column_index, column in enumerate(columns):
                value = row[column]
                self.table.setItem(row_index, column_index, QTableWidgetItem("" if value is None else str(value)))
        self.status.setText("{} row{} shown (maximum 1,000)".format(len(rows), "" if len(rows) == 1 else "s"))
