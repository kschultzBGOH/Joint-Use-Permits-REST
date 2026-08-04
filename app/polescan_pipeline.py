"""Runs PoleScan's discovery pipeline in-process.

PoleScan was built as a proof-of-concept CLI (main.py), not a library with
a stable API, and it wasn't designed to run in a separate process/
environment from whatever calls it. Rather than shelling out to that CLI
(which needs a second Python environment, a matching project path, and
scraping a job ID back out of log lines), this imports PoleScan's actual
pipeline modules directly from wherever POLESCAN_PROJECT_DIR points, and
runs the same steps main.py runs -- minus PoleScan's own GIS output step
(write_gis_outputs), since Joint Use Permits creates its own features via
permit_creation.py instead of PoleScan_Poles.

This means this service's Python environment now needs to be the SAME one
PoleScan itself uses -- the Anaconda environment with torch, transformers,
arcgis, numpy, pymupdf, and pillow already installed (see PoleScan's own
INSTALL.txt). arcpy is not required here: we never import polescan.output,
which is the only part of PoleScan that touches arcpy.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

from . import config

# One PoleScan run at a time. main.py was never built to run concurrently --
# it loads a shared vision model onto a single GPU per run. Two overlapping
# jobs would race on that, not speed anything up.
_pipeline_lock = threading.Lock()

_polescan_path_added = False


class PoleScanPipelineError(RuntimeError):
    """Raised when PoleScan's pipeline fails."""


def _ensure_polescan_importable() -> None:
    global _polescan_path_added
    if _polescan_path_added:
        return

    project_dir = str(config.POLESCAN_PROJECT_DIR)
    if project_dir not in sys.path:
        sys.path.insert(0, project_dir)
    _polescan_path_added = True


def run_discovery(pdf_path: Path) -> dict[str, Any]:
    """Runs PoleScan's intake + native-text + visual discovery against
    pdf_path and returns the same shape as pole_discovery.json."""

    _ensure_polescan_importable()

    # Imported here (not at module load time) so POLESCAN_PROJECT_DIR is on
    # sys.path first, and so importing this module doesn't itself require
    # PoleScan's dependencies to be installed until a job actually runs.
    from polescan.config import (
        JOBS_DIR,
        MAX_PDF_MB,
        MODEL_DIR,
        POLE_DB_PATH,
        POLE_ID_COLUMN,
        POLE_SOURCE_ID_COLUMN,
        POLE_STATUS_COLUMN,
        POLE_TABLE,
        POLE_UPDATED_COLUMN,
        POLE_X_COLUMN,
        POLE_Y_COLUMN,
    )
    from polescan.discovery.native_text import NativeTextDiscoveryError, discover_native_text
    from polescan.discovery.renderer import VisualRenderError, prepare_visual_images
    from polescan.discovery.results import DiscoveryResultError, write_pole_discovery_result
    from polescan.discovery.visual_discovery import VisualDiscoveryError, discover_visual_poles
    from polescan.intake.service import prepare_plan_job
    from polescan.intake.validation import PDFValidationError
    from polescan.models import VisionModelLoadError, load_vision_model, unload_vision_model
    from polescan.reference.catalog import PoleCatalog, PoleCatalogError
    from polescan.reference.loader import PoleReferenceError, PoleSourceConfig, load_pole_records

    with _pipeline_lock:
        vision_model = None
        try:
            plan_job = prepare_plan_job(
                pdf_path=pdf_path,
                jobs_dir=JOBS_DIR,
                max_pdf_mb=MAX_PDF_MB,
            )
            stored_pdf_path = Path(plan_job["source_file"]["stored_file_path"])
            visual_output_directory = Path(plan_job["directories"]["pages"]) / "visual"

            pole_source_config = PoleSourceConfig(
                database_path=POLE_DB_PATH,
                table=POLE_TABLE,
                pole_id_column=POLE_ID_COLUMN,
                source_id_column=POLE_SOURCE_ID_COLUMN,
                x_column=POLE_X_COLUMN,
                y_column=POLE_Y_COLUMN,
                status_column=POLE_STATUS_COLUMN,
                updated_at_column=POLE_UPDATED_COLUMN,
            )
            pole_reference = load_pole_records(pole_source_config)
            pole_catalog = PoleCatalog.from_load_result(pole_reference)

            native_discovery = discover_native_text(
                pdf_path=stored_pdf_path,
                pole_catalog=pole_catalog,
            )

            # Matches main.py: retain pure embedded-text pages in the visual
            # fallback when native discovery found nothing, to protect recall.
            include_text_only_fallback = native_discovery["exact_match_occurrence_count"] == 0

            visual_renders = prepare_visual_images(
                pdf_path=stored_pdf_path,
                page_inspections=plan_job["pages"],
                native_discovery=native_discovery,
                output_directory=visual_output_directory,
                preview_dpi=140,
                render_tiles=False,
                include_embedded_text_without_matches=include_text_only_fallback,
            )

            visual_discovery = None
            if visual_renders["selected_page_count"] > 0:
                vision_model = load_vision_model(MODEL_DIR)
                visual_discovery = discover_visual_poles(
                    pdf_path=stored_pdf_path,
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
                    minimum_candidate_confidence=0.80,
                    strict=False,
                )

            result_output_directory = Path(plan_job["directories"]["pages"]).parent / "results"

            return write_pole_discovery_result(
                job_id=plan_job["job_id"],
                source_pdf_path=stored_pdf_path,
                output_directory=result_output_directory,
                pole_catalog=pole_catalog,
                native_discovery=native_discovery,
                visual_discovery=visual_discovery,
            )

        except (
            FileNotFoundError,
            PDFValidationError,
            PoleReferenceError,
            PoleCatalogError,
            NativeTextDiscoveryError,
            VisualRenderError,
            VisionModelLoadError,
            VisualDiscoveryError,
            DiscoveryResultError,
        ) as exc:
            raise PoleScanPipelineError(str(exc)) from exc

        finally:
            if vision_model is not None:
                try:
                    unload_vision_model(vision_model)
                except Exception:
                    # Best-effort cleanup -- don't mask the real error (if any)
                    # or fail a successful run over a cleanup problem.
                    pass
