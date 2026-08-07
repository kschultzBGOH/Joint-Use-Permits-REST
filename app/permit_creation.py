"""Creates a new permit (the WorkAreas layer) and its related poles (the
Poles layer) from a pole discovery result (see app/discovery/).

Both layers live in one hosted feature service with a real one-to-many
relationship between them; poles reference their permit's globalid via
permit_globalid. Joint-Use-Permits/scripts/create_layers.py is the source
of truth for both schemas.

Field names below are the expected names; the actual names are resolved
case-insensitively from the live layer at runtime (see resolve_fields).
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from arcgis.features import FeatureLayer, Table
from arcgis.gis import GIS, Item

from . import config
from .gis_connection import get_gis
from .job_store import CreatedPermit
from .work_area import build_work_area_polygon

logger = logging.getLogger(__name__)

# Guards the permit-number sequence table's read-increment-write below --
# without it, two permits created at nearly the same moment (two upload
# jobs finishing close together, or a job finishing while someone clicks
# "skip upload") could both read the same last-issued number and save the
# same "next" one, handing out a duplicate permit number. Only protects
# against a race within this one process; see job_store.py's docstring
# about this service assuming a single worker process.
_permit_number_lock = threading.Lock()

PERMIT_NUMBER_SEQUENCE_FIELDS = {
    "objectid": "objectid",
    "year": "year",
    "number": "number",
}

WORK_AREA_FIELDS = {
    "permit_number": "permit_number",
    "totalcitypolecount": "totalcitypolecount",
    "foreignpolecount": "foreignpolecount",
    "polereviewstatus": "polereviewstatus",
}

POLE_FIELDS = {
    "permit_number": "permit_number",
    "permit_globalid": "permit_globalid",
    "pole_id": "pole_id",
    "pole_owner": "pole_owner",
    "confidence": "confidence",
    "status": "status",
}

STATUS_APPROVED = "Approved"
STATUS_NEEDS_REVIEW = "Needs Review"

#: Every new permit starts here, regardless of individual pole confidence
#: -- a human explicitly marks a permit "Review of Poles Complete" (the
#: widget's Under Review tab) rather than it silently qualifying just
#: because no single pole happened to score below the review threshold.
PERMIT_REVIEW_STATUS_NEEDS_REVIEW = "Needs Review"


def resolve_pole_owner(pole_id: str) -> str:
    """City-owned pole IDs never end in 'F'; foreign-owned ones always do
    (see candidate_resolver.py's terminal-F handling, which already treats
    that suffix as meaningful when correcting OCR readings)."""

    return "Foreign" if pole_id.strip().upper().endswith("F") else "City"


def resolve_pole_status(confidence: float) -> str:
    return STATUS_APPROVED if confidence >= config.POLE_REVIEW_CONFIDENCE_THRESHOLD else STATUS_NEEDS_REVIEW


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


def _get_item(gis: GIS, item_id: str) -> Item:
    """Fetches the service item, retrying once with a brand-new connection
    if the first attempt fails on permissions.

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

    return item


def get_layer(gis: GIS, item_id: str, layer_index: int) -> FeatureLayer:
    """Fetches one spatial layer of the service (see _get_item)."""

    item = _get_item(gis, item_id)

    layers = item.layers
    if layer_index >= len(layers):
        raise PermitCreationError(
            f"Service {item_id} has no layer at index {layer_index} "
            f"(it has {len(layers)} layer(s): "
            f"{', '.join(str(layer.properties.name) for layer in layers)}). "
            f"Check WORK_AREAS_LAYER_INDEX / POLES_LAYER_INDEX."
        )

    layer = layers[layer_index]
    logger.info(
        "Fetched %s layer %s (%s) as %s (%s).",
        item_id,
        layer_index,
        layer.properties.name,
        _identity(gis),
        _describe_item(item),
    )
    return layer


