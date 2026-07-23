"""Small deterministic suggestion engine and optional local OCR pipeline."""

import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Tuple

from pdf_engine import render_page


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return re.sub(r"\s+", " ", text).strip()


def suggest_category(text: str, categories: Iterable[Dict]) -> Tuple[Optional[int], float, str]:
    normalized = normalize_text(text)
    best = (None, 0.0, "No configured keywords matched")
    for category in categories:
        keywords = [normalize_text(k) for k in category["keywords"].split(",") if k.strip()]
        keywords = [keyword for keyword in keywords if keyword]
        if not keywords:
            continue
        matches = [keyword for keyword in keywords if re.search(
            r"(?<!\w){}(?!\w)".format(re.escape(keyword)), normalized)]
        score = 100.0 * len(matches) / len(keywords)
        if matches and score > best[1]:
            best = (category["id"], score, "Matched: {}".format(", ".join(matches)))
    return best


class EmbeddedPdfTextEngine:
    """Extract text already embedded in a PDF without rendering or OCR."""

    def recognize_page(self, pdf_path: str, page_number: int) -> str:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("PDF text extraction requires pypdf") from exc
        try:
            reader = PdfReader(pdf_path)
            if reader.is_encrypted:
                unlocked = reader.decrypt("")
                if not unlocked:
                    raise ValueError("PDF is password protected")
            if page_number < 1 or page_number > len(reader.pages):
                raise ValueError("Page number is outside the source document")
            return reader.pages[page_number - 1].extract_text() or ""
        except ValueError:
            raise
        except Exception as exc:
            raise RuntimeError(
                "Could not extract the PDF text layer from page {}: {}".format(
                    page_number, exc)
            ) from exc


@dataclass
class AnalysisPipeline:
    database: object
    engine: object

    def analyze_page(self, source_document_id: int, page_number: int) -> Dict:
        document = self.database.get_source_document(source_document_id)
        if not document:
            raise ValueError("Source document not found")
        if page_number < 1 or page_number > document["page_count"]:
            raise ValueError("Page number is outside the source document")
        try:
            if hasattr(self.engine, "recognize_page"):
                text = self.engine.recognize_page(
                    document["filepath"], page_number)
            else:
                # Compatibility path for optional image-based OCR adapters.
                image = render_page(document["filepath"], page_number, scale=2.0)
                text = self.engine.recognize(image)
            category_id, score, explanation = suggest_category(text, self.database.list_categories())
            status = "PENDING" if category_id else "NO_MATCH"
            self.database.save_analysis(source_document_id, page_number, text, category_id, score, explanation, status)
        except Exception as exc:
            self.database.save_analysis(source_document_id, page_number, "", None, None, "OCR failed", "FAILED", str(exc))
            raise
        return self.database.get_analysis(source_document_id, page_number)

    def analyze_document(self, source_document_id: int):
        document = self.database.get_source_document(source_document_id)
        if not document:
            raise ValueError("Source document not found")
        for page in range(1, document["page_count"] + 1):
            try:
                yield self.analyze_page(source_document_id, page)
            except Exception:
                yield self.database.get_analysis(source_document_id, page)

    def accept(self, source_document_id: int, page_numbers: Iterable[int]):
        pages = sorted(set(page_numbers))
        if not pages:
            return
        suggestions = []
        for page in pages:
            analysis = self.database.get_analysis(source_document_id, page)
            if not analysis or not analysis["suggested_category_id"]:
                raise ValueError("Page {} has no suggestion to accept".format(page))
            suggestions.append((page, analysis["suggested_category_id"]))
        # Validate the entire selection first, so a bad page cannot leave a
        # confusing half-accepted batch.
        for page, category_id in suggestions:
            self.database.assign_pages(source_document_id, [page], category_id, "ASSIGNED")
        self.database.mark_analysis(source_document_id, pages, "ACCEPTED")
