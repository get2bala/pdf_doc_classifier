from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

from database import DatabaseManager
from pdf_engine import PdfExporter, contiguous_runs, render_page


def make_pdf(path: Path, pages: int):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=300, height=400)
    with path.open("wb") as handle:
        writer.write(handle)


def test_contiguous_runs():
    assert contiguous_runs([5, 2, 1, 4, 9]) == [[1, 2], [4, 5], [9]]


def test_end_to_end_export_and_no_overwrite(tmp_path):
    source, output = tmp_path / "source.pdf", tmp_path / "out"
    make_pdf(source, 5)
    db = DatabaseManager(":memory:")
    combined = db.add_category("Identity", "", "COMBINE", "{employee_id}_identity.pdf")
    separate = db.add_category("Letters", "", "SEPARATE", "{employee_id}_letter_{instance}.pdf")
    document_id = db.add_source_document(str(source), "EMP001", 5)
    db.assign_pages(document_id, [1, 3], combined)
    db.assign_pages(document_id, [2, 4], separate)
    db.set_page_status(document_id, [5], "EXCLUDED")

    files = PdfExporter(db).export(document_id, str(output))
    assert sorted(Path(f).name for f in files) == [
        "EMP001_identity.pdf", "EMP001_letter_01.pdf", "EMP001_letter_02.pdf"
    ]
    assert len(PdfReader(str(output / "EMP001_identity.pdf")).pages) == 2
    assert len(PdfReader(str(output / "EMP001_letter_01.pdf")).pages) == 1
    with pytest.raises(FileExistsError):
        PdfExporter(db).export(document_id, str(output))
    assert not list(output.glob(".classifier-*"))


def test_export_blocks_unclassified_pages(tmp_path):
    source = tmp_path / "source.pdf"
    make_pdf(source, 2)
    db = DatabaseManager(":memory:")
    category = db.add_category("ID", "", "COMBINE", "id.pdf")
    document_id = db.add_source_document(str(source), "E", 2)
    db.assign_pages(document_id, [1], category)
    with pytest.raises(ValueError, match="unresolved: 2"):
        PdfExporter(db).build_plan(document_id, str(tmp_path / "out"))


def test_render_real_pdf_page(tmp_path):
    source = tmp_path / "source.pdf"
    make_pdf(source, 1)
    image = render_page(str(source), 1, scale=0.5)
    assert image.width == 150
    assert image.height == 200


def test_export_rejects_changed_source(tmp_path):
    source = tmp_path / "source.pdf"
    make_pdf(source, 1)
    db = DatabaseManager(":memory:")
    category = db.add_category("ID", "", "COMBINE", "id.pdf")
    document = db.add_source_document(str(source), "E", 1)
    db.assign_pages(document, [1], category)
    source.write_bytes(source.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="changed after import"):
        PdfExporter(db).export(document, str(tmp_path / "out"))
