"""Environment-driven configuration for the Joint Use Permits REST API.

Values come from the repo-root .env file (see .env.example), falling back
to the defaults below. This service is self-contained -- it runs its own
PDF/pole-discovery pipeline (see app/discovery/) rather than depending on
any other project.

The .env file is loaded explicitly by absolute path rather than relying on
python-dotenv's search-from-cwd behavior, so the settings apply no matter
which directory uvicorn was launched from.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

#: Repo root (this file lives at <repo root>/app/config.py).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

ENV_FILE_LOADED = load_dotenv(ENV_FILE)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value else default


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    return float(value) if value else default


# ---------------------------------------------------------------------------
# Pole discovery (app/discovery/)
# ---------------------------------------------------------------------------

def _path(name: str, default: Path) -> Path:
    """Resolves a configured path, anchoring anything relative to the repo
    root rather than the current working directory -- so the service
    behaves the same regardless of where uvicorn was launched from."""

    value = _env(name)
    path = Path(value) if value else default
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


# Directory this service uses to stage uploaded plan set PDFs.
UPLOAD_DIR = _path("UPLOAD_DIR", Path("uploads"))

# Authoritative pole reference database (SQLite). Column/table names are
# configurable since the schema belongs to whatever system maintains it,
# not this service.
POLE_DB_PATH = _path("POLE_DB_PATH", Path("pole_reference.sqlite"))
POLE_TABLE = _env("POLE_TABLE", "SupportStructure")
POLE_ID_COLUMN = _env("POLE_ID_COLUMN", "FacilityID")
POLE_SOURCE_ID_COLUMN = _env("POLE_SOURCE_ID_COLUMN", "OBJECTID")
POLE_X_COLUMN = _env("POLE_X_COLUMN", "X")
POLE_Y_COLUMN = _env("POLE_Y_COLUMN", "Y")

# Local Qwen3-VL model directory, used to visually read pole ID labels on
# pages the native-text pass couldn't match.
QWEN_MODEL_DIR = Path(_env("QWEN_MODEL_DIR", r"C:\PoleScan\models\Qwen3-VL-8B-Instruct"))

# Visual readings below this confidence are discarded before catalog matching.
MINIMUM_CANDIDATE_CONFIDENCE = _env_float("MINIMUM_CANDIDATE_CONFIDENCE", 0.80)

MAX_PDF_MB = _env_int("MAX_PDF_MB", 500)

# ---------------------------------------------------------------------------
# ArcGIS Portal
# ---------------------------------------------------------------------------

# "pro" uses the active portal signed into ArcGIS Pro; "profile" uses a
# saved ArcGIS API for Python profile.
ARCGIS_AUTH_MODE = _env("ARCGIS_AUTH_MODE", "pro")
ARCGIS_PROFILE = _env("ARCGIS_PROFILE")

POLES_LAYER_ITEM_ID = _env("POLES_LAYER_ITEM_ID", "579b19fe7d694112b4995d59f5572936")
WORK_AREAS_LAYER_ITEM_ID = _env(
    "WORK_AREAS_LAYER_ITEM_ID", "4bf5a6e4fd234f6d92d81a576ba9a8c3"
)

# Matches scripts/create_layers.py's WKID in the Joint-Use-Permits repo
# (NAD_1983_StatePlane_Ohio_North_FIPS_3401_Feet). The pole catalog's x/y
# must already be in this spatial reference -- this service does not
# reproject them.
POLE_COORDINATE_WKID = _env_int("POLE_COORDINATE_WKID", 3734)

# Feet (this WKID's linear unit) to buffer around the discovered pole
# locations when deriving a work-area polygon.
WORK_AREA_BUFFER_FEET = _env_int("WORK_AREA_BUFFER_FEET", 50)

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

HOST = _env("HOST", "0.0.0.0")
PORT = _env_int("PORT", 8000)

# Comma-separated list of allowed CORS origins for the ExB app calling this
# API (e.g. "https://localhost:3001,https://experience.arcgis.com"). "*"
# is the default for a first-pass internal tool -- lock this down before
# this API is reachable from anywhere untrusted.
ALLOWED_ORIGINS = [origin.strip() for origin in _env("ALLOWED_ORIGINS", "*").split(",")]
