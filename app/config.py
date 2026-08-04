"""Environment-driven configuration for the Joint Use Permits REST API.

All values are read from environment variables (see .env.example). This
service is self-contained -- it runs its own PDF/pole-discovery pipeline
(see app/discovery/) rather than depending on any other project.
"""

from __future__ import annotations

import os
from pathlib import Path


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

# Directory this service uses to stage uploaded plan set PDFs.
UPLOAD_DIR = Path(_env("UPLOAD_DIR", "./uploads"))

# Authoritative pole reference database (SQLite). Column/table names are
# configurable since the schema belongs to whatever system maintains it,
# not this service.
POLE_DB_PATH = Path(_env("POLE_DB_PATH", "./pole_reference.sqlite"))
POLE_TABLE = _env("POLE_TABLE", "Poles")
POLE_ID_COLUMN = _env("POLE_ID_COLUMN", "PoleID")
POLE_X_COLUMN = _env("POLE_X_COLUMN", "X")
POLE_Y_COLUMN = _env("POLE_Y_COLUMN", "Y")

# Local Qwen vision-language model directory, used to visually read pole ID
# labels on pages the native-text pass couldn't match.
QWEN_MODEL_DIR = Path(_env("QWEN_MODEL_DIR", "./models/qwen3-vl"))

# DPI used when rendering a PDF page to an image for the vision model.
VISUAL_RENDER_DPI = _env_int("VISUAL_RENDER_DPI", 200)

# Visual readings below this confidence are discarded before catalog matching.
MINIMUM_CANDIDATE_CONFIDENCE = _env_float("MINIMUM_CANDIDATE_CONFIDENCE", 0.80)

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
