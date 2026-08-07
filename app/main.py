"""Joint Use Permits REST API.

Implements the job-submit/poll contract documented in the Joint-Use-Permits
widget repo's restApiService.ts and README:

    POST /jobs                multipart/form-data, field "file" = plan set PDF
    GET  /jobs/{job_id}       poll for status/result
                              -> {"status": "queued" | "processing", "detail"?: string}
                              -> {"status": "completed", "permit": {...}}
                              -> {"status": "failed", "error": string}

`detail` is a human-readable "what's happening right now" (e.g. "Reading
page 3 of 12..."), set by the discovery pipeline's on_progress callback as
it runs -- optional and best-effort, so an older/simpler pipeline stage
that doesn't report progress just leaves it unset rather than breaking
anything that reads it.

Submitting a job runs this service's own pole-discovery pipeline
(app/discovery/) against the PDF, derives a work-area polygon from the
discovered pole locations, and creates the permit + its poles directly in
the Joint Use Permits hosted layers. Finding zero poles doesn't fail the
job -- it creates a shape-less permit with none, for the widget's "Define
Project Scope" screen to fill in by hand.

    POST /permits             no body -- creates an empty permit synchronously
                              -> 201 {objectId, globalId, permitNumber, poleCount: 0}

For someone with no plan set PDF to upload at all: skips discovery
entirely rather than making a PDF upload mandatory just to reach the
screen where poles get added by hand anyway.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from . import config
from .discovery.pipeline import DiscoveryError, discover_poles
from .job_store import job_store
from .permit_creation import PermitCreationError, create_permit_and_poles

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Joint Use Permits REST API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def log_resolved_config() -> None:
    """Logs the settings actually in effect.

    A .env file that isn't being read is invisible otherwise -- the service
    just quietly runs on defaults. Logging this (especially whether .env was
    found at all) makes that immediately obvious instead of surfacing later
    as a confusing downstream failure.
    """

    if config.ENV_FILE_LOADED:
        logger.info("Loaded settings from %s", config.ENV_FILE)
    else:
        logger.warning(
            "No .env file found at %s -- running entirely on built-in defaults.",
            config.ENV_FILE,
        )

    logger.info("  ARCGIS_AUTH_MODE=%s", config.ARCGIS_AUTH_MODE)
    logger.info("  ARCGIS_PROFILE=%s", config.ARCGIS_PROFILE or "(unset)")
    logger.info(
        "  SERVICE_ITEM_ID=%s (WorkAreas layer %s, Poles layer %s)",
        config.SERVICE_ITEM_ID or "(unset -- permit creation will fail)",
        config.WORK_AREAS_LAYER_INDEX,
        config.POLES_LAYER_INDEX,
    )
    logger.info("  POLE_DB_PATH=%s (exists=%s)", config.POLE_DB_PATH, config.POLE_DB_PATH.exists())
    logger.info("  POLE_TABLE=%s, POLE_ID_COLUMN=%s", config.POLE_TABLE, config.POLE_ID_COLUMN)
    logger.info("  QWEN_MODEL_DIR=%s", config.QWEN_MODEL_DIR)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/jobs", status_code=202)
async def create_job(file: UploadFile) -> dict:
    job = job_store.create()

    job_input_dir = config.UPLOAD_DIR / job.id / "input"
    job_input_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = job_input_dir / "source.pdf"
    pdf_path.write_bytes(await file.read())

    thread = threading.Thread(target=_process_job, args=(job.id, pdf_path), daemon=True)
    thread.start()

    return {"jobId": job.id}


def _serialize_permit(permit) -> dict:
    return {
        "objectId": permit.object_id,
        "globalId": permit.global_id,
        "permitNumber": permit.permit_number,
        "poleCount": permit.pole_count,
    }


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} was not found.")

    if job.status in ("queued", "processing"):
        response: dict = {"status": job.status}
        if job.detail:
            response["detail"] = job.detail
        return response

    if job.status == "completed":
        return {"status": "completed", "permit": _serialize_permit(job.permit)}

    return {"status": "failed", "error": job.error}


@app.post("/permits", status_code=201)
def create_empty_permit() -> dict:
    """Creates a permit with no discovered poles, synchronously (no PDF to
    process, so no job/polling needed) -- for someone with no plan set to
    upload who wants to define the project's scope by hand from the start
    (search the pole catalog, or click poles on the linked map) rather
    than being forced through the upload step first.
    """

    empty_discovery_result = {
        "status": "no_poles_found",
        "accepted_pole_count": 0,
        "review_candidate_count": 0,
        "accepted_pole_ids": [],
        "accepted_poles": [],
    }

    try:
        permit = create_permit_and_poles(empty_discovery_result, pdf_path=None)
    except PermitCreationError as exc:
        logger.error("Failed to create an empty permit: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    logger.info("Created empty permit %s (no plan set uploaded).", permit.permit_number)
    return _serialize_permit(permit)


def _process_job(job_id: str, pdf_path: Path) -> None:
    job_store.set_processing(job_id)
    logger.info("Job %s: discovering poles in %s", job_id, pdf_path)

    def report_progress(detail: str) -> None:
        logger.info("Job %s: %s", job_id, detail)
        job_store.set_progress(job_id, detail)

    try:
        discovery_result = discover_poles(job_id, pdf_path, on_progress=report_progress)
        logger.info(
            "Job %s: discovery status=%s, accepted_poles=%s",
            job_id,
            discovery_result.get("status"),
            discovery_result.get("accepted_pole_count"),
        )

        pole_count = discovery_result.get("accepted_pole_count", 0)
        if pole_count == 0:
            report_progress(
                "No poles found automatically -- creating the permit so you can define its "
                "scope by hand..."
            )
        else:
            report_progress(f"Creating the permit and {pole_count} pole(s) in ArcGIS...")
        permit = create_permit_and_poles(discovery_result, pdf_path)
        job_store.set_completed(job_id, permit)
        logger.info(
            "Job %s: created permit %s with %s pole(s)",
            job_id,
            permit.permit_number,
            permit.pole_count,
        )

    except (DiscoveryError, PermitCreationError) as exc:
        logger.error("Job %s failed: %s", job_id, exc)
        job_store.set_failed(job_id, str(exc))

    except Exception as exc:  # noqa: BLE001 -- surface unexpected failures to the caller too
        logger.exception("Job %s failed unexpectedly.", job_id)
        job_store.set_failed(job_id, f"Unexpected error: {exc}")
