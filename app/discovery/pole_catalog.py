"""Load and index the authoritative electric-pole reference catalog.

Reads pole records from a SQLite database the city maintains independently
of this service (column/table names are configurable, see .env.example),
then indexes them for fast exact-match lookup. Duplicate pole IDs and
duplicate source IDs are preserved rather than silently collapsed. This
module intentionally does not perform fuzzy or OCR-character matching --
that lives in candidate_resolver.py.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from .. import config

logger = logging.getLogger(__name__)


class PoleRecord(TypedDict):
    """One pole loaded from the authoritative reference database."""

    source_id: str
    pole_id_raw: str
    pole_id_normalized: str
    x: float | None
    y: float | None
    status: str | None
    source_updated_at: str | None


class PoleCatalogSummary(TypedDict):
    record_count: int
    unique_pole_id_count: int
    duplicate_pole_id_count: int
    duplicate_source_id_count: int


class ExactMatchResult(TypedDict):
    candidate_raw: str
    candidate_normalized: str
    match_status: str
    match_count: int
    records: list[PoleRecord]


class PoleCatalogError(RuntimeError):
    """Raised when the pole reference database or its records are invalid."""


def normalize_pole_id(pole_id: str) -> str:
    """Normalize safe formatting differences without guessing characters."""

    return "".join(pole_id.upper().split())


@dataclass(frozen=True, slots=True)
class PoleSourceConfig:
    database_path: str | Path
    table: str
    pole_id_column: str
    source_id_column: str
    x_column: str | None = None
    y_column: str | None = None
    status_column: str | None = None
    updated_at_column: str | None = None


def load_pole_records(source_config: PoleSourceConfig) -> list[Mapping[str, object]]:
    """Load raw pole records from SQLite using the configured column mapping."""

    database_path = Path(source_config.database_path).expanduser().resolve()

    if not database_path.exists():
        raise PoleCatalogError(f"Pole reference database does not exist: {database_path}")

    connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row

    try:
        _validate_configured_columns(connection, source_config)

        query = _build_select_query(source_config)
        cursor = connection.execute(query)

        records: list[Mapping[str, object]] = []
        for row in cursor:
            pole_id_raw = _optional_text(row["pole_id_raw"])
            if pole_id_raw is None:
                continue

            source_id = _optional_text(row["source_id"])
            if source_id is None:
                raise PoleCatalogError(
                    "A pole record has a blank source ID. Every loaded pole "
                    "must have a stable source ID."
                )

            records.append(
                {
                    "source_id": source_id,
                    "pole_id_raw": pole_id_raw,
                    "x": _optional_float(row["x"]),
                    "y": _optional_float(row["y"]),
                    "status": _optional_text(row["status"]),
                    "source_updated_at": _optional_text(row["source_updated_at"]),
                }
            )

        return records

    except sqlite3.Error as exc:
        raise PoleCatalogError(
            f"SQLite failed while loading pole records from {database_path}: {exc}"
        ) from exc
    finally:
        connection.close()


class PoleCatalog:
    """Fast, read-only-style indexes over authoritative pole records."""

    def __init__(self, records: Iterable[Mapping[str, object]]) -> None:
        validated_records: list[PoleRecord] = []
        records_by_pole_id: defaultdict[str, list[PoleRecord]] = defaultdict(list)
        records_by_source_id: defaultdict[str, list[PoleRecord]] = defaultdict(list)

        for record in records:
            validated_record = _validate_record(record)
            validated_records.append(validated_record)
            records_by_pole_id[validated_record["pole_id_normalized"]].append(validated_record)
            records_by_source_id[validated_record["source_id"]].append(validated_record)

        self._records = tuple(validated_records)
        self._records_by_pole_id = {
            pole_id: tuple(matches) for pole_id, matches in records_by_pole_id.items()
        }
        self._records_by_source_id = {
            source_id: tuple(matches) for source_id, matches in records_by_source_id.items()
        }
        self._all_ids = frozenset(self._records_by_pole_id)
        self._duplicate_ids = tuple(
            sorted(pole_id for pole_id, matches in self._records_by_pole_id.items() if len(matches) > 1)
        )
        self._duplicate_source_ids = tuple(
            sorted(
                source_id
                for source_id, matches in self._records_by_source_id.items()
                if len(matches) > 1
            )
        )

    @classmethod
    def load(cls) -> "PoleCatalog":
        source_config = PoleSourceConfig(
            database_path=config.POLE_DB_PATH,
            table=config.POLE_TABLE,
            pole_id_column=config.POLE_ID_COLUMN,
            source_id_column=config.POLE_SOURCE_ID_COLUMN,
            x_column=config.POLE_X_COLUMN,
            y_column=config.POLE_Y_COLUMN,
        )
        catalog = cls(load_pole_records(source_config))

        with_coordinates = sum(
            1 for record in catalog.records if record["x"] is not None and record["y"] is not None
        )
        logger.info(
            "Loaded pole catalog from %s (%s.%s): %s records, %s unique IDs, "
            "%s with usable coordinates.",
            config.POLE_DB_PATH,
            config.POLE_TABLE,
            config.POLE_ID_COLUMN,
            catalog.record_count,
            len(catalog.get_all_ids()),
            with_coordinates,
        )

        if with_coordinates == 0:
            raise PoleCatalogError(
                f"No record in {config.POLE_TABLE!r} has usable coordinates in "
                f"{config.POLE_X_COLUMN!r}/{config.POLE_Y_COLUMN!r}. Discovered poles "
                f"cannot be placed and no work area can be built. Confirm those are "
                f"the correct coordinate columns and that they contain values."
            )

        return catalog

    def __len__(self) -> int:
        return len(self._records)

    @property
    def record_count(self) -> int:
        return len(self._records)

    @property
    def records(self) -> tuple[PoleRecord, ...]:
        """All records in their original load order."""

        return self._records

    def exact_matches(self, candidate: str) -> tuple[PoleRecord, ...]:
        normalized = normalize_pole_id(str(candidate or "").strip())
        if not normalized:
            return ()
        return self._records_by_pole_id.get(normalized, ())

    def match_exact(self, candidate: str) -> ExactMatchResult:
        candidate_raw = str(candidate or "").strip()
        candidate_normalized = normalize_pole_id(candidate_raw)
        matches = self._records_by_pole_id.get(candidate_normalized, ())

        return {
            "candidate_raw": candidate_raw,
            "candidate_normalized": candidate_normalized,
            "match_status": "reference_exact" if matches else "not_found",
            "match_count": len(matches),
            "records": [dict(record) for record in matches],
        }

    def get_all_ids(self) -> set[str]:
        return set(self._all_ids)

    def summary(self) -> PoleCatalogSummary:
        return {
            "record_count": self.record_count,
            "unique_pole_id_count": len(self._all_ids),
            "duplicate_pole_id_count": len(self._duplicate_ids),
            "duplicate_source_id_count": len(self._duplicate_source_ids),
        }


def _table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    table_row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND lower(name) = lower(?)",
        (table,),
    ).fetchone()

    if table_row is None:
        available = connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        names = ", ".join(str(row["name"]) for row in available)
        raise PoleCatalogError(
            f"Pole reference table or view {table!r} was not found. "
            f"Available: {names or 'none'}"
        )

    actual_table = str(table_row["name"])
    return [
        str(row["name"])
        for row in connection.execute(f'PRAGMA table_info("{actual_table}")').fetchall()
    ]


def _validate_configured_columns(
    connection: sqlite3.Connection, source_config: PoleSourceConfig
) -> None:
    """Fails loudly when a configured column doesn't exist.

    Without this, a wrong or blank column name produces `NULL AS x` in the
    select (or a raw SQLite error deep in the query), which surfaces much
    later as "no poles with coordinates" -- far from the actual cause.
    """

    available = _table_columns(connection, source_config.table)
    available_lower = {column.lower() for column in available}

    configured = {
        "POLE_ID_COLUMN": source_config.pole_id_column,
        "POLE_SOURCE_ID_COLUMN": source_config.source_id_column,
        "POLE_X_COLUMN": source_config.x_column,
        "POLE_Y_COLUMN": source_config.y_column,
        "POLE_STATUS_COLUMN": source_config.status_column,
        "POLE_UPDATED_COLUMN": source_config.updated_at_column,
    }

    missing = [
        f"{setting}={column!r}"
        for setting, column in configured.items()
        if column and column.lower() not in available_lower
    ]

    if missing:
        raise PoleCatalogError(
            f"Configured columns not found in {source_config.table!r}: "
            f"{', '.join(missing)}. Available columns: {', '.join(available)}"
        )

    # Coordinates aren't optional for this service's purpose: without them a
    # discovered pole can't be placed and no work area can be built. Catch a
    # blank/unset coordinate column here rather than after a full Qwen run.
    if not source_config.x_column or not source_config.y_column:
        raise PoleCatalogError(
            "POLE_X_COLUMN and POLE_Y_COLUMN must both be set -- discovered "
            "poles need coordinates to place features and build a work area. "
            f"Available columns in {source_config.table!r}: {', '.join(available)}"
        )


def _build_select_query(source_config: PoleSourceConfig) -> str:
    columns = [
        (f'"{source_config.pole_id_column}" AS "pole_id_raw"'),
        (f'"{source_config.source_id_column}" AS "source_id"'),
        _optional_column_expression(source_config.x_column, "x"),
        _optional_column_expression(source_config.y_column, "y"),
        _optional_column_expression(source_config.status_column, "status"),
        _optional_column_expression(source_config.updated_at_column, "source_updated_at"),
    ]
    return "SELECT\n    " + ",\n    ".join(columns) + f'\nFROM "{source_config.table}"'


def _optional_column_expression(column_name: str | None, alias: str) -> str:
    if not column_name:
        return f'NULL AS "{alias}"'
    return f'"{column_name}" AS "{alias}"'


def _validate_record(record: Mapping[str, object]) -> PoleRecord:
    pole_id_raw = str(record["pole_id_raw"])
    return {
        "source_id": str(record["source_id"]),
        "pole_id_raw": pole_id_raw,
        "pole_id_normalized": normalize_pole_id(pole_id_raw),
        "x": record.get("x"),
        "y": record.get("y"),
        "status": record.get("status"),
        "source_updated_at": record.get("source_updated_at"),
    }


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
