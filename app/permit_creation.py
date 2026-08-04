"""Creates a new permit (JointUsePermits_WorkAreas) and its poles
(JointUsePermits_Poles) from a pole discovery result (see app/discovery/).

Field names here must match scripts/create_layers.py in the
Joint-Use-Permits repo -- that script is the source of truth for both
layers' schemas.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from arcgis.features import FeatureLayer
from arcgis.geometry import buffer as geometry_buffer
from arcgis.gis import GIS

from . import config
from .job_store import CreatedPermit

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


def get_layer(gis: GIS, item_id: str) -> FeatureLayer:
    item = gis.content.get(item_id)
    if item is None:
        raise PermitCreationError(f"Layer item {item_id} was not found.")
    return item.layers[0]


def generate_permit_number(work_areas_layer: FeatureLayer) -> str:
    """Sequential-per-year permit number, e.g. "1338-2026".

    Scoped to JointUsePermits_WorkAreas' own PERMIT_NUMBER values -- this
    does NOT coordinate with the original (unconfirmed-schema) Joint Use
    Permits layer's numbering. If both layers need one shared sequence,
    this needs to query that layer too.
    """

    year = datetime.now(timezone.utc).year
    field = WORK_AREA_FIELDS["permit_number"]

    result = work_areas_layer.query(
        where=f"{field} LIKE '%-{year}'",
        out_fields=field,
        return_geometry=False,
    )

    max_sequence = 0
    for feature in result.features:
        value = str(feature.attributes.get(field, ""))
        prefix = value.split("-")[0]
        if prefix.isdigit():
            max_sequence = max(max_sequence, int(prefix))

    return f"{max_sequence + 1}-{year}"


def build_work_area_geometry(gis: GIS, valid_poles: list[dict[str, Any]]) -> dict[str, Any]:
    """Buffers a multipoint of the discovered pole locations into a polygon.

    Buffering the raw multipoint (rather than a convex hull) works for any
    pole count, including 1 or 2, without special-casing degenerate shapes.
    """

    if not valid_poles:
        raise PermitCreationError(
            "No poles with coordinates were discovered; a work area cannot be created."
        )

    multipoint = {
        "points": [[pole["x"], pole["y"]] for pole in valid_poles],
        "spatialReference": {"wkid": config.POLE_COORDINATE_WKID},
    }

    buffered = geometry_buffer(
        geometries=[multipoint],
        in_sr=config.POLE_COORDINATE_WKID,
        distances=[config.WORK_AREA_BUFFER_FEET],
        unit=9002,  # esriSRUnit_Foot -- POLE_COORDINATE_WKID's linear unit is feet
        out_sr=config.POLE_COORDINATE_WKID,
        union_results=True,
        gis=gis,
    )

    if not buffered:
        raise PermitCreationError("The geometry service did not return a buffered polygon.")

    return buffered[0]


def create_permit_and_poles(
    gis: GIS, discovery_result: dict[str, Any]
) -> CreatedPermit:
    accepted_poles = discovery_result.get("accepted_poles", [])
    valid_poles = [
        pole for pole in accepted_poles if pole.get("x") is not None and pole.get("y") is not None
    ]

    work_areas_layer = get_layer(gis, config.WORK_AREAS_LAYER_ITEM_ID)
    poles_layer = get_layer(gis, config.POLES_LAYER_ITEM_ID)

    geometry = build_work_area_geometry(gis, valid_poles)
    permit_number = generate_permit_number(work_areas_layer)

    add_result = work_areas_layer.edit_features(
        adds=[
            {
                "geometry": geometry,
                "attributes": {WORK_AREA_FIELDS["permit_number"]: permit_number},
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
                    POLE_FIELDS["permit_globalid"]: permit_global_id,
                    POLE_FIELDS["permit_number"]: permit_number,
                    POLE_FIELDS["pole_id"]: pole["pole_id"],
                    # This pipeline only matches readings against the city's
                    # own authoritative pole catalog, so every pole it
                    # discovers is city-owned. Foreign-owned poles aren't
                    # something it can detect -- that count stays manual,
                    # filled in later on the permit form.
                    POLE_FIELDS["pole_owner"]: "City",
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
