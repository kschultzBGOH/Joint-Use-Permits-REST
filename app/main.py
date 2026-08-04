"""Joint Use Permits REST API.

Implements the job-submit/poll contract documented in the Joint-Use-Permits
widget repo's restApiService.ts and README:

    POST /jobs                multipart/form-data, field "file" = plan set PDF
    GET  /jobs/{job_id}       poll for status/result

Submitting a job runs PoleScan's discovery pipeline against the PDF,
derives a work-area polygon from the discovered pole locations, and creates
the permit + its poles directly in the Joint Use Permits hosted layers.
"""

from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from . import config
from .gis_connection import get_gis
from .job_store import job_store
from .permit_creation import PermitCreationError, create_permit_and_poles
from .polescan_pipeline import PoleScanPipelineError, run_discovery

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Joint Use Permits REST API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/jobs", status_code=202)
async def create_job(file: UploadFile) -> dict:
    job = job_store.create()

    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = config.UPLOAD_DIR / f"{job.id}.pdf"
    pdf_path.write_bytes(await file.read())

    thread = threading.Thread(target=_process_job, args=(job.id, pdf_path), daemon=True)
    thread.start()

    return {"jobId": job.id}


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} was not found.")

    if job.status in ("queued", "processing"):
        return {"status": job.status}

    if job.status == "completed":
        return {
            "status": "completed",
            "permit": {
                "objectId": job.permit.object_id,
                "globalId": job.permit.global_id,
                "permitNumber": job.permit.permit_number,
                "poleCount": job.permit.pole_count,
            },
        }

    return {"status": "failed", "error": job.error}


def _process_job(job_id: str, pdf_path: Path) -> None:
    job_store.set_processing(job_id)
    logger.info("Job %s: running PoleScan discovery on %s", job_id, pdf_path)

    try:
        discovery_result = run_discovery(pdf_path)
        logger.info(
            "Job %s: PoleScan discovery status=%s, accepted_poles=%s",
            job_id,
            discovery_result.get("status"),
            discovery_result.get("accepted_pole_count"),
        )

        gis = get_gis()
        permit = create_permit_and_poles(gis, discovery_result)
        job_store.set_completed(job_id, permit)
        logger.info(
            "Job %s: created permit %s with %s pole(s)",
            job_id,
            permit.permit_number,
            permit.pole_count,
        )

    except (PoleScanPipelineError, PermitCreationError) as exc:
        logger.error("Job %s failed: %s", job_id, exc)
        job_store.set_failed(job_id, str(exc))

    except Exception as exc:  # noqa: BLE001 -- surface unexpected failures to the caller too
        logger.exception("Job %s failed unexpectedly.", job_id)
        job_store.set_failed(job_id, f"Unexpected error: {exc}")
