"""Orchestrates pole discovery for one uploaded plan set PDF.

Runs the same sequence as the original design this was adapted from:
validate -> load the authoritative pole catalog -> fast native-text pass
-> visual (Qwen) pass for any pages that pass didn't resolve -> merge into
a final accepted-pole list with catalog coordinates.

Serialized with a lock -- the vision model uses one shared GPU and was
never designed to handle concurrent requests.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .. import config
from .native_text import NativeTextDiscoveryError, discover_native_text
from .pdf_inspection import PDFValidationError, inspect_pages, validate_pdf
from .pole_catalog import PoleCatalog, PoleCatalogError
from .renderer import VisualRenderError, prepare_visual_images
from .results import build_pole_discovery_result
from .vision_model import VisionModelLoadError, load_vision_model, unload_vision_model
from .visual_discovery import VisualDiscoveryError, discover_visual_poles

_pipeline_lock = threading.Lock()


class DiscoveryError(RuntimeError):
    """Raised when pole discovery fails."""


def discover_poles(job_id: str, pdf_path: Path) -> dict[str, Any]:
    with _pipeline_lock:
        try:
            validated_pdf_path = validate_pdf(pdf_path=pdf_path, max_pdf_mb=config.MAX_PDF_MB)
            page_inspections = inspect_pages(validated_pdf_path)
            pole_catalog = PoleCatalog.load()

            native_discovery = discover_native_text(
                pdf_path=validated_pdf_path, pole_catalog=pole_catalog
            )

            # When native discovery finds nothing at all, retain pure
            # embedded-text pages in the visual fallback to protect recall.
            include_text_only_fallback = native_discovery["exact_match_occurrence_count"] == 0

            job_directory = config.UPLOAD_DIR / job_id
            visual_output_directory = job_directory / "pages" / "visual"

            visual_renders = prepare_visual_images(
                pdf_path=validated_pdf_path,
                page_inspections=page_inspections,
                native_discovery=native_discovery,
                output_directory=visual_output_directory,
                preview_dpi=140,
                include_embedded_text_without_matches=include_text_only_fallback,
            )

            visual_discovery = None
            vision_model = None

            if visual_renders["selected_page_count"] > 0:
                try:
                    vision_model = load_vision_model(config.QWEN_MODEL_DIR)
                    visual_discovery = discover_visual_poles(
                        pdf_path=validated_pdf_path,
                        visual_renders=visual_renders,
                        pole_catalog=pole_catalog,
                        vision_model=vision_model,
                        output_directory=visual_output_directory,
                        page_confidence_threshold=0.90,
                        preview_max_edge_pixels=1800,
                        tile_dpi=225,
                        tile_pixels=1800,
                        tile_overlap_ratio=0.15,
                        tile_model_max_edge_pixels=1800,
                        minimum_candidate_confidence=config.MINIMUM_CANDIDATE_CONFIDENCE,
                        strict=False,
                    )
                finally:
                    if vision_model is not None:
                        unload_vision_model(vision_model)

            return build_pole_discovery_result(
                pole_catalog=pole_catalog,
                native_discovery=native_discovery,
                visual_discovery=visual_discovery,
            )

        except (
            FileNotFoundError,
            PDFValidationError,
            PoleCatalogError,
            NativeTextDiscoveryError,
            VisualRenderError,
            VisionModelLoadError,
            VisualDiscoveryError,
        ) as exc:
            raise DiscoveryError(str(exc)) from exc
