from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

from classifier import AnalysisPipeline, suggest_category
from database import DatabaseManager
from pdf_engine import PdfExporter, page_count, safe_filename


def make_pdf(path: Path, pages: int = 1, password=None):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=100, height=100)
    if password:
        writer.encrypt(password)
    with path.open("wb") as handle:
        writer.write(handle)


def ready_export(tmp_path, pattern="result.pdf"):
    source = tmp_path / "source.pdf"
    make_pdf(source)
    db = DatabaseManager(":memory:")
    category = db.add_category("Records", "", "COMBINE", pattern)
    document = db.add_source_document(str(source), "E1", 1)
    db.assign_pages(document, [1], category)
    return db, document, source


def test_page_count_rejects_corrupt_and_password_protected_files(tmp_path):
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"not a pdf")
    with pytest.raises(ValueError, match="Cannot read PDF"):
        page_count(str(corrupt))

    encrypted = tmp_path / "encrypted.pdf"
    make_pdf(encrypted, password="secret")
    with pytest.raises(ValueError, match="password protected"):
        page_count(str(encrypted))


def test_filename_is_single_safe_bounded_component():
    name = safe_filename(" ../bad\x00:name/" + "x" * 300 + ".pdf")
    assert "/" not in name and "\\" not in name and "\x00" not in name
    assert len(name) <= 180
    assert name.endswith(".pdf")


def test_output_dir_is_resolved_and_pattern_cannot_escape(tmp_path, monkeypatch):
    db, document, _ = ready_export(tmp_path, "../../{employee_id}:record.pdf")
    monkeypatch.chdir(tmp_path)
    plan = PdfExporter(db).build_plan(document, "relative-output")
    expected = (tmp_path / "relative-output").resolve()
    assert Path(plan[0]["path"]).parent == expected
    assert Path(plan[0]["path"]).name == "_.._E1_record.pdf"


def test_publish_race_never_overwrites_and_rolls_back(tmp_path, monkeypatch):
    db, document, _ = ready_export(tmp_path)
    output = tmp_path / "out"
    real_link = __import__("os").link

    def competing_link(source, destination):
        Path(destination).write_bytes(b"created by another process")
        return real_link(source, destination)

    monkeypatch.setattr("pdf_engine.os.link", competing_link)
    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        PdfExporter(db).export(document, str(output))
    assert (output / "result.pdf").read_bytes() == b"created by another process"
    assert not list(output.glob(".classifier-*"))


def test_exported_pdf_is_readable_and_status_changes(tmp_path):
    db, document, _ = ready_export(tmp_path)
    [result] = PdfExporter(db).export(document, str(tmp_path / "out"))
    assert len(PdfReader(result).pages) == 1
    assert db.get_source_document(document)["status"] == "EXPORTED"


def test_keyword_matching_does_not_match_inside_another_word():
    categories = [{"id": 1, "keywords": "id"}]
    assert suggest_category("paid invoice", categories)[0] is None
    assert suggest_category("employee ID card", categories)[0] == 1


def test_accept_prevalidates_entire_batch(tmp_path, monkeypatch):
    db = DatabaseManager(":memory:")
    category = db.add_category("Offer", "offer")
    source = tmp_path / "source.pdf"
    make_pdf(source, 2)
    document = db.add_source_document(str(source), "E1", 2)
    db.save_analysis(document, 1, "offer", category, 100, "match")
    pipeline = AnalysisPipeline(db, object())
    with pytest.raises(ValueError, match="Page 2"):
        pipeline.accept(document, [1, 2])
    assert db.get_page_assignments(document)[0]["status"] == "UNCLASSIFIED"


def test_analyze_document_rejects_unknown_document():
    pipeline = AnalysisPipeline(DatabaseManager(":memory:"), object())
    with pytest.raises(ValueError, match="Source document not found"):
        list(pipeline.analyze_document(999))
