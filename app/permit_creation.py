"""Creates a new permit (JointUsePermits_WorkAreas) and its poles
(JointUsePermits_Poles) from a pole discovery result (see app/discovery/).

Field names here must match scripts/create_layers.py in the
Joint-Use-Permits repo -- that script is the source of truth for both
layers' schemas.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from arcgis.features import FeatureLayer
from arcgis.gis import GIS, Item

from . import config
from .gis_connection import get_gis
from .job_store import CreatedPermit
from .work_area import build_work_area_polygon

logger = logging.getLogger(__name__)

WORK_AREA_FIELDS = {
    "permit_number": "PERMIT_NUMBER",
    "permit_globalid": "PERMIT_GLOBALID",
}

POLE_FIELDS = {
    "permit_number": "PERMIT_NUMBER",
    "permit_globalid": "PERMIT_GLOBALID",
    "pole_id": "POLE_ID",
    "pole_owner": "POLE_OWNER",
}


class PermitCreationError(RuntimeError):
    """Raised when the permit or its poles can't be created."""


def _identity(gis: GIS) -> str:
    try:
        return f"{gis.users.me.username}@{gis.properties.portalHostname}"
    except Exception:
        return "unknown identity"


def _describe_item(item: Item) -> str:
    try:
        return f"owner={item.owner}, access={item.access}"
    except Exception:
        return "item details unavailable"


def get_layer(gis: GIS, item_id: str) -> FeatureLayer:
    """Fetches a layer's first sublayer, retrying once with a brand-new
    connection if the first attempt fails on permissions.

    A permission failure here is surprising -- these items are meant to be
    readable by this service's own account -- so this doesn't silently
    swallow it: it retries with a guaranteed-fresh connection (ruling out
    a stale/expired token) and, if that still fails, raises an error that
    names exactly who connected and what the item's actual sharing state
    is, so the next failure is diagnosable from the server log alone.
    """

    try:
        item = gis.content.get(item_id)
    except Exception as exc:
        if "permission" not in str(exc).lower():
            raise

        logger.warning(
            "content.get(%s) failed as %s (%s) -- retrying with a fresh connection.",
            item_id,
            _identity(gis),
            exc,
        )
        fresh_gis = get_gis()

        try:
            item = fresh_gis.content.get(item_id)
        except Exception as retry_exc:
            raise PermitCreationError(
                f"Could not access layer item {item_id} even after reconnecting. "
                f"Connected as {_identity(fresh_gis)}. "
                f"First error: {exc}. Retry error: {retry_exc}. "
                f"Confirm this account can view the item in Portal "
                f"(My Content -> the item -> Share)."
            ) from retry_exc

    if item is None:
        raise PermitCreationError(f"Layer item {item_id} was not found.")

    logger.info("Fetched item %s as %s (%s).", item_id, _identity(gis), _describe_item(item))
    return item.layers[0]


def resolve_fields(
    layer: FeatureLayer, wanted: dict[str, str], layer_label: str
) -> dict[str, str]:
    """Maps logical field keys to the layer's ACTUAL field names.

    Portal doesn't necessarily preserve the casing a layer was created
    with -- these layers were defined with PERMIT_NUMBER but the hosted
    service reports permit_number. Resolving against the live schema
    case-insensitively means this service works regardless of how Portal
    cased things, instead of breaking on an exact-match string compare.
    """

    try:
        layer_fields = layer.properties.fields
    except Exception as exc:
        raise PermitCreationError(f"Could not read {layer_label}'s field list: {exc}") from exc

    actual_by_lower: dict[str, str] = {}
    for field in layer_fields:
        name = field["name"] if isinstance(field, dict) else getattr(field, "name", None)
        if name:
            actual_by_lower[str(name).lower()] = str(name)

    resolved: dict[str, str] = {}
    missing: list[str] = []
    for key, expected_name in wanted.items():
        actual = actual_by_lower.get(expected_name.lower())
        if actual is None:
            missing.append(expected_name)
        else:
            resolved[key] = actual

    if missing:
        raise PermitCreationError(
            f"{layer_label} is missing expected field(s): {', '.join(sorted(missing))}. "
            f"Available fields: {', '.join(sorted(actual_by_lower.values()))}"
        )

    return resolved


