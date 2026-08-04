"""Cached ArcGIS Portal connection, matching the auth conventions used by
Joint-Use-Permits/scripts/create_layers.py and PoleScan's own hosted output
(GIS_PORTAL_AUTH_MODE=pro/profile)."""

from __future__ import annotations

from arcgis.gis import GIS

from . import config

_gis: GIS | None = None


def get_gis() -> GIS:
    global _gis
    if _gis is not None:
        return _gis

    if config.ARCGIS_AUTH_MODE == "pro":
        _gis = GIS("pro")
    elif config.ARCGIS_AUTH_MODE == "profile":
        if not config.ARCGIS_PROFILE:
            raise ValueError("ARCGIS_PROFILE is required when ARCGIS_AUTH_MODE=profile")
        _gis = GIS(profile=config.ARCGIS_PROFILE)
    else:
        raise ValueError(f"Unsupported ARCGIS_AUTH_MODE: {config.ARCGIS_AUTH_MODE!r}")

    return _gis
