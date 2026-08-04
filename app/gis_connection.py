"""ArcGIS Portal connection, matching the auth conventions used by
Joint-Use-Permits/scripts/create_layers.py (GIS_PORTAL_AUTH_MODE=pro/profile).

Deliberately NOT cached. A pole-discovery job can run for several minutes
(loading Qwen, running the vision pass) before this connection is actually
used -- caching a GIS object made once (e.g. at server startup, or on the
first job) risks reusing a since-expired/invalidated session on every job
after that point, which surfaces as a confusing 403 "You do not have
permissions" error despite nothing about real access having changed.
Building the connection fresh right before use avoids that entirely.
"""

from __future__ import annotations

from arcgis.gis import GIS

from . import config


def get_gis() -> GIS:
    if config.ARCGIS_AUTH_MODE == "pro":
        return GIS("pro")

    if config.ARCGIS_AUTH_MODE == "profile":
        if not config.ARCGIS_PROFILE:
            raise ValueError("ARCGIS_PROFILE is required when ARCGIS_AUTH_MODE=profile")
        return GIS(profile=config.ARCGIS_PROFILE)

    raise ValueError(f"Unsupported ARCGIS_AUTH_MODE: {config.ARCGIS_AUTH_MODE!r}")
