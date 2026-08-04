"""In-memory job tracking.

Deliberately simple -- one process, no persistence, no queue broker. If
this service is ever run with multiple worker processes (e.g. multiple
uvicorn workers), jobs must move to a shared store (Redis, a database
table) since each process would otherwise have its own dict. Fine for a
single-worker internal tool.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Literal, Optional

JobStatus = Literal["queued", "processing", "completed", "failed"]


@dataclass
class CreatedPermit:
    object_id: int
    global_id: str
    permit_number: str
    pole_count: int


@dataclass
class Job:
    id: str
    status: JobStatus = "queued"
    permit: Optional[CreatedPermit] = None
    error: Optional[str] = None


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self) -> Job:
        job = Job(id=str(uuid.uuid4()))
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def set_processing(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.status = "processing"

    def set_completed(self, job_id: str, permit: CreatedPermit) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.status = "completed"
                job.permit = permit

    def set_failed(self, job_id: str, error: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.status = "failed"
                job.error = error


job_store = JobStore()
