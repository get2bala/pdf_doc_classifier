"""Appearance contracts for readable native light and dark modes."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication

from appearance import apply_appearance, build_palette
from database import DatabaseManager
from dialogs import SettingsDialog


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("mode", ("LIGHT", "DARK"))
def test_explicit_palettes_keep_text_and_surfaces_readable(mode):
    palette = build_palette(mode)
    foreground = palette.color(QPalette.WindowText).lightness()
    surface = palette.color(QPalette.Window).lightness()
    if mode == "DARK":
        assert foreground > surface
    else:
        assert foreground < surface
    assert palette.color(QPalette.Highlight) != palette.color(QPalette.Base)


def test_application_theme_uses_semantic_palette_styles(app):
    apply_appearance(app, "DARK")
    assert app.property("appearanceMode") == "DARK"
    assert "palette(base)" in app.styleSheet()
    assert "background:white" not in app.styleSheet().replace(" ", "").lower()
    apply_appearance(app, "LIGHT")
    assert app.property("appearanceMode") == "LIGHT"


def test_configuration_persists_appearance_choice(app):
    database = DatabaseManager(":memory:")
    database.add_category("Identity", "card")
    dialog = SettingsDialog(database)
    assert tuple(
        dialog.appearance.itemData(index)
        for index in range(dialog.appearance.count())
    ) == ("SYSTEM", "LIGHT", "DARK")
    dialog.appearance.setCurrentIndex(dialog.appearance.findData("DARK"))
    dialog.save()
    assert database.get_setting("appearance/mode") == "DARK"
    database.close()
