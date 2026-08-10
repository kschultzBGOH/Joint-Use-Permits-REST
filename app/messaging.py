"""Contractor <-> city messaging, backing Joint-Use-External.

A contractor has no Portal account, so this is the ONLY path they have
into the JointUsePermits service -- every read and write here is scoped
to whichever contractor a valid access token resolves to (see
_resolve_contractor), unlike permit_creation.py's functions, which the
internal Joint-Use-Permits widget calls with its own trusted, direct
ArcGIS connection instead.

Messages has no relationship class to WorkAreas (see Joint-Use-Permits/
scripts/create_layers.py's MESSAGES_TABLE_ID comment) -- threads are
grouped by the plain `threadid` attribute, and tied to a permit (once one
exists) by the plain `permit_globalid` attribute, both queried directly
rather than through queryRelatedFeatures.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from arcgis.features import Table
from arcgis.gis import GIS

from . import config, email_service
from .gis_connection import get_gis
from .permit_creation import get_layer, get_table, resolve_fields

logger = logging.getLogger(__name__)

MESSAGE_FIELDS = {
    "objectid": "objectid",
    "globalid": "globalid",
    "threadid": "threadid",
    "permit_globalid": "permit_globalid",
    "contractor_name": "contractor_name",
    "sender_type": "sender_type",
    "sender_name": "sender_name",
    "body": "body",
    "read_by_city": "read_by_city",
    "read_by_contractor": "read_by_contractor",
}

CONTRACTOR_LOOKUP_FIELDS = {
    "name": "name",
    "email": "email",
    "accesstoken": "accesstoken",
}

WORK_AREA_LOOKUP_FIELDS = {
    "globalid": "globalid",
    "permit_number": "permit_number",
}

SENDER_CITY = "City"
SENDER_CONTRACTOR = "Contractor"
YES = "Yes"
NO = "No"


class MessagingError(RuntimeError):
    """Raised for a bad token, an unknown thread, or a thread that doesn't
    belong to the token presented -- callers turn this into a 4xx, not a
    500, since it's a client mistake, not a server one."""


def _get_messages_table(gis: GIS) -> Table:
    return get_table(gis, config.SERVICE_ITEM_ID, config.MESSAGES_TABLE_INDEX)


def _get_contractors_table(gis: GIS) -> Table:
    return get_table(gis, config.SERVICE_ITEM_ID, config.CONTRACTORS_TABLE_INDEX)


def _resolve_contractor(gis: GIS, token: str) -> tuple[str, str]:
    """Looks up which contractor a token belongs to. Raises MessagingError
    for a blank or unrecognized token -- never returns a partial identity."""

    token = (token or "").strip()
    if not token:
        raise MessagingError("An access token is required.")

    table = _get_contractors_table(gis)
    fields = resolve_fields(table, CONTRACTOR_LOOKUP_FIELDS, "Contractors table")

    escaped = token.replace("'", "''")
    result = table.query(
        where=f"{fields['accesstoken']} = '{escaped}'",
        out_fields=f"{fields['name']},{fields['email']}",
        return_geometry=False,
    )
    if not result.features:
        raise MessagingError("Invalid access token.")

    attrs = result.features[0].attributes
    name = attrs.get(fields["name"])
    if not name:
        raise MessagingError("This contractor has no name on file.")
    return str(name), str(attrs.get(fields["email"]) or "")


def _guid_where(field: str, value: str) -> str:
    """GUID fields come back brace-wrapped from some endpoints and bare
    from others (see Joint-Use-Permits' permitLayerService.ts, which hits
    the same inconsistency querying permit_globalid) -- match either."""

    bare = str(value).strip("{}")
    escaped = bare.replace("'", "''")
    return f"{field} = '{{{escaped}}}' OR {field} = '{escaped}'"


def _query_thread(table: Table, fields: dict[str, str], thread_id: str) -> list[Any]:
    result = table.query(
        where=_guid_where(fields["threadid"], thread_id),
        out_fields="*",
        return_geometry=False,
        # Ordered by OBJECTID, not the editor-tracking creation date field --
        # that field is unreliable on this service (see
        # permitLayerService.ts's resolveCreatedDateField), while OBJECTID
        # is always populated and always increases with insert order.
        order_by_fields=f"{fields['objectid']} ASC",
    )
    return result.features


def _require_own_thread(rows: list[Any], fields: dict[str, str], contractor_name: str) -> None:
    if not rows:
        raise MessagingError("That thread was not found.")
    if not any(str(row.attributes.get(fields["contractor_name"])) == contractor_name for row in rows):
        raise MessagingError("That thread doesn't belong to your access token.")


def _thread_permit_globalid(rows: list[Any], fields: dict[str, str]) -> str | None:
    for row in rows:
        value = row.attributes.get(fields["permit_globalid"])
        if value:
            return str(value)
    return None


