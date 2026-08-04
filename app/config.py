"""Environment-driven configuration for the Joint Use Permits REST API.

All values are read from environment variables (see .env.example). Nothing
here is auto-detected -- this service imports and runs PoleScan's pipeline
in-process (see polescan_pipeline.py), so it must itself run inside
PoleScan's own configured environment: torch, transformers, arcgis, numpy,
pymupdf, and pillow already installed (see PoleScan's INSTALL.txt).
"""

from __future__ import annotations

import os
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value else default


# ---------------------------------------------------------------------------
# PoleScan pipeline
# ---------------------------------------------------------------------------

# Path to the PoleScan project checkout (contains the polescan/ package).
# Inserted onto sys.path so `import polescan...` resolves from here --
# see polescan_pipeline.py.
POLESCAN_PROJECT_DIR = Path(_env("POLESCAN_PROJECT_DIR", r"C:\PoleScan"))

# Directory this service uses to stage uploaded PDFs before handing them to
# PoleScan's pipeline.
UPLOAD_DIR = Path(_env("UPLOAD_DIR", str(POLESCAN_PROJECT_DIR / "uploads")))

# This service never calls PoleScan's own write_gis_outputs -- it creates
# Joint Use Permits' features itself (see permit_creation.py). PoleScan's
# GIS_OUTPUT_TARGETS setting has no effect on anything this service does.

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
# (NAD_1983_StatePlane_Ohio_North_FIPS_3401_Feet), and PoleScan's own
# POLE_COORDINATE_WKID. Pole x/y from pole_discovery.json are already in
# this spatial reference.
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
