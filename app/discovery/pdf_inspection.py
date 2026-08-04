"""Validate an uploaded PDF and classify each page's content.

Page classification (content_type, likely_scanned_page) drives which pages
get selected for the visual (Qwen) discovery pass -- see renderer.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

import pymupdf


class PageInspection(TypedDict):
    page_number: int
    page_index: int
    width_points: float
    height_points: float
    embedded_word_count: int
    image_count: int
    largest_image_coverage: float
    has_embedded_text: bool
    has_images: bool
    likely_scanned_page: bool
    content_type: str


class PDFValidationError(ValueError):
    """Raised when an uploaded PDF fails validation."""


def validate_pdf(pdf_path: str | Path, max_pdf_mb: int) -> Path:
    resolved_path = Path(pdf_path).resolve()

    if not resolved_path.exists():
        raise FileNotFoundError(f"PDF file does not exist: {resolved_path}")

    if not resolved_path.is_file():
        raise PDFValidationError(f"The supplied path is not a file: {resolved_path}")

    if resolved_path.suffix.lower() != ".pdf":
        raise PDFValidationError(f"The file must have a .pdf extension: {resolved_path.name}")

    file_size_bytes = resolved_path.stat().st_size
    if file_size_bytes == 0:
        raise PDFValidationError(f"The PDF is empty: {resolved_path.name}")

    max_file_size_bytes = max_pdf_mb * 1024 * 1024
    if file_size_bytes > max_file_size_bytes:
        file_size_mb = file_size_bytes / (1024 * 1024)
        raise PDFValidationError(
            f"The PDF is {file_size_mb:.2f} MB. The maximum allowed size is {max_pdf_mb} MB."
        )

    with resolved_path.open("rb") as pdf_file:
        file_header = pdf_file.read(1024)
    if b"%PDF-" not in file_header:
        raise PDFValidationError(f"The file does not contain a valid PDF header: {resolved_path.name}")

    try:
        with pymupdf.open(resolved_path) as document:
            if not document.is_pdf:
                raise PDFValidationError(f"PyMuPDF does not recognize this as a PDF: {resolved_path.name}")
            if document.needs_pass:
                raise PDFValidationError(f"The PDF is password protected: {resolved_path.name}")
            if document.page_count == 0:
                raise PDFValidationError(f"The PDF does not contain any pages: {resolved_path.name}")
            if document.load_page(0).rect.get_area() <= 0:
                raise PDFValidationError(f"The first page has invalid dimensions: {resolved_path.name}")
    except PDFValidationError:
        raise
    except Exception as exc:
        raise PDFValidationError(f"The PDF could not be opened or is damaged: {resolved_path.name}") from exc

    return resolved_path


def classify_page_content(word_count: int, image_count: int, largest_image_coverage: float) -> str:
    has_text = word_count > 0
    has_images = image_count > 0

    if has_text and has_images:
        return "mixed"
    if has_text:
        return "embedded_text"
    if has_images and largest_image_coverage >= 0.75:
        return "likely_scanned"
    if has_images:
        return "image_only"

    # The page might contain vector linework even though no text or raster
    # images were detected.
    return "vector_or_empty"


def _largest_image_coverage(page: pymupdf.Page, image_information: list[dict]) -> float:
    page_area = page.rect.get_area()
    if page_area <= 0 or not image_information:
        return 0.0

    largest_coverage = 0.0
    for image in image_information:
        bbox = image.get("bbox")
        if bbox is None:
            continue
        coverage = min(max(pymupdf.Rect(bbox).get_area() / page_area, 0.0), 1.0)
        largest_coverage = max(largest_coverage, coverage)

    return round(largest_coverage, 4)


def inspect_pages(pdf_path: str | Path) -> list[PageInspection]:
    resolved_path = Path(pdf_path).resolve()
    page_results: list[PageInspection] = []

    with pymupdf.open(resolved_path) as document:
        for page_index, page in enumerate(document):
            page_number = page_index + 1
            words = page.get_text("words", sort=False)
            image_information = page.get_image_info()

            word_count = len(words)
            image_count = len(image_information)
            largest_image_coverage = _largest_image_coverage(page, image_information)
            likely_scanned_page = word_count == 0 and largest_image_coverage >= 0.75

            page_results.append(
                {
                    "page_number": page_number,
                    "page_index": page_index,
                    "width_points": round(page.rect.width, 2),
                    "height_points": round(page.rect.height, 2),
                    "embedded_word_count": word_count,
                    "image_count": image_count,
                    "largest_image_coverage": largest_image_coverage,
                    "has_embedded_text": word_count > 0,
                    "has_images": image_count > 0,
                    "likely_scanned_page": likely_scanned_page,
                    "content_type": classify_page_content(
                        word_count=word_count,
                        image_count=image_count,
                        largest_image_coverage=largest_image_coverage,
                    ),
                }
            )

    return page_results