def get_table(gis: GIS, item_id: str, table_index: int) -> Table:
    """Fetches one non-spatial table of the service (see _get_item).

    A table is a separate collection from the service's spatial layers
    (item.tables, not item.layers) -- table_index is this table's
    position within THAT collection, not its combined layer+table id in
    the service's own numbering (see config.py's comment on this).
    """

    item = _get_item(gis, item_id)

    tables = item.tables
    if table_index >= len(tables):
        raise PermitCreationError(
            f"Service {item_id} has no table at index {table_index} "
            f"(it has {len(tables)} table(s): "
            f"{', '.join(str(table.properties.name) for table in tables)}). "
            f"Check PERMIT_NUMBER_SEQUENCE_TABLE_INDEX."
        )

    table = tables[table_index]
    logger.info(
        "Fetched %s table %s (%s) as %s (%s).",
        item_id,
        table_index,
        table.properties.name,
        _identity(gis),
        _describe_item(item),
    )
    return table


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


def generate_permit_number(gis: GIS, year: int) -> str:
    """Sequential-per-year permit number, e.g. "1338-2026".

    Backed by the PermitNumberSequence table (see Joint-Use-Permits/
    scripts/create_layers.py) rather than scanning WorkAreas for the
    highest existing permit_number -- that couldn't be seeded ahead of
    time, so a fresh deployment (or one that needs to match wherever a
    legacy/manual numbering scheme currently stands) had no way to start
    anywhere but 1. This table can be opened and edited directly in
    Portal before going live: set `number` for the current year to
    wherever numbering actually left off, and the next permit created
    continues from number + 1.

    Read-increment-write is wrapped in _permit_number_lock so two permits
    created at nearly the same moment can't both read the same last-issued
    number and hand out a duplicate (see the lock's module-level comment).
    """

    with _permit_number_lock:
        table = get_table(
            gis, config.SERVICE_ITEM_ID, config.PERMIT_NUMBER_SEQUENCE_TABLE_INDEX
        )
        fields = resolve_fields(
            table, PERMIT_NUMBER_SEQUENCE_FIELDS, "PermitNumberSequence table"
        )

        result = table.query(
            where=f"{fields['year']} = {year}",
            out_fields=f"{fields['objectid']},{fields['number']}",
            return_geometry=False,
        )

        if result.features:
            row = result.features[0]
            object_id = row.attributes[fields["objectid"]]
            current_number = int(row.attributes[fields["number"]] or 0)
        else:
            add_result = table.edit_features(
                adds=[{"attributes": {fields["year"]: year, fields["number"]: 0}}]
            )
            add_row = add_result["addResults"][0]
            if not add_row.get("success"):
                raise PermitCreationError(
                    f"Failed to create the permit number sequence row for {year}: "
                    f"{add_row.get('error')}"
                )
            object_id = add_row["objectId"]
            current_number = 0

        next_number = current_number + 1
        update_result = table.edit_features(
            updates=[
                {"attributes": {fields["objectid"]: object_id, fields["number"]: next_number}}
            ]
        )
        update_row = update_result["updateResults"][0]
        if not update_row.get("success"):
            raise PermitCreationError(
                f"Failed to update the permit number sequence for {year}: "
                f"{update_row.get('error')}"
            )

    return f"{next_number}-{year}"