def generate_permit_number(work_areas_layer: FeatureLayer, permit_number_field: str) -> str:
    """Sequential-per-year permit number, e.g. "1338-2026".

    Scoped to JointUsePermits_WorkAreas' own permit numbers -- this does
    NOT coordinate with the original (unconfirmed-schema) Joint Use
    Permits layer's numbering. If both layers need one shared sequence,
    this needs to query that layer too.
    """

    year = datetime.now(timezone.utc).year

    result = work_areas_layer.query(
        where=f"{permit_number_field} LIKE '%-{year}'",
        out_fields=permit_number_field,
        return_geometry=False,
    )

    max_sequence = 0
    for feature in result.features:
        value = str(feature.attributes.get(permit_number_field, ""))
        prefix = value.split("-")[0]
        if prefix.isdigit():
            max_sequence = max(max_sequence, int(prefix))

    return f"{max_sequence + 1}-{year}"


def build_work_area_geometry(valid_poles: list[dict[str, Any]]) -> dict[str, Any]:
    """Work-area polygon covering the discovered poles (see work_area.py)."""

    if not valid_poles:
        raise PermitCreationError(
            "No poles with coordinates were discovered; a work area cannot be created."
        )

    points = [(float(pole["x"]), float(pole["y"])) for pole in valid_poles]
    polygon = build_work_area_polygon(
        points=points,
        buffer_distance=float(config.WORK_AREA_BUFFER_FEET),
        wkid=config.POLE_COORDINATE_WKID,
    )

    logger.info(
        "Built work area from %s pole(s), buffered %sft, %s ring vertices.",
        len(points),
        config.WORK_AREA_BUFFER_FEET,
        len(polygon["rings"][0]),
    )
    return polygon


def create_permit_and_poles(discovery_result: dict[str, Any]) -> CreatedPermit:
    """Connects fresh (right here, right before use) and creates the permit
    + its poles. Discovery can take several minutes (Qwen load + inference)
    before this ever runs -- connecting at the last possible moment, rather
    than earlier and holding onto that connection, avoids using a
    connection that's gone stale by the time it's actually needed.
    """

    gis = get_gis()
    logger.info("Connected as %s for permit creation.", _identity(gis))

    accepted_poles = discovery_result.get("accepted_poles", [])
    valid_poles = [
        pole for pole in accepted_poles if pole.get("x") is not None and pole.get("y") is not None
    ]

    work_areas_layer = get_layer(gis, config.WORK_AREAS_LAYER_ITEM_ID)
    poles_layer = get_layer(gis, config.POLES_LAYER_ITEM_ID)

    work_area_fields = resolve_fields(
        work_areas_layer, WORK_AREA_FIELDS, "JointUsePermits_WorkAreas"
    )
    pole_fields = resolve_fields(poles_layer, POLE_FIELDS, "JointUsePermits_Poles")

    geometry = build_work_area_geometry(valid_poles)
    permit_number = generate_permit_number(work_areas_layer, work_area_fields["permit_number"])

    add_result = work_areas_layer.edit_features(
        adds=[
            {
                "geometry": geometry,
                "attributes": {work_area_fields["permit_number"]: permit_number},
            }
        ]
    )
    permit_add = add_result["addResults"][0]
    if not permit_add.get("success"):
        raise PermitCreationError(f"Failed to create permit: {permit_add.get('error')}")

    permit_object_id = permit_add["objectId"]
    permit_global_id = permit_add["globalId"]

    if valid_poles:
        pole_adds = [
            {
                "geometry": {
                    "x": pole["x"],
                    "y": pole["y"],
                    "spatialReference": {"wkid": config.POLE_COORDINATE_WKID},
                },
                "attributes": {
                    pole_fields["permit_globalid"]: permit_global_id,
                    pole_fields["permit_number"]: permit_number,
                    pole_fields["pole_id"]: pole["pole_id"],
                    # This pipeline only matches readings against the city's
                    # own authoritative pole catalog, so every pole it
                    # discovers is city-owned. Foreign-owned poles aren't
                    # something it can detect -- that count stays manual,
                    # filled in later on the permit form.
                    pole_fields["pole_owner"]: "City",
                },
            }
            for pole in valid_poles
        ]

        pole_result = poles_layer.edit_features(adds=pole_adds)
        failures = [r for r in pole_result["addResults"] if not r.get("success")]
        if failures:
            raise PermitCreationError(
                f"Created permit {permit_number} but failed to create "
                f"{len(failures)} of {len(valid_poles)} pole(s): {failures[0].get('error')}"
            )

    return CreatedPermit(
        object_id=permit_object_id,
        global_id=permit_global_id,
        permit_number=permit_number,
        pole_count=len(valid_poles),
    )
