"""Fast first pass: scans a PDF's embedded text for exact pole ID matches.

Cheap and free of GPU/model cost -- pages where this finds nothing fall
through to the Qwen visual pass (see visual_discovery.py).
"""

from __future__ import annotations

import re
from typing import Any

from .pdf_pages import PageText
from .pole_catalog import PoleCatalog

# Broad on purpose: catches any alphanumeric token that might be a pole ID.
# False positives are unlikely since a match also requires the token to
# exist in the pole catalog, but a common word that happens to collide with
# a real pole ID would still be accepted -- see README's Open Items.
_WORD_PATTERN = re.compile(r"[A-Za-z0-9]{2,12}")


def run_native_text_discovery(
    pages: list[PageText], pole_catalog: PoleCatalog
) -> list[dict[str, Any]]:
    accepted: list[dict[str, Any]] = []

    for page in pages:
        for word in _WORD_PATTERN.findall(page.text):
            record = pole_catalog.match(word)
            if record is None or record.x is None or record.y is None:
                continue

            accepted.append(
                {
                    "pole_id": record.pole_id,
                    "x": record.x,
                    "y": record.y,
                    "source": "native_text",
                    "page_number": page.page_number,
                    "raw_text": word,
                    "confidence": 1.0,
                }
            )

    return accepted
