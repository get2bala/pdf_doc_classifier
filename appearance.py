"""Accessible, palette-driven application appearance."""

from PySide6.QtGui import QColor, QPalette


MODES = ("SYSTEM", "LIGHT", "DARK")


def _set_group(palette, group, role, color):
    palette.setColor(group, role, QColor(color))


def build_palette(mode):
    """Build a complete explicit palette for deterministic light/dark modes."""
    mode = str(mode or "SYSTEM").upper()
    if mode not in ("LIGHT", "DARK"):
        raise ValueError("Appearance must be SYSTEM, LIGHT, or DARK")
    dark = mode == "DARK"
    colors = {
        "window": "#1C1C1E" if dark else "#F5F5F7",
        "base": "#242426" if dark else "#FFFFFF",
        "alternate": "#2C2C2E" if dark else "#F2F2F7",
        "text": "#F5F5F7" if dark else "#1D1D1F",
        "secondary": "#AEAEB2" if dark else "#6E6E73",
        "button": "#2C2C2E" if dark else "#FFFFFF",
        "mid": "#48484A" if dark else "#D1D1D6",
        "highlight": "#0A84FF" if dark else "#0066CC",
        "highlighted": "#FFFFFF",
    }
    palette = QPalette()
    assignments = (
        (QPalette.Window, colors["window"]),
        (QPalette.Base, colors["base"]),
        (QPalette.AlternateBase, colors["alternate"]),
        (QPalette.WindowText, colors["text"]),
        (QPalette.Text, colors["text"]),
        (QPalette.Button, colors["button"]),
        (QPalette.ButtonText, colors["text"]),
        (QPalette.ToolTipBase, colors["base"]),
        (QPalette.ToolTipText, colors["text"]),
        (QPalette.PlaceholderText, colors["secondary"]),
        (QPalette.Mid, colors["mid"]),
        (QPalette.Highlight, colors["highlight"]),
        (QPalette.HighlightedText, colors["highlighted"]),
        (QPalette.Link, colors["highlight"]),
    )
    for role, color in assignments:
        palette.setColor(QPalette.Active, role, QColor(color))
        palette.setColor(QPalette.Inactive, role, QColor(color))
    for role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
        _set_group(palette, QPalette.Disabled, role, colors["secondary"])
    return palette


APP_STYLE = """
QMainWindow, QDialog { background-color: palette(window); color: palette(text); }
QLabel { background-color: transparent; }
QPushButton, QToolButton {
    min-height: 28px;
    padding: 4px 10px;
}
QLineEdit, QComboBox {
    min-height: 28px;
    padding: 2px 6px;
}
QTableView::item { padding: 5px 7px; }
QLineEdit, QComboBox, QListWidget, QTableWidget, QScrollArea {
    background-color: palette(base);
    color: palette(text);
}
QFrame#ReviewCard {
    background-color: palette(base);
    border: 2px solid palette(mid);
    border-radius: 10px;
}
QFrame#ReviewCard[selected="true"] { border-color: palette(highlight); }
QFrame#ReviewCard[keyboardFocus="true"][selected="false"] {
    border-color: palette(highlight);
    border-style: dashed;
}
QLabel#ThumbnailSurface, QLabel#PdfPreview {
    background-color: palette(alternate-base);
    color: palette(placeholder-text);
    border: 1px solid palette(mid);
    border-radius: 8px;
}
QPushButton#SummaryCard {
    background-color: palette(base);
    border: 1px solid palette(mid);
    border-radius: 10px;
    font-size: 14px;
    font-weight: 600;
    padding: 10px 8px;
}
QPushButton#SummaryCard:checked {
    border: 2px solid palette(highlight);
}
QLabel#CategoryLabel { color: palette(link); }
QLabel#SelectionBadge { color: palette(link); font-weight: 600; }
QLabel#SecondaryLabel { color: palette(placeholder-text); }
QWidget#SelectionBar {
    background-color: palette(alternate-base);
    border: 1px solid palette(mid);
    border-radius: 10px;
}
QPushButton[primary="true"] { font-weight: 600; padding: 6px 14px; }
"""


def apply_appearance(application, mode):
    """Apply an explicit or system-derived palette and semantic styling."""
    requested = str(mode or "SYSTEM").upper()
    if requested not in MODES:
        requested = "SYSTEM"
    effective = requested
    if requested == "SYSTEM":
        scheme = getattr(application.styleHints(), "colorScheme", lambda: None)()
        name = getattr(scheme, "name", "")
        if not name:
            name = str(scheme)
        effective = "DARK" if "Dark" in name else "LIGHT"
    application.setPalette(build_palette(effective))
    application.setStyleSheet(APP_STYLE)
    application.setProperty("appearanceMode", requested)
    application.setProperty("effectiveAppearance", effective)


def follow_system_appearance(application):
    """Keep System mode synchronized with OS appearance changes."""
    if application.property("systemAppearanceBound"):
        return
    signal = getattr(application.styleHints(), "colorSchemeChanged", None)
    if signal is not None:
        signal.connect(
            lambda _scheme: apply_appearance(application, "SYSTEM")
            if application.property("appearanceMode") == "SYSTEM" else None)
    application.setProperty("systemAppearanceBound", True)
