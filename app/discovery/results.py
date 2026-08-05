"""Build the final pole-discovery result: accepted pole IDs merged with
their authoritative catalog coordinates.

Coordinates always come from the pole catalog. The vision model identifies
text only -- it never supplies or estimates GIS coordinates.
"""

from __future__ import annotations

from typing import Literal, TypedDict

from .native_text import NativeTextDiscoveryResult
from .pole_catalog import PoleCatalog
from .visual_discovery import VisualDiscoveryResult

DiscoveryStatus = Literal["completed", "needs_review", "no_poles_found"]


class AcceptedPole(TypedDict):
    pole_id: str
    x: float | None
    y: float | None
    discovery_sources: list[str]
    pages: list[int]
    confidence: float


class PoleDiscoveryResult(TypedDict):
    status: DiscoveryStatus
    accepted_pole_count: int
    review_candidate_count: int
    accepted_pole_ids: list[str]
    accepted_poles: list[AcceptedPole]


def build_pole_discovery_result(
    pole_catalog: PoleCatalog,
    native_discovery: NativeTextDiscoveryResult,
    visual_discovery: VisualDiscoveryResult | None,
) -> PoleDiscoveryResult:
    native_pole_ids = set(native_discovery["exact_unique_pole_ids"])
    visual_pole_ids = set(visual_discovery["exact_unique_pole_ids"]) if visual_discovery is not None else set()
    accepted_pole_ids = sorted(native_pole_ids | visual_pole_ids)

    accepted_poles = [
        _build_accepted_pole(pole_id, pole_catalog, native_discovery, visual_discovery)
        for pole_id in accepted_pole_ids
    ]

    review_candidate_count = (
        visual_discovery["review_candidate_observation_count"] if visual_discovery is not None else 0
    )

    if review_candidate_count > 0:
        status: DiscoveryStatus = "needs_review"
    elif accepted_poles:
        status = "completed"
    else:
        status = "no_poles_found"

    return {
        "status": status,
        "accepted_pole_count": len(accepted_poles),
        "review_candidate_count": review_candidate_count,
        "accepted_pole_ids": accepted_pole_ids,
        "accepted_poles": accepted_poles,
    }


def _build_accepted_pole(
    pole_id: str,
    pole_catalog: PoleCatalog,
    native_discovery: NativeTextDiscoveryResult,
    visual_discovery: VisualDiscoveryResult | None,
) -> AcceptedPole:
    native_observations = [
        candidate
        for candidate in native_discovery["candidates"]
        if candidate["match_status"] == "reference_exact" and candidate["normalized_text"] == pole_id
    ]
    visual_observations = (
        [
            candidate
            for candidate in visual_discovery["candidates"]
            if candidate["acceptance_status"] == "accepted" and candidate["normalized_text"] == pole_id
        ]
        if visual_discovery is not None
        else []
    )

    records = pole_catalog.exact_matches(pole_id)
    x, y = _resolve_coordinates(records)

    discovery_sources: list[str] = []
    if native_observations:
        discovery_sources.append("native_text")
    if visual_observations:
        discovery_sources.append("visual")

    pages = sorted({o["page_number"] for o in (native_observations + visual_observations)})
    confidence = _resolve_confidence(native_observations, visual_observations)

    return {
        "pole_id": pole_id,
        "x": x,
        "y": y,
        "discovery_sources": discovery_sources,
        "pages": pages,
        "confidence": confidence,
    }


def _resolve_confidence(native_observations: list, visual_observations: list) -> float:
    """Best-evidence confidence across whichever pass(es) found this pole.

    Native-text matches are exact literal text extracted from the PDF, not
    a model guess, so they carry full confidence. Visual matches carry
    whatever the resolver settled on after homoglyph-correction penalties.
    When a pole was found both ways, the higher confidence wins.
    """

    scores: list[float] = []
    if native_observations:
        scores.append(1.0)
    for observation in visual_observations:
        scores.append(
            float(observation.get("resolution_confidence", observation.get("model_confidence", 0.0)))
        )

    return max(scores) if scores else 0.0


def _resolve_coordinates(records) -> tuple[float | None, float | None]:
    if not records:
        return None, None

    coordinate_pairs = {(record["x"], record["y"]) for record in records}
    if len(coordinate_pairs) > 1:
        # Multiple reference records disagree on where this pole is --
        # don't guess which one is right.
        return None, None

    return next(iter(coordinate_pairs))
