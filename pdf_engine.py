"""PDF validation, rendering, and all-or-nothing batch export."""

import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Dict, List, Sequence


class PdfDependencyError(RuntimeError):
    pass


def _pypdf():
    try:
        from pypdf import PdfReader, PdfWriter
        return PdfReader, PdfWriter
    except ImportError as exc:
        raise PdfDependencyError("PDF support requires: pip install pypdf") from exc


def page_count(pdf_path: str) -> int:
    PdfReader, _ = _pypdf()
    try:
        reader = PdfReader(pdf_path)
        if reader.is_encrypted:
            try:
                unlocked = reader.decrypt("")
            except Exception:
                unlocked = 0
            if not unlocked:
                raise ValueError("PDF is password protected")
        count = len(reader.pages)
        if count < 1:
            raise ValueError("PDF contains no pages")
        return count
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Cannot read PDF: {}".format(exc)) from exc


def render_page(pdf_path: str, page_number: int, scale: float = 1.5):
    """Return a PIL image. Rendering is optional so export remains lightweight."""
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise PdfDependencyError("Preview/OCR requires: pip install pypdfium2 Pillow") from exc
    if scale <= 0:
        raise ValueError("Render scale must be greater than zero")
    document = page = bitmap = None
    try:
        document = pdfium.PdfDocument(pdf_path)
        if page_number < 1 or page_number > len(document):
            raise IndexError("Page number out of range")
        # Materialize the PIL image before closing the PDFium objects.  This is
        # important on Windows, where an open document also locks the source.
        page = document[page_number - 1]
        bitmap = page.render(scale=scale)
        image = bitmap.to_pil().copy()
        return image
    except (IndexError, ValueError, PdfDependencyError):
        raise
    except Exception as exc:
        raise ValueError("Cannot render PDF page {}: {}".format(page_number, exc)) from exc
    finally:
        for resource in (bitmap, page, document):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass


def contiguous_runs(pages: Sequence[int]) -> List[List[int]]:
    runs: List[List[int]] = []
    for page in sorted(set(pages)):
        if not runs or page != runs[-1][-1] + 1:
            runs.append([page])
        else:
            runs[-1].append(page)
    return runs


def safe_filename(value: str) -> str:
    value = re.sub(r"[\x00-\x1f\x7f\\/:*?\"<>|]+", "_", str(value)).strip(" .")
    # Keep room for collision suffixes and avoid filesystem-specific path limits.
    if len(value) > 180:
        suffix = Path(value).suffix[:20]
        value = value[:180 - len(suffix)].rstrip(" .") + suffix
    return value or "document.pdf"


class PdfExporter:
    def __init__(self, database):
        self.database = database

    def build_plan(self, source_document_id: int, output_dir: str) -> List[Dict]:
        document = self.database.get_source_document(source_document_id)
        if not document:
            raise ValueError("Source document not found")
        source = Path(document["filepath"])
        if not source.is_file():
            raise FileNotFoundError("Source PDF no longer exists: {}".format(source))
        stat = source.stat()
        if stat.st_size != document["file_size"] or stat.st_mtime_ns != document["file_mtime_ns"]:
            raise ValueError("Source PDF changed after import; import it again before exporting")
        assignments = self.database.get_page_assignments(source_document_id)
        unresolved = [a["page_number"] for a in assignments if a["status"] in ("UNCLASSIFIED", "NEEDS_REVIEW")]
        if unresolved:
            raise ValueError("Classify or exclude every page before export (unresolved: {})".format(
                ", ".join(map(str, unresolved))))

        destination = Path(output_dir).expanduser().resolve()
        plan = []
        used = set()
        for group in self.database.export_groups(source_document_id):
            chunks = contiguous_runs(group["pages"]) if group["output_policy"] == "SEPARATE" else [group["pages"]]
            for index, pages in enumerate(chunks, 1):
                values = {
                    "employee_id": document["employee_id"],
                    "category": group["category_name"],
                    "instance": "{:02d}".format(index),
                }
                try:
                    filename = group["filename_pattern"].format(**values)
                except (KeyError, ValueError) as exc:
                    raise ValueError("Invalid filename pattern for {}: {}".format(group["category_name"], exc))
                if not filename.lower().endswith(".pdf"):
                    filename += ".pdf"
                filename = safe_filename(filename)
                base, suffix = Path(filename).stem, Path(filename).suffix
                candidate, serial = filename, 2
                while candidate.lower() in used:
                    candidate = "{}_{}{}".format(base, serial, suffix)
                    serial += 1
                used.add(candidate.lower())
                plan.append({
                    "path": str(destination / candidate),
                    "pages": pages,
                    "category_id": group.get("category_id"),
                })
        if not plan:
            raise ValueError("There are no assigned pages to export")
        return plan

    def export(self, source_document_id: int, output_dir: str) -> List[str]:
        PdfReader, PdfWriter = _pypdf()
        document = self.database.get_source_document(source_document_id)
        plan = self.build_plan(source_document_id, output_dir)
        destination = Path(output_dir).expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".classifier-", dir=str(destination)))
        published = []
        try:
            reader = PdfReader(document["filepath"])
            if reader.is_encrypted:
                try:
                    unlocked = reader.decrypt("")
                except Exception:
                    unlocked = 0
                if not unlocked:
                    raise ValueError("Source PDF is password protected and cannot be exported")
            staged = []
            for item in plan:
                writer = PdfWriter()
                for page in item["pages"]:
                    if page < 1 or page > len(reader.pages):
                        raise ValueError("Assigned page {} is outside the source PDF".format(page))
                    writer.add_page(reader.pages[page - 1])
                temp_path = staging / Path(item["path"]).name
                with temp_path.open("wb") as handle:
                    writer.write(handle)
                # Re-open before publishing to detect incomplete writes.
                if len(PdfReader(str(temp_path)).pages) != len(item["pages"]):
                    raise IOError("Export verification failed for {}".format(temp_path.name))
                staged.append((temp_path, Path(item["path"])))
            conflicts = [str(final) for _, final in staged if final.exists()]
            if conflicts:
                raise FileExistsError("Refusing to overwrite existing output: {}".format(", ".join(conflicts)))
            for temp_path, final_path in staged:
                # A preflight exists() check alone has a race: replace() can
                # overwrite a file created by another process.  Hard-linking is
                # atomic and fails if the destination appeared in the meantime.
                try:
                    os.link(str(temp_path), str(final_path))
                except FileExistsError:
                    raise FileExistsError("Refusing to overwrite existing output: {}".format(final_path))
                published.append(final_path)
            with self.database.transaction() as conn:
                conn.execute("UPDATE source_documents SET status='EXPORTED' WHERE id=?", (source_document_id,))
            return [str(final) for _, final in staged]
        except Exception:
            # Roll back only files created by this invocation; existing files are
            # rejected before publication and are never touched.
            for path in published:
                path.unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(str(staging), ignore_errors=True)