def _lookup_permit_numbers(gis: GIS, permit_globalids: list[str]) -> dict[str, str]:
    unique = [g for g in dict.fromkeys(permit_globalids) if g]
    if not unique:
        return {}

    layer = get_layer(gis, config.SERVICE_ITEM_ID, config.WORK_AREAS_LAYER_INDEX)
    fields = resolve_fields(layer, WORK_AREA_LOOKUP_FIELDS, "WorkAreas layer")
    where = " OR ".join(_guid_where(fields["globalid"], g) for g in unique)
    result = layer.query(where=where, out_fields=f"{fields['globalid']},{fields['permit_number']}", return_geometry=False)
    return {
        str(feature.attributes.get(fields["globalid"])).strip("{}"): feature.attributes.get(fields["permit_number"])
        for feature in result.features
    }


def _attach_file(table: Table, object_id: int, path: Path) -> None:
    result = table.attachments.add(object_id, str(path))
    add_result = result.get("addAttachmentResult", {})
    if not add_result.get("success"):
        raise MessagingError(f"Failed to attach {path.name}: {add_result.get('error') or result}")


def start_thread(token: str, body: str, attachment_path: Path | None) -> dict:
    """A contractor's first message, with no permit yet -- see
    Joint-Use-Permits' "Messages" tab for how city staff turn this into a
    real work order."""

    if not body.strip():
        raise MessagingError("A message body is required.")

    gis = get_gis()
    contractor_name, _ = _resolve_contractor(gis, token)
    table = _get_messages_table(gis)
    fields = resolve_fields(table, MESSAGE_FIELDS, "Messages table")

    add_result = table.edit_features(
        adds=[
            {
                "attributes": {
                    fields["contractor_name"]: contractor_name,
                    fields["sender_type"]: SENDER_CONTRACTOR,
                    fields["sender_name"]: contractor_name,
                    fields["body"]: body,
                    fields["read_by_city"]: NO,
                    fields["read_by_contractor"]: YES,
                }
            }
        ]
    )
    add_row = add_result["addResults"][0]
    if not add_row.get("success"):
        raise MessagingError(f"Failed to start a new thread: {add_row.get('error')}")

    object_id = add_row["objectId"]
    global_id = add_row["globalId"]

    # threadid is the root message's own globalid -- can't be known until
    # after the add above assigns one, so this is a second, small write
    # rather than something set in the same call.
    update_result = table.edit_features(
        updates=[{"attributes": {fields["objectid"]: object_id, fields["threadid"]: global_id}}]
    )
    update_row = update_result["updateResults"][0]
    if not update_row.get("success"):
        raise MessagingError(f"Failed to finish starting the thread: {update_row.get('error')}")

    if attachment_path is not None:
        _attach_file(table, object_id, attachment_path)

    email_service.send_message_notification(
        config.CITY_NOTIFICATION_EMAIL, contractor_name, body, permit_number=None
    )

    logger.info("Contractor %s started thread %s.", contractor_name, global_id)
    return {"threadId": global_id, "messageObjectId": object_id}


def reply_to_thread(token: str, thread_id: str, body: str, attachment_path: Path | None) -> dict:
    if not body.strip():
        raise MessagingError("A message body is required.")

    gis = get_gis()
    contractor_name, _ = _resolve_contractor(gis, token)
    table = _get_messages_table(gis)
    fields = resolve_fields(table, MESSAGE_FIELDS, "Messages table")

    rows = _query_thread(table, fields, thread_id)
    _require_own_thread(rows, fields, contractor_name)
    permit_globalid = _thread_permit_globalid(rows, fields)

    add_result = table.edit_features(
        adds=[
            {
                "attributes": {
                    fields["threadid"]: thread_id,
                    fields["permit_globalid"]: permit_globalid,
                    fields["contractor_name"]: contractor_name,
                    fields["sender_type"]: SENDER_CONTRACTOR,
                    fields["sender_name"]: contractor_name,
                    fields["body"]: body,
                    fields["read_by_city"]: NO,
                    fields["read_by_contractor"]: YES,
                }
            }
        ]
    )
    add_row = add_result["addResults"][0]
    if not add_row.get("success"):
        raise MessagingError(f"Failed to send reply: {add_row.get('error')}")

    object_id = add_row["objectId"]
    if attachment_path is not None:
        _attach_file(table, object_id, attachment_path)

    permit_number = _lookup_permit_numbers(gis, [permit_globalid]).get((permit_globalid or "").strip("{}"))
    email_service.send_message_notification(config.CITY_NOTIFICATION_EMAIL, contractor_name, body, permit_number)

    logger.info("Contractor %s replied on thread %s.", contractor_name, thread_id)
    return {"messageObjectId": object_id}


def _serialize_message(row: Any, fields: dict[str, str]) -> dict:
    attrs = row.attributes
    return {
        "objectId": attrs.get(fields["objectid"]),
        "senderType": attrs.get(fields["sender_type"]),
        "senderName": attrs.get(fields["sender_name"]),
        "body": attrs.get(fields["body"]),
    }


