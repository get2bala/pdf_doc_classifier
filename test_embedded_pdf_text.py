from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject, DictionaryObject, NameObject,
)

from classifier import AnalysisPipeline, EmbeddedPdfTextEngine
from database import DatabaseManager


def make_searchable_pdf(path, text):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    font_reference = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): font_reference,
        }),
    })
    stream = DecodedStreamObject()
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream.set_data(
        "BT /F1 12 Tf 72 720 Td ({}) Tj ET".format(escaped).encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)


def test_embedded_pdf_text_drives_suggestion_without_rendering_or_tesseract(tmp_path):
    source = tmp_path / "searchable.pdf"
    make_searchable_pdf(
        source, "Income Tax Department Permanent Account Number")
    database = DatabaseManager(":memory:")
    category = database.add_category(
        "PAN Card", "permanent account number,income tax department",
        "COMBINE", "{employee_id}_{category}.pdf")
    document = database.add_source_document(str(source), "EMP1", 1)

    result = AnalysisPipeline(
        database, EmbeddedPdfTextEngine()).analyze_page(document, 1)

    assert result["suggested_category_id"] == category
    assert result["score"] == 100
    assert "Permanent Account Number" in result["ocr_text"]
    assert database.get_page_assignments(document)[0]["status"] == "UNCLASSIFIED"


def test_searchable_pdf_with_no_matching_text_is_a_controlled_no_match(tmp_path):
    source = tmp_path / "other.pdf"
    make_searchable_pdf(source, "Unrelated searchable document")
    database = DatabaseManager(":memory:")
    database.add_category(
        "PAN Card", "permanent account number", "COMBINE",
        "{employee_id}_{category}.pdf")
    document = database.add_source_document(str(source), "EMP2", 1)
    result = AnalysisPipeline(
        database, EmbeddedPdfTextEngine()).analyze_page(document, 1)
    assert result["status"] == "NO_MATCH"
    assert result["ocr_text"] == "Unrelated searchable document"
