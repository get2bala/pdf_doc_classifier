from pathlib import Path


def test_project_metadata_and_entrypoint_exist():
    # Keep this smoke test dependency-free on the supported Python 3.9 runtime.
    metadata = Path("pyproject.toml").read_text(encoding="utf-8")
    assert 'pdf-document-classifier = "main_window:main"' in metadata
    assert Path("PdfDocumentClassifier.spec").is_file()