def list_threads(token: str) -> list[dict]:
    """This contractor's own threads only -- never every thread on the
    service, which would leak other contractors' conversations to anyone
    holding any valid token."""

    gis = get_gis()
    contractor_name, _ = _resolve_contractor(gis, token)
    table = _get_messages_table(gis)
    fields = resolve_fields(table, MESSAGE_FIELDS, "Messages table")

    escaped = contractor_name.replace("'", "''")
    result = table.query(
        where=f"{fields['contractor_name']} = '{escaped}'",
        out_fields="*",
        return_geometry=False,
        order_by_fields=f"{fields['objectid']} ASC",
    )

    threads: dict[str, list[Any]] = {}
    for row in result.features:
        thread_id = str(row.attributes.get(fields["threadid"]))
        threads.setdefault(thread_id, []).append(row)

    permit_globalids = [_thread_permit_globalid(rows, fields) for rows in threads.values()]
    permit_numbers = _lookup_permit_numbers(gis, [g for g in permit_globalids if g])

    summaries = []
    for thread_id, rows in threads.items():
        # rows are already OBJECTID-ascending (the query above is), so the
        # last one is this thread's most recent message.
        last = rows[-1]
        permit_globalid = _thread_permit_globalid(rows, fields)
        unread = sum(
            1
            for row in rows
            if row.attributes.get(fields["sender_type"]) == SENDER_CITY
            and row.attributes.get(fields["read_by_contractor"]) != YES
        )
        summaries.append(
            {
                "threadId": thread_id,
                "permitGlobalId": permit_globalid,
                "permitNumber": permit_numbers.get((permit_globalid or "").strip("{}")),
                "lastMessage": last.attributes.get(fields["body"]),
                "lastMessageFrom": last.attributes.get(fields["sender_type"]),
                "unreadCount": unread,
                "lastMessageObjectId": last.attributes.get(fields["objectid"]),
            }
        )

    # Most recently active thread first -- OBJECTID is a reliable proxy
    # for recency (see _query_thread's ordering comment).
    summaries.sort(key=lambda summary: summary["lastMessageObjectId"], reverse=True)
    for summary in summaries:
        del summary["lastMessageObjectId"]
    return summaries


def get_thread(token: str, thread_id: str) -> dict:
    """Fetching a thread also marks every city message in it read-by-
    contractor -- the same "viewing it is what marks it read" behavior
    Joint-Use-Permits uses for the city side (see its Messages panel)."""

    gis = get_gis()
    contractor_name, _ = _resolve_contractor(gis, token)
    table = _get_messages_table(gis)
    fields = resolve_fields(table, MESSAGE_FIELDS, "Messages table")

    rows = _query_thread(table, fields, thread_id)
    _require_own_thread(rows, fields, contractor_name)

    unread_city_messages = [
        row
        for row in rows
        if row.attributes.get(fields["sender_type"]) == SENDER_CITY
        and row.attributes.get(fields["read_by_contractor"]) != YES
    ]
    if unread_city_messages:
        table.edit_features(
            updates=[
                {"attributes": {fields["objectid"]: row.attributes[fields["objectid"]], fields["read_by_contractor"]: YES}}
                for row in unread_city_messages
            ]
        )

    permit_globalid = _thread_permit_globalid(rows, fields)
    permit_number = _lookup_permit_numbers(gis, [permit_globalid]).get((permit_globalid or "").strip("{}"))

    return {
        "threadId": thread_id,
        "permitGlobalId": permit_globalid,
        "permitNumber": permit_number,
        "messages": [_serialize_message(row, fields) for row in rows],
    }


def list_attachments(token: str, thread_id: str, message_object_id: int) -> list[dict]:
    gis = get_gis()
    contractor_name, _ = _resolve_contractor(gis, token)
    table = _get_messages_table(gis)
    fields = resolve_fields(table, MESSAGE_FIELDS, "Messages table")

    rows = _query_thread(table, fields, thread_id)
    _require_own_thread(rows, fields, contractor_name)
    if not any(row.attributes.get(fields["objectid"]) == message_object_id for row in rows):
        raise MessagingError("That message was not found in this thread.")

    attachments = table.attachments.get_list(message_object_id)
    return [
        {"id": a["id"], "name": a["name"], "size": a.get("size"), "contentType": a.get("contentType")}
        for a in attachments
    ]


def download_attachment(token: str, thread_id: str, message_object_id: int, attachment_id: int, dest_dir: Path) -> Path:
    gis = get_gis()
    contractor_name, _ = _resolve_contractor(gis, token)
    table = _get_messages_table(gis)
    fields = resolve_fields(table, MESSAGE_FIELDS, "Messages table")

    rows = _query_thread(table, fields, thread_id)
    _require_own_thread(rows, fields, contractor_name)
    if not any(row.attributes.get(fields["objectid"]) == message_object_id for row in rows):
        raise MessagingError("That message was not found in this thread.")

    dest_dir.mkdir(parents=True, exist_ok=True)
    paths = table.attachments.download(oid=message_object_id, attachment_id=attachment_id, save_path=str(dest_dir))
    if not paths:
        raise MessagingError(f"Attachment {attachment_id} was not found on message {message_object_id}.")
    return Path(paths[0])


def new_local_id() -> str:
    """Unique-enough directory name for staging an uploaded attachment
    before it's attached -- not a GUID the service ever sees."""

    return uuid.uuid4().hex
