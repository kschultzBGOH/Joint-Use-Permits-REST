"""ArcGIS Portal connection, matching the auth conventions used by
Joint-Use-Permits/scripts/create_layers.py (GIS_PORTAL_AUTH_MODE=pro/profile).

Deliberately NOT cached -- see get_gis()'s docstring.

Only ARCGIS_AUTH_MODE=pro touches arcpy/ArcGIS Pro at all -- it hands off
to the Pro application's own signed-in session, which requires Pro
installed and running on this machine, and this service running inside
Pro's own Python environment. "profile" and "credentials" both talk to
Portal purely over REST via the `arcgis` package (its PyPI wheel, not the
one bundled with Pro) -- no arcpy, no Pro, no special conda environment.
"credentials" is the simplest of the two for a server deployment: no
interactive one-time setup, just a service account's username/password.
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
    elif config.ARCGIS_AUTH_MODE == "credentials":
        missing = [
            name
            for name, value in (
                ("ARCGIS_URL", config.ARCGIS_URL),
                ("ARCGIS_USERNAME", config.ARCGIS_USERNAME),
                ("ARCGIS_PASSWORD", config.ARCGIS_PASSWORD),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                f"ARCGIS_AUTH_MODE=credentials requires {', '.join(missing)} to be set."
            )
        gis = GIS(config.ARCGIS_URL, config.ARCGIS_USERNAME, config.ARCGIS_PASSWORD)
    else:
        raise ValueError(f"Unsupported ARCGIS_AUTH_MODE: {config.ARCGIS_AUTH_MODE!r}")

    if gis.users.me is None:
        if config.ARCGIS_AUTH_MODE == "profile":
            hint = (
                f", ARCGIS_PROFILE={config.ARCGIS_PROFILE!r} -- that saved profile's token is "
                "likely missing, expired, or was never created successfully. Recreate it (see "
                "Joint-Use-Permits-REST's README)."
            )
        elif config.ARCGIS_AUTH_MODE == "credentials":
            hint = (
                f" -- ARCGIS_URL={config.ARCGIS_URL!r}, ARCGIS_USERNAME={config.ARCGIS_USERNAME!r}. "
                "Check the username/password and that this account isn't locked or disabled."
            )
        else:
            hint = " -- ArcGIS Pro may not actually be signed in right now."

        raise GisConnectionError(
            f"Connected to {gis.url} but authenticated as nobody (anonymous session). "
            f"ARCGIS_AUTH_MODE={config.ARCGIS_AUTH_MODE!r}{hint}"
        )

    return gis
