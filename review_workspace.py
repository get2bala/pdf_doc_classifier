"""Cross-document review in small, predictable batches.

The workspace deliberately shows at most fifty proposals.  This keeps the UI
responsive and gives operators a clear loop: scan, deselect exceptions, commit,
and receive the next batch.
"""

from pathlib import Path

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QAction, QFont, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


REVIEW_PRESETS = (
    "Strong Suggestions",
    "Needs Review",
    "Unassigned",
    "Extraction Failures",
)
BATCH_SIZE = 50


class CategoryPickerDialog(QDialog):
    """Small searchable chooser used only when changing an assignment."""

    def __init__(self, categories, title="Choose Category", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(420, 130)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Choose the category for the selected pages."))
        self.category = QComboBox()
        self.category.setEditable(True)
        self.category.setInsertPolicy(QComboBox.NoInsert)
        self.category.completer().setCaseSensitivity(Qt.CaseInsensitive)
        self.category.lineEdit().setPlaceholderText("Search categories")
        for item in categories:
            self.category.addItem(item["name"], item["id"])
        layout.addWidget(self.category)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_category_id(self):
        return self.category.currentData()


class ReviewCard(QFrame):
    """A compact, whole-card selection target for one page range."""

    selection_changed = Signal()

    activated = Signal(object)

    def __init__(self, item, thumbnail_service=None, selection_handler=None,
                 parent=None):
        super().__init__(parent)
        self.item = item
        self.item_id = item.get("id", item.get("item_id"))
        self.selection_handler = selection_handler
        self.selectable = bool(item.get("selectable", True))
        self._selected = bool(
            item.get("eligible", item.get("strong_eligible", False))
            and item.get("strength", "STRONG") == "STRONG"
            and item.get("status", "SUGGESTED") == "SUGGESTED"
        )
        self.setObjectName("ReviewCard")
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumWidth(205)
        self.setMaximumWidth(260)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        self.selection_badge = QLabel("✓ Selected")
        self.selection_badge.setAlignment(Qt.AlignRight)
        self.selection_badge.setObjectName("SelectionBadge")
        layout.addWidget(self.selection_badge)
        self.thumbnail_label = QLabel("Loading preview…")
        self.thumbnail_label.setAlignment(Qt.AlignCenter)
        self.thumbnail_label.setFixedHeight(210)
        self.thumbnail_label.setObjectName("ThumbnailSurface")
        self.source_label = QLabel(item.get("source_name") or
                                   Path(item.get("filepath", "")).name)
        source_font = QFont(self.source_label.font())
        source_font.setWeight(QFont.DemiBold)
        self.source_label.setFont(source_font)
        self.employee_label = QLabel(
            "Employee: {}".format(item.get("employee_id") or "—"))
        start, end = self._pages(item)
        self.page_label = QLabel(
            "Page {}".format(start) if start == end
            else "Pages {}–{}".format(start, end))
        self.metadata_label = QLabel(
            "{} · {}".format(
                item.get("employee_id") or "No employee ID",
                self.page_label.text()))
        self.metadata_label.setObjectName("SecondaryLabel")
        category = (
            item.get("suggested_category")
            or item.get("suggested_category_name")
            or item.get("current_category")
            or "Unassigned"
        )
        self.category_label = QLabel(category)
        self.category_label.setObjectName("CategoryLabel")
        category_font = QFont(self.category_label.font())
        category_font.setWeight(QFont.DemiBold)
        self.category_label.setFont(category_font)
        self.evidence_label = QLabel(item.get("evidence") or
                                    item.get("explanation") or "No evidence")
        self.evidence_label.setWordWrap(True)
        self.evidence_label.setMaximumHeight(52)
        self.evidence_label.setToolTip(self.evidence_label.text())
        self.status_label = QLabel(
            item.get("status") or item.get("review_status") or "UNASSIGNED")
        self.status_label.setObjectName("SecondaryLabel")
        for widget in (
            self.thumbnail_label, self.source_label, self.metadata_label,
            self.category_label, self.evidence_label,
        ):
            layout.addWidget(widget)
        self.setAccessibleName(
            "{}, {}, {}".format(
                self.source_label.text(), self.page_label.text(),
                self.category_label.text()))
        self._apply_style()
        self._request_thumbnail(thumbnail_service, start)

    @staticmethod
    def _pages(item):
        pages = item.get("page_numbers")
        if pages:
            return min(pages), max(pages)
        start = int(item.get("start_page", item.get("page_number", 1)))
        return start, int(item.get("end_page", start))

    def _request_thumbnail(self, service, page):
        if service is None:
            self.thumbnail_label.setText("Preview unavailable")
            return
        if hasattr(service, "request_thumbnail"):
            service.request_thumbnail(
                self.item.get("document_id",
                              self.item.get("source_document_id")),
                page,
                self.set_thumbnail,
            )

    def set_thumbnail(self, result):
        path = getattr(result, "path", result)
        pixmap = result if isinstance(result, QPixmap) else QPixmap(str(path))
        if pixmap.isNull():
            self.thumbnail_label.setText("Preview unavailable")
            return
        self.thumbnail_label.setPixmap(
            pixmap.scaled(
                self.thumbnail_label.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        )

    def is_selected(self):
        return self._selected

    def set_selected(self, selected):
        self._selected = bool(selected) and self.selectable
        self._apply_style()
        self.selection_changed.emit()

    def _apply_style(self):
        self.setProperty("selected", self._selected)
        self.selection_badge.setVisible(self._selected)
        self.setAccessibleDescription(
            "Selected" if self._selected else "Not selected")
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def mousePressEvent(self, event):
        if self.selection_handler is not None:
            self.selection_handler.handle_card_click(self, event)
        else:
            self.set_selected(not self._selected)
        if event is not None:
            super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.activated.emit(self)
        super().mouseDoubleClickEvent(event)

    def focusInEvent(self, event):
        self.setProperty("keyboardFocus", True)
        self.style().unpolish(self)
        self.style().polish(self)
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        self.setProperty("keyboardFocus", False)
        self.style().unpolish(self)
        self.style().polish(self)
        super().focusOutEvent(event)

    def keyPressEvent(self, event):
        if self.selection_handler is not None and \
                self.selection_handler.handle_card_key(self, event):
            return
        if event.key() == Qt.Key_Space:
            self.set_selected(not self.is_selected())
            return
        super().keyPressEvent(event)


class ReviewWorkspace(QWidget):
    """Unified suggested, unresolved, and extraction-failure review."""

    source_open_requested = Signal(int, int)

    def __init__(self, database, thumbnail_service=None, confirm=None,
                 category_chooser=None, parent=None):
        super().__init__(parent)
        self.database = database
        self.thumbnail_service = thumbnail_service
        self.confirm = confirm or self._confirm
        self.category_chooser = category_chooser or self._choose_category
        self._categories = []
        self.cards = []
        self.session_completed = 0
        self.batch_token = None
        self._selection_anchor = None
        self._columns = 5
        self._build_ui()
        self._load_categories()
        self.refresh()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(12)
        title = QLabel("Review Workspace")
        title_font = QFont(title.font())
        title_font.setPointSize(title_font.pointSize() + 6)
        title_font.setWeight(QFont.Bold)
        title.setFont(title_font)
        subtitle = QLabel(
            "High-confidence matches are preselected. Review exceptions, then "
            "confirm the batch."
        )
        subtitle.setObjectName("SecondaryLabel")
        subtitle.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(subtitle)

        filters = QHBoxLayout()
        self.view_selector = QComboBox()
        self.view_selector.addItem("Suggestions", "SUGGESTED")
        self.view_selector.addItem("Needs Review", "NEEDS_REVIEW")
        self.view_selector.addItem("Unassigned", "UNASSIGNED")
        self.view_selector.addItem("Extraction Issues", "FAILED")
        self.view_selector.setAccessibleName("Review view")
        self.category_filter = QComboBox()
        self.category_filter.setAccessibleName("Category filter")
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search document text")
        self.search_input.setAccessibleName("Search extracted document text")
        self.search_input.setClearButtonEnabled(True)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self.apply_filters)
        filters.addWidget(QLabel("View"))
        filters.addWidget(self.view_selector)
        filters.addWidget(QLabel("Category"))
        filters.addWidget(self.category_filter)
        filters.addWidget(self.search_input, 1)
        root.addLayout(filters)

        self.progress_label = QLabel()
        self.progress_label.setObjectName("SecondaryLabel")
        root.addWidget(self.progress_label)
        self.active_filters_label = QLabel()
        self.active_filters_label.setObjectName("SecondaryLabel")
        root.addWidget(self.active_filters_label)
        # Kept as data aliases for extensions; these are not duplicate UI.
        self.showing_label = QLabel()
        self.remaining_label = QLabel()
        self.session_progress_label = QLabel("Resolved this session: 0")

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.contact_sheet = QWidget()
        self.card_grid = QGridLayout(self.contact_sheet)
        self.card_grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.empty_state = QLabel()
        self.empty_state.setAlignment(Qt.AlignCenter)
        self.empty_state.setWordWrap(True)
        self.empty_state.setObjectName("EmptyState")
        self.empty_state.hide()
        self.scroll.setWidget(self.contact_sheet)
        root.addWidget(self.scroll, 1)

        self.selection_bar = QWidget()
        self.selection_bar.setObjectName("SelectionBar")
        actions = QHBoxLayout(self.selection_bar)
        actions.setContentsMargins(12, 8, 12, 8)
        self.selection_count_label = QLabel("0 selected")
        self.selection_count_label.setObjectName("SecondaryLabel")
        self.approve_button = QPushButton("Confirm Suggestions")
        self.approve_button.setProperty("primary", True)
        self.change_category_button = QPushButton("Change Category…")
        self.more_button = QToolButton()
        self.more_button.setText("More")
        self.more_button.setToolTip("More actions")
        self.more_button.setPopupMode(QToolButton.InstantPopup)
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self.more_button)
        self.more_button.setMenu(menu)
        self.change_category_action = QAction("Change Category…", self)
        self.open_source_action = QAction("Open in Document", self)
        self.open_source_action.setToolTip(
            "Open the original PDF at the selected page")
        self.clear_selection_action = QAction("Deselect All", self)
        menu.addAction(self.change_category_action)
        menu.addAction(self.open_source_action)
        menu.addSeparator()
        menu.addAction(self.clear_selection_action)
        actions.addWidget(self.selection_count_label)
        actions.addStretch()
        for widget in (
            self.approve_button, self.change_category_button,
            self.more_button,
        ):
            actions.addWidget(widget)
        root.addWidget(self.selection_bar)

        self.search_input.returnPressed.connect(self.apply_filters)
        self.search_input.textChanged.connect(
            lambda _text: self._search_timer.start())
        self.view_selector.currentIndexChanged.connect(self.apply_filters)
        self.category_filter.currentIndexChanged.connect(self.apply_filters)
        self.approve_button.clicked.connect(self.approve_selected)
        self.change_category_button.clicked.connect(self.choose_and_assign)
        self.change_category_action.triggered.connect(self.choose_and_assign)
        self.open_source_action.triggered.connect(self.open_selected_source)
        self.clear_selection_action.triggered.connect(self.clear_selection)

    def _load_categories(self):
        self.category_filter.blockSignals(True)
        self.category_filter.addItem("All categories", None)
        category_loader = getattr(self.database, "list_review_categories", None)
        if category_loader is None:
            category_loader = self.database.list_categories
        for category in category_loader():
            self._categories.append(dict(category))
            self.category_filter.addItem(category["name"], category["id"])
        self.category_filter.blockSignals(False)

    def _filters(self):
        view = self.view_selector.currentData()
        return {
            "category_id": self.category_filter.currentData(),
            "status": view,
            "view": view,
            "search": self.search_input.text().strip(),
        }

    def apply_filters(self):
        sender = self.sender()
        if sender is not None and hasattr(sender, "clearFocus"):
            sender.clearFocus()
        self.refresh()

    def activate_preset(self, name):
        view = {
            "Strong Suggestions": "SUGGESTED",
            "Needs Review": "NEEDS_REVIEW",
            "Unassigned": "UNASSIGNED",
            "Extraction Failures": "FAILED",
        }[name]
        self._set_combo(self.category_filter, None)
        self._set_combo(self.view_selector, view)
        self.refresh()

    @staticmethod
    def _set_combo(combo, value):
        index = combo.findData(value)
        if index >= 0:
            combo.blockSignals(True)
            combo.setCurrentIndex(index)
            combo.blockSignals(False)

    def _query(self):
        filters = self._filters()
        # The production database exposes the snapshot-token API directly.
        # Lightweight UI fakes may instead expose the dictionary adapter.
        if hasattr(self.database, "query_review_batch"):
            if filters["view"] == "FAILED":
                view = "FAILED"
            elif filters["view"] == "UNASSIGNED":
                view = "UNASSIGNED"
            elif filters["view"] == "NEEDS_REVIEW":
                view = "NEEDS_REVIEW"
            else:
                view = "SUGGESTED"
            minimum_score = self.database.get_setting(
                "classification/minimum_score", 80)
            minimum_matches = self.database.get_setting(
                "classification/minimum_matches", 2)
            return self.database.query_review_batch(
                view=view,
                category_id=(filters["category_id"]
                             if isinstance(filters["category_id"], int) else None),
                search=filters["search"],
                limit=BATCH_SIZE,
                minimum_score=minimum_score,
                minimum_matches=minimum_matches,
            )
        return self.database.query_review_items(filters, limit=BATCH_SIZE)

    def refresh(self):
        result = self._query()
        self.batch_token = result.get("batch_token")
        self._clear_cards()
        for index, item in enumerate(result.get("items", [])[:BATCH_SIZE]):
            if "status" not in item:
                item["status"] = item.get("review_status", "UNASSIGNED")
            if "strength" not in item:
                item["strength"] = (
                    "STRONG" if item.get("strong_eligible") else "CHECK")
            card = ReviewCard(
                item, self.thumbnail_service, selection_handler=self)
            card.selection_changed.connect(self._update_selection_label)
            card.activated.connect(self._open_card)
            self.cards.append(card)
        self._reflow_cards()
        shown = len(self.cards)
        remaining = result.get("remaining", result.get("total", shown))
        self.showing_label.setText("Showing {} of current batch".format(shown))
        self.remaining_label.setText("{} remaining".format(remaining))
        self.progress_label.setText(
            "Showing {} of {} · {} resolved this session".format(
                shown, remaining, self.session_completed))
        filters = self._filters()
        active = []
        if filters["category_id"] is not None:
            active.append("Category: {}".format(
                self.category_filter.currentText()))
        if filters["search"]:
            active.append('Text: "{}"'.format(filters["search"]))
        self.active_filters_label.setText(
            "Filtered by " + " · ".join(active) if active else "")
        self.active_filters_label.setVisible(bool(active))
        if shown == 0:
            if filters["search"] or filters["category_id"] is not None:
                message = (
                    "No pages match these filters.\n"
                    "Clear the category or search to see more pages.")
            elif filters["view"] == "FAILED":
                message = "No extraction issues. All searchable text was read."
            else:
                message = "You’re all caught up in this review queue."
            self.empty_state.setText(message)
            self.empty_state.show()
            self.card_grid.addWidget(self.empty_state, 0, 0, 1, self._columns)
        else:
            self.empty_state.hide()
        self._selection_anchor = None
        self._update_selection_label()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow_cards()

    def _reflow_cards(self):
        if not hasattr(self, "card_grid"):
            return
        available = max(220, self.width() - 48)
        columns = max(1, min(6, available // 230))
        self._columns = int(columns)
        for index, card in enumerate(self.cards):
            self.card_grid.addWidget(
                card, index // self._columns, index % self._columns)

    def _clear_cards(self):
        while self.card_grid.count():
            item = self.card_grid.takeAt(0)
            if item.widget() is self.empty_state:
                self.empty_state.hide()
            elif item.widget():
                item.widget().deleteLater()
        self.cards = []

    def _selected_cards(self):
        return [card for card in self.cards if card.is_selected()]

    def select_all_strong(self):
        for card in self.cards:
            eligible = card.item.get(
                "eligible", card.item.get("strong_eligible", False))
            card.set_selected(
                bool(eligible)
                and card.item.get("strength", "STRONG") == "STRONG"
                and card.item.get("status", "SUGGESTED") == "SUGGESTED"
            )

    def clear_selection(self):
        for card in self.cards:
            card.set_selected(False)

    def _update_selection_label(self):
        count = len(self._selected_cards())
        self.selection_count_label.setText(
            "{} selected".format(count))
        self.approve_button.setEnabled(count > 0)
        self.change_category_button.setEnabled(count > 0)
        self.change_category_action.setEnabled(count > 0)
        self.open_source_action.setEnabled(count == 1)
        view = self.view_selector.currentData()
        can_classify = view != "FAILED"
        self.approve_button.setVisible(can_classify and view != "UNASSIGNED")
        self.change_category_button.setVisible(view == "UNASSIGNED")
        self.change_category_action.setVisible(
            can_classify and view != "UNASSIGNED")
        self.change_category_button.setText(
            "Assign Category…" if view == "UNASSIGNED"
            else "Change Category…")
        self.approve_button.setText(
            "Confirm Suggestions ({})".format(count))

    def _confirmed(self, action, cards):
        if not cards:
            return False
        source_count = len({
            card.item.get("document_id",
                          card.item.get("source_document_id"))
            for card in cards
        })
        return self.confirm(action, len(cards), source_count)

    def _apply(self, action, cards, category_id=None):
        if not self._confirmed(action, cards):
            return
        ids = [card.item_id for card in cards]
        if hasattr(self.database, "apply_review_action"):
            database_action = {
                "Approve": "APPROVE_SUGGESTION",
                "Assign": "ASSIGN",
                "Defer": "DEFER",
            }[action]
            self.database.apply_review_action(
                self.batch_token, ids, database_action,
                **({"category_id": category_id}
                   if database_action == "ASSIGN" else {})
            )
        elif action == "Approve":
            self.database.approve_review_items(ids)
        elif action == "Assign":
            self.database.assign_review_items(ids, category_id)
        else:
            self.database.defer_review_items(ids)
        self.session_completed += len(cards)
        self.session_progress_label.setText(
            "Resolved this session: {}".format(self.session_completed))
        self.refresh()

    def handle_card_click(self, card, event):
        index = self.cards.index(card)
        modifiers = event.modifiers() if event is not None else Qt.NoModifier
        if modifiers & Qt.ShiftModifier and self._selection_anchor is not None:
            self._select_range(self._selection_anchor, index)
        else:
            card.set_selected(not card.is_selected())
            self._selection_anchor = index
        card.setFocus()

    def handle_card_key(self, card, event):
        index = self.cards.index(card)
        modifiers = event.modifiers()
        if event.key() == Qt.Key_Space:
            card.set_selected(not card.is_selected())
            self._selection_anchor = index
            return True
        if event.key() == Qt.Key_A and modifiers & (
                Qt.ControlModifier | Qt.MetaModifier):
            for visible_card in self.cards:
                visible_card.set_selected(visible_card.selectable)
            self._selection_anchor = index
            return True
        if event.key() == Qt.Key_Escape:
            self.clear_selection()
            return True
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._open_card(card)
            return True
        moves = {
            Qt.Key_Left: -1,
            Qt.Key_Right: 1,
            Qt.Key_Up: -self._columns,
            Qt.Key_Down: self._columns,
        }
        if event.key() in moves:
            target = max(0, min(len(self.cards) - 1,
                                index + moves[event.key()]))
            if modifiers & Qt.ShiftModifier:
                if self._selection_anchor is None:
                    self._selection_anchor = index
                self._select_range(self._selection_anchor, target)
            else:
                self._selection_anchor = target
            self.cards[target].setFocus()
            return True
        return False

    def _select_range(self, start, end):
        low, high = sorted((start, end))
        for index, card in enumerate(self.cards):
            card.set_selected(low <= index <= high and card.selectable)

    def _open_card(self, card):
        start, _ = ReviewCard._pages(card.item)
        document_id = card.item.get(
            "document_id", card.item.get("source_document_id"))
        self.source_open_requested.emit(int(document_id), int(start))

    def approve_selected(self):
        self._apply("Approve", self._selected_cards())

    def _choose_category(self):
        dialog = CategoryPickerDialog(self._categories, parent=self)
        return dialog.selected_category_id() if dialog.exec() else None

    def choose_and_assign(self):
        if not self._selected_cards():
            return
        category_id = self.category_chooser()
        if category_id is not None:
            self.assign_selected(category_id)

    def assign_selected(self, category_id=None):
        if category_id is not None:
            self._apply("Assign", self._selected_cards(), category_id)

    def defer_selected(self):
        self._apply("Defer", self._selected_cards())

    def open_selected_source(self):
        cards = self._selected_cards()
        if not cards:
            return
        card = cards[0]
        start, _ = ReviewCard._pages(card.item)
        document_id = card.item.get(
            "document_id", card.item.get("source_document_id"))
        self.source_open_requested.emit(int(document_id), int(start))

    def _confirm(self, action, item_count, source_count):
        if action == "Approve":
            question = (
                "Confirm the suggested category for {} selected page "
                "range{} across {} source PDF{}?"
            ).format(
                item_count, "" if item_count == 1 else "s",
                source_count, "" if source_count == 1 else "s")
        elif action == "Assign":
            question = (
                "Assign {} selected page range{} across {} source PDF{} "
                "to the chosen category?"
            ).format(
                item_count, "" if item_count == 1 else "s",
                source_count, "" if source_count == 1 else "s")
        else:
            question = "{} {} selected item{}?".format(
                action, item_count, "" if item_count == 1 else "s")
        return QMessageBox.question(
            self,
            "{} selected items".format(action),
            question,
            QMessageBox.Yes | QMessageBox.No,
        ) == QMessageBox.Yes
