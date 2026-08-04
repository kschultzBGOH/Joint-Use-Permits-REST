"""Runs PoleScan's discovery pipeline against an uploaded PDF and reads back
its result.

PoleScan's main.py is a CLI tool: it takes a PDF path, creates its own job
directory with an internally generated job ID (we don't get to choose it),
runs intake + native-text + visual discovery, and writes
jobs/<job_id>/results/pole_discovery.json. It doesn't print anything
machine-structured on success -- we recover the job ID by matching the
"Job ID: <id>" line it logs (see main.py's print_job_summary), then read the
JSON it wrote from that job's results directory.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from . import config

_JOB_ID_PATTERN = re.compile(r"Job ID:\s*(\S+)")


class PoleScanPipelineError(RuntimeError):
    """Raised when PoleScan's CLI fails or its result can't be located/read."""


def run_discovery(pdf_path: Path) -> dict[str, Any]:
    """Runs PoleScan's main.py against pdf_path and returns its
    pole_discovery.json contents."""

    command = [config.POLESCAN_PYTHON_EXECUTABLE, "main.py", str(pdf_path)]

    try:
        completed = subprocess.run(
            command,
            cwd=config.POLESCAN_PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=config.PIPELINE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise PoleScanPipelineError(
            f"PoleScan did not finish within {config.PIPELINE_TIMEOUT_SECONDS}s."
        ) from exc

    combined_output = completed.stdout + completed.stderr

    if completed.returncode != 0:
        raise PoleScanPipelineError(
            f"PoleScan exited with code {completed.returncode}: "
            f"{_tail(combined_output)}"
        )

    job_id = _extract_job_id(combined_output)
    if job_id is None:
        raise PoleScanPipelineError(
            "Could not find PoleScan's Job ID in its output: "
            f"{_tail(combined_output)}"
        )

    result_path = config.POLESCAN_JOBS_DIR / job_id / "results" / "pole_discovery.json"
    if not result_path.exists():
        raise PoleScanPipelineError(f"Expected discovery result not found: {result_path}")

    try:
        return json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PoleScanPipelineError(f"Could not read {result_path}: {exc}") from exc


def _extract_job_id(output: str) -> str | None:
    match = _JOB_ID_PATTERN.search(output)
    return match.group(1) if match else None


def _tail(text: str, lines: int = 20) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])
