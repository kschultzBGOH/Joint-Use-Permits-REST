"""Qwen-based visual pole discovery for pages the native-text pass missed."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pymupdf

from .. import config
from .pdf_pages import PageText, render_page_image
from .pole_catalog import PoleCatalog
from .vision_model import (
    VisionModelBundle,
    load_vision_model,
    read_pole_labels,
    unload_vision_model,
)


def pages_needing_visual_pass(
    native_pages: list[PageText], matched_pole_ids_by_page: dict[int, set[str]]
) -> list[int]:
    """Pages where the native-text pass found no catalog matches."""

    return [
        page.page_number
        for page in native_pages
        if not matched_pole_ids_by_page.get(page.page_number)
    ]


def run_visual_discovery(
    document: pymupdf.Document, page_numbers: list[int], pole_catalog: PoleCatalog
) -> list[dict[str, Any]]:
    if not page_numbers:
        return []

    accepted: list[dict[str, Any]] = []
    vision_model: VisionModelBundle | None = None

    try:
        vision_model = load_vision_model(Path(config.QWEN_MODEL_DIR))

        for page_number in page_numbers:
            image = render_page_image(document, page_number, dpi=config.VISUAL_RENDER_DPI)
            readings = read_pole_labels(vision_model, image)

            for reading in readings:
                raw_text = str(reading.get("text", "")).strip()
                try:
                    confidence = float(reading.get("confidence", 0.0))
                except (TypeError, ValueError):
                    confidence = 0.0

                if not raw_text or confidence < config.MINIMUM_CANDIDATE_CONFIDENCE:
                    continue

                record = pole_catalog.match(raw_text)
                if record is None or record.x is None or record.y is None:
                    continue

                accepted.append(
                    {
                        "pole_id": record.pole_id,
                        "x": record.x,
                        "y": record.y,
                        "source": "visual",
                        "page_number": page_number,
                        "raw_text": raw_text,
                        "confidence": confidence,
                    }
                )
    finally:
        if vision_model is not None:
            unload_vision_model(vision_model)

    return accepted
