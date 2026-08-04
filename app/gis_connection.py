"""ArcGIS Portal connection, matching the auth conventions used by
Joint-Use-Permits/scripts/create_layers.py (GIS_PORTAL_AUTH_MODE=pro/profile).

Deliberately NOT cached -- see get_gis()'s docstring.
"""

from __future__ import annotations

from arcgis.gis import GIS

from . import config


class GisConnectionError(RuntimeError):
    """Raised when the ArcGIS connection didn't actually authenticate."""


def get_gis() -> GIS:
    """Connects fresh every call -- never cached.

    A pole-discovery job can run for several minutes (loading Qwen,
    running the vision pass) before this connection is actually used.
    Caching a GIS object made once (e.g. at server startup, or on an
    earlier job) risks reusing a since-expired/invalidated session on
    every job after that point, which surfaces as a confusing 403 "You do
    not have permissions" error despite nothing about real access having
    changed. Building the connection fresh right before use avoids that.

    Also verifies the connection actually logged in as a real user rather
    than silently falling back to an anonymous session -- ``GIS(profile=...)``
    can do this quietly (e.g. an expired/invalid saved token) and an
    anonymous session will always fail permission checks on private
    items, which otherwise shows up several steps downstream as a
    confusing generic 403 instead of this much clearer error.
    """

    if config.ARCGIS_AUTH_MODE == "pro":
        gis = GIS("pro")
    elif config.ARCGIS_AUTH_MODE == "profile":
        if not config.ARCGIS_PROFILE:
            raise ValueError("ARCGIS_PROFILE is required when ARCGIS_AUTH_MODE=profile")
        gis = GIS(profile=config.ARCGIS_PROFILE)
    else:
        raise ValueError(f"Unsupported ARCGIS_AUTH_MODE: {config.ARCGIS_AUTH_MODE!r}")

    if gis.users.me is None:
        raise GisConnectionError(
            f"Connected to {gis.url} but authenticated as nobody (anonymous session). "
            f"ARCGIS_AUTH_MODE={config.ARCGIS_AUTH_MODE!r}"
            + (
                f", ARCGIS_PROFILE={config.ARCGIS_PROFILE!r} -- "
                "that saved profile's token is likely missing, expired, or was never "
                "created successfully. Recreate it (see Joint-Use-Permits-REST's README)."
                if config.ARCGIS_AUTH_MODE == "profile"
                else " -- ArcGIS Pro may not actually be signed in right now."
            )
        )

    return gis
