"""Discovers poles in a plan set PDF.

Two-tier pass: fast native-PDF-text matching first (free, no GPU), then the
Qwen vision model for any pages where that found nothing. Deduplicates by
pole ID -- a pole found on multiple pages/sources counts once. Runs are
serialized (one plan set at a time) since the vision model uses a single
shared GPU and was never designed to handle concurrent requests.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .native_text import run_native_text_discovery
from .pdf_pages import PdfError, extract_native_text, open_pdf
from .pole_catalog import PoleCatalog, PoleCatalogError
from .vision_model import VisionModelError
from .visual_discovery import pages_needing_visual_pass, run_visual_discovery

_pipeline_lock = threading.Lock()


class DiscoveryError(RuntimeError):
    """Raised when pole discovery fails."""


def discover_poles(pdf_path: Path) -> dict[str, Any]:
    with _pipeline_lock:
        try:
            document = open_pdf(pdf_path)
        except PdfError as exc:
            raise DiscoveryError(str(exc)) from exc

        try:
            pole_catalog = PoleCatalog.load()
        except PoleCatalogError as exc:
            document.close()
            raise DiscoveryError(str(exc)) from exc

        try:
            native_pages = extract_native_text(document)
            native_results = run_native_text_discovery(native_pages, pole_catalog)

            matched_by_page: dict[int, set[str]] = {}
            for result in native_results:
                matched_by_page.setdefault(result["page_number"], set()).add(result["pole_id"])

            pages_to_render = pages_needing_visual_pass(native_pages, matched_by_page)
            visual_results = run_visual_discovery(document, pages_to_render, pole_catalog)

        except VisionModelError as exc:
            raise DiscoveryError(str(exc)) from exc
        finally:
            document.close()

        accepted_by_id: dict[str, dict[str, Any]] = {}
        for result in native_results + visual_results:
            accepted_by_id.setdefault(result["pole_id"], result)

        accepted_poles = list(accepted_by_id.values())

        return {
            "status": "completed" if accepted_poles else "no_poles_found",
            "accepted_pole_count": len(accepted_poles),
            "accepted_poles": accepted_poles,
            "pages_used_native_text": sorted(matched_by_page.keys()),
            "pages_used_visual": pages_to_render,
        }
