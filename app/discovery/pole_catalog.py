"""Authoritative pole reference catalog.

Loads pole IDs and their X/Y coordinates from a SQLite database the city
maintains independently of this service. Column/table names are
configurable (see .env.example) since the schema isn't ours to dictate.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .. import config


@dataclass(frozen=True)
class PoleRecord:
    pole_id: str
    x: float | None
    y: float | None


class PoleCatalogError(RuntimeError):
    """Raised when the pole reference database can't be loaded."""


class PoleCatalog:
    def __init__(self, records_by_id: dict[str, PoleRecord]):
        self._records_by_id = records_by_id

    @classmethod
    def load(cls) -> "PoleCatalog":
        db_path = Path(config.POLE_DB_PATH)
        if not db_path.exists():
            raise PoleCatalogError(f"Pole reference database not found: {db_path}")

        connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row

        try:
            query = (
                f'SELECT "{config.POLE_ID_COLUMN}" AS pole_id, '
                f'"{config.POLE_X_COLUMN}" AS x, "{config.POLE_Y_COLUMN}" AS y '
                f'FROM "{config.POLE_TABLE}"'
            )
            records_by_id: dict[str, PoleRecord] = {}

            for row in connection.execute(query):
                raw_id = row["pole_id"]
                if raw_id is None:
                    continue

                normalized_id = normalize_pole_id(str(raw_id))
                if not normalized_id:
                    continue

                x = float(row["x"]) if row["x"] is not None else None
                y = float(row["y"]) if row["y"] is not None else None
                records_by_id[normalized_id] = PoleRecord(pole_id=normalized_id, x=x, y=y)

            if not records_by_id:
                raise PoleCatalogError(
                    f"Pole reference table {config.POLE_TABLE!r} returned no usable records."
                )

            return cls(records_by_id)

        except sqlite3.Error as exc:
            raise PoleCatalogError(f"Failed to read pole reference database: {exc}") from exc
        finally:
            connection.close()

    def match(self, raw_text: str) -> PoleRecord | None:
        """Exact match, with a zero/O correction fallback.

        A reading like "01135" resolves to catalog ID "O1135" only when the
        raw value starts with zero and the O-substituted variant is an
        exact catalog match -- this avoids guessing when it's ambiguous.
        """

        normalized = normalize_pole_id(raw_text)
        if not normalized:
            return None

        direct = self._records_by_id.get(normalized)
        if direct is not None:
            return direct

        if normalized.startswith("0") and len(normalized) > 1:
            corrected = "O" + normalized[1:]
            return self._records_by_id.get(corrected)

        return None

    def __len__(self) -> int:
        return len(self._records_by_id)


def normalize_pole_id(value: str) -> str:
    return "".join(value.upper().split())