def build_work_area_geometry(valid_poles: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Work-area polygon covering the discovered poles (see work_area.py).

    Returns None when there's nothing to build one from -- rather than
    failing permit creation outright, the permit is created shape-less
    and the Joint Use Permits widget's "Define Project Scope" screen lets
    a human add poles by hand from there (search, or click-to-add on the
    linked map). That screen already rebuilds the work area from whatever
    poles actually exist on every add/approve/reject
    (regeneratePermitWorkArea); zero discovered poles is just the
    starting point for that, not a dead end.
    """

    if not valid_poles:
        return None

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


def attach_plan_set(work_areas_layer: FeatureLayer, object_id: int, pdf_path: Path) -> None:
    """Attaches the uploaded plan set PDF to the permit's WorkAreas feature.

    This is the same PDF pole discovery just ran against -- attaching it to
    the polygon it produced gives whoever opens the permit later the
    original source document, not just the derived geometry and poles.
    """

    result = work_areas_layer.attachments.add(object_id, str(pdf_path))
    add_result = result.get("addAttachmentResult", {})
    if not add_result.get("success"):
        raise PermitCreationError(
            f"Failed to attach {pdf_path.name} to permit: {add_result.get('error') or result}"
        )


def create_permit_and_poles(
    discovery_result: dict[str, Any], pdf_path: Path | None
) -> CreatedPermit:
    """Connects fresh (right here, right before use) and creates the permit,
    its poles, and (if there is one) attaches the source plan set PDF to the
    permit feature. Discovery can take several minutes (Qwen load +
    inference) before this ever runs -- connecting at the last possible
    moment, rather than earlier and holding onto that connection, avoids
    using a connection that's gone stale by the time it's actually needed.

    `pdf_path` is None for a permit created with no plan set at all (see
    the /permits endpoint in main.py) -- someone with nothing to upload
    defining the project's scope by hand from the start, rather than being
    forced through a PDF upload first. Nothing here treats a PDF as
    required; discovery_result's own poles (possibly empty) are what
    actually drive pole/geometry creation either way.
    """

    gis = get_gis()
    logger.info("Connected as %s for permit creation.", _identity(gis))

    accepted_poles = discovery_result.get("accepted_poles", [])
    valid_poles = [
        pole for pole in accepted_poles if pole.get("x") is not None and pole.get("y") is not None
    ]

    if not config.SERVICE_ITEM_ID:
        raise PermitCreationError(
            "SERVICE_ITEM_ID is not configured. Run Joint-Use-Permits' "
            "scripts/create_layers.py and put the item ID it prints in .env."
        )

    work_areas_layer = get_layer(
        gis, config.SERVICE_ITEM_ID, config.WORK_AREAS_LAYER_INDEX
    )
    poles_layer = get_layer(gis, config.SERVICE_ITEM_ID, config.POLES_LAYER_INDEX)

    work_area_fields = resolve_fields(work_areas_layer, WORK_AREA_FIELDS, "WorkAreas layer")
    pole_fields = resolve_fields(poles_layer, POLE_FIELDS, "Poles layer")

    geometry = build_work_area_geometry(valid_poles)
    year = datetime.now(timezone.utc).year
    permit_number = generate_permit_number(gis, year)

    pole_owners = [resolve_pole_owner(pole["pole_id"]) for pole in valid_poles]
    total_city_pole_count = sum(1 for owner in pole_owners if owner == "City")
    foreign_pole_count = sum(1 for owner in pole_owners if owner == "Foreign")

    new_work_area: dict[str, Any] = {
        "attributes": {
            work_area_fields["permit_number"]: permit_number,
            work_area_fields["totalcitypolecount"]: total_city_pole_count,
            work_area_fields["foreignpolecount"]: foreign_pole_count,
            work_area_fields["polereviewstatus"]: PERMIT_REVIEW_STATUS_NEEDS_REVIEW,
        },
    }
    # Omitted rather than sent as null -- a shape-less permit is a valid,
    # intentional state (see build_work_area_geometry), and some hosted
    # layers are pickier about an explicit null geometry than about the
    # key being absent entirely.
    if geometry is not None:
        new_work_area["geometry"] = geometry

    add_result = work_areas_layer.edit_features(adds=[new_work_area])
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
                    pole_fields["pole_owner"]: owner,
                    pole_fields["confidence"]: pole.get("confidence", 0.0),
                    pole_fields["status"]: resolve_pole_status(pole.get("confidence", 0.0)),
                },
            }
            for pole, owner in zip(valid_poles, pole_owners)
        ]

        pole_result = poles_layer.edit_features(adds=pole_adds)
        failures = [r for r in pole_result["addResults"] if not r.get("success")]
        if failures:
            raise PermitCreationError(
                f"Created permit {permit_number} but failed to create "
                f"{len(failures)} of {len(valid_poles)} pole(s): {failures[0].get('error')}"
            )

    if pdf_path is not None:
        try:
            attach_plan_set(work_areas_layer, permit_object_id, pdf_path)
        except PermitCreationError as exc:
            raise PermitCreationError(
                f"Created permit {permit_number} with {len(valid_poles)} pole(s), but {exc}"
            ) from exc

    return CreatedPermit(
        object_id=permit_object_id,
        global_id=permit_global_id,
        permit_number=permit_number,
        pole_count=len(valid_poles),
    )
