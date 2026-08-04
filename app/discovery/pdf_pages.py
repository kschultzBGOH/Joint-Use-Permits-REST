"""PDF page inspection and rendering via PyMuPDF.

Self-contained -- no dependency on any other project's PDF handling.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pymupdf
from PIL import Image


@dataclass
class PageText:
    page_number: int  # 1-based
    text: str


class PdfError(RuntimeError):
    """Raised when a PDF can't be opened or read."""


def open_pdf(pdf_path: Path) -> pymupdf.Document:
    try:
        document = pymupdf.open(pdf_path)
    except Exception as exc:  # pymupdf raises its own exception types
        raise PdfError(f"Could not open PDF {pdf_path}: {exc}") from exc

    if document.page_count == 0:
        document.close()
        raise PdfError(f"PDF has no pages: {pdf_path}")

    return document


def extract_native_text(document: pymupdf.Document) -> list[PageText]:
    return [
        PageText(page_number=index + 1, text=page.get_text("text") or "")
        for index, page in enumerate(document)
    ]


def render_page_image(document: pymupdf.Document, page_number: int, dpi: int) -> Image.Image:
    """Renders one page (1-based) to a PIL image at the given DPI."""

    page = document[page_number - 1]
    zoom = dpi / 72
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
