"""Resolve visually read pole labels against the authoritative catalog.

Plan sheets print a label such as ``P#O999F`` where ``P#`` means "pole
number" and ``O999F`` is the FacilityID. Scanned sheets have no text layer,
so every label arrives from the vision model and the leading letter is
routinely rendered as a lookalike digit (``O`` as ``0``, ``I`` as ``1``,
``S`` as ``5``, ``G`` as ``6``).

Catalog membership alone cannot certify a reading. FacilityID blocks are
nearly contiguous, so a single mis-read digit usually lands on a different
real pole. This module therefore does three things:

1. applies a small, auditable set of corrections;
2. scores every correction so downstream code can rank readings; and
3. reports how many other catalog IDs sit one digit away, which is the
   measure of whether the ID can be trusted without spatial proof.

Nothing here guesses beyond the catalog. ``requires_spatial_confirmation``
signals that the ID must be confirmed some other way (currently: the
evidence-review workflow) before being treated as authoritative.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from typing import Literal, TypedDict

from .pole_catalog import PoleCatalog, PoleRecord, normalize_pole_id

ResolutionStatus = Literal[
    "reference_exact",
    "reference_resolved",
    "reference_ambiguous",
    "unknown_by_design",
    "not_found",
    "ignored",
]

#: Drafter tokens that deliberately mean "pole not identified". Not
#: failures -- must not inflate the not-found rate.
UNKNOWN_LABEL_TOKENS = frozenset(
    {"UNK", "UNKN", "UNKNOWN", "TBD", "NA", "N/A", "NONE", "?", "??", "???"}
)

#: Digit -> plausible letters, ordered most to least likely, with the
#: confidence penalty charged for the substitution. Only letters that
#: actually begin a catalog ID are ever tried.
LEADING_DIGIT_HOMOGLYPHS: dict[str, tuple[tuple[str, float], ...]] = {
    "0": (("O", 0.03), ("D", 0.18), ("Q", 0.22)),
    "1": (("I", 0.08), ("L", 0.16), ("J", 0.20)),
    "2": (("Z", 0.24),),
    "4": (("A", 0.20),),
    "5": (("S", 0.10),),
    "6": (("G", 0.10), ("C", 0.22)),
    "7": (("T", 0.22),),
    "8": (("B", 0.20),),
    "9": (("G", 0.24),),
}

#: Punctuation a drafter may place between a pole marker and the ID. A
#: catalog FacilityID never contains any of these, so their presence
#: reliably separates the marker from the identifier.
PLAN_PREFIX_SEPARATORS = "-#:.,/"

#: Longest letter run treated as a pole marker, sized for "POLE".
MAXIMUM_PLAN_PREFIX_LETTERS = 4

_SEPARATED_PREFIX_PATTERN = re.compile(
    r"^[A-Z]{1,%d}[%s]+(?P<body>[A-Z0-9]+)$"
    % (MAXIMUM_PLAN_PREFIX_LETTERS, re.escape(PLAN_PREFIX_SEPARATORS))
)
_UNSPACED_PREFIX_PATTERN = re.compile(r"^(?P<marker>[A-Z]{1,2})(?P<body>\d[A-Z0-9]*)$")

#: Confidence charged for each non-homoglyph transformation.
TRANSFORMATION_PENALTIES: dict[str, float] = {
    "removed_ocr_plan_prefix": 0.02,
    "removed_pole_number_prefix": 0.02,
    "removed_generic_plan_prefix": 0.04,
    "removed_unspaced_plan_prefix": 0.06,
    "model_confirmed_explicit_pole_context": 0.05,
    "added_terminal_f": 0.15,
    "removed_terminal_f": 0.15,
}

#: A single winner must beat the runner-up by at least this much.
MINIMUM_SCORE_MARGIN = 0.05

#: Cap on how many neighbour IDs are recorded in the audit trail.
MAXIMUM_REPORTED_NEIGHBOURS = 12


class ScoredCandidate(TypedDict):
    pole_id: str
    confidence: float
    transformations: list[str]
    f_suffix_agrees: bool


class CandidateResolution(TypedDict):
    raw_text: str
    raw_normalized_text: str
    status: ResolutionStatus
    resolved_pole_id: str | None
    resolution_method: str
    transformations: list[str]
    candidate_variants: list[str]
    match_count: int
    records: list[PoleRecord]
    resolution_confidence: float
    scored_candidates: list[ScoredCandidate]
    f_suffix_agrees: bool | None
    id_neighborhood_count: int
    id_neighborhood_ids: list[str]
    requires_spatial_confirmation: bool


class _Variant(TypedDict):
    text: str
    transformations: list[str]


class CatalogCandidateResolver:
    """Resolve visual readings using catalog membership plus scoring."""

    def __init__(self, pole_catalog: PoleCatalog) -> None:
        self._pole_catalog = pole_catalog
        catalog_ids = [pole_id for pole_id in pole_catalog.get_all_ids() if pole_id]
        self._catalog_shapes = {_character_shape(pole_id) for pole_id in catalog_ids}
        self._catalog_leading_letters = frozenset(
            pole_id[0] for pole_id in catalog_ids if pole_id[0].isalpha()
        )
        self._neighborhood = _build_neighborhood_index(catalog_ids)

    def resolve(self, raw_text: str, *, explicit_pole_context: bool = False) -> CandidateResolution:
        """Resolve a model reading without fuzzy or spatial guessing."""

        raw_value = str(raw_text).strip()
        raw_normalized = normalize_pole_id(raw_value)

        if not raw_normalized:
            return _resolution(
                raw_text=raw_value,
                raw_normalized_text=raw_normalized,
                status="ignored",
                resolution_method="ignored_empty_text",
            )

        unknown_body = _unknown_label_body(raw_normalized)
        if unknown_body is not None:
            return _resolution(
                raw_text=raw_value,
                raw_normalized_text=raw_normalized,
                status="unknown_by_design",
                resolution_method="drafter_marked_pole_unknown",
                candidate_variants=[unknown_body],
            )

        direct_records = self._exact_records(raw_normalized)
        if direct_records:
            return self._finalize_single(
                raw_value=raw_value,
                raw_normalized=raw_normalized,
                status="reference_exact",
                resolution_method="catalog_exact",
                candidate=_scored(pole_id=raw_normalized, transformations=[], raw_body=raw_normalized),
                candidate_variants=[raw_normalized],
                scored_candidates=[],
                records=direct_records,
            )

        # A drafter may punctuate the ID itself, as in "O-131F". Closing the
        # gap is an exact catalog lookup, not a guess.
        joined = _remove_separators(raw_normalized)
        joined_records = self._exact_records(joined) if joined != raw_normalized else []
        if joined_records:
            return self._finalize_single(
                raw_value=raw_value,
                raw_normalized=raw_normalized,
                status="reference_resolved",
                resolution_method="catalog_exact_after_separator_removal",
                candidate=_scored(
                    pole_id=joined, transformations=["removed_identifier_separators"], raw_body=joined
                ),
                candidate_variants=[raw_normalized, joined],
                scored_candidates=[],
                records=joined_records,
            )

        prefix_result = self._remove_plan_prefix(raw_normalized)

        if prefix_result is None and explicit_pole_context and raw_normalized[:1].isdigit():
            variants = self._base_variants(
                body=raw_normalized, prefix_transformation="model_confirmed_explicit_pole_context"
            )
            return self._resolve_variants(
                raw_value=raw_value,
                raw_normalized=raw_normalized,
                body=raw_normalized,
                variants=variants,
                allow_terminal_f=False,
            )

        if prefix_result is None:
            if _character_shape(raw_normalized) not in self._catalog_shapes:
                return _resolution(
                    raw_text=raw_value,
                    raw_normalized_text=raw_normalized,
                    status="ignored",
                    resolution_method="ignored_non_pole_text",
                    candidate_variants=[raw_normalized],
                )

            return _resolution(
                raw_text=raw_value,
                raw_normalized_text=raw_normalized,
                status="not_found",
                resolution_method="catalog_exact_not_found",
                candidate_variants=[raw_normalized],
            )

        body, prefix_transformation = prefix_result

        if _unknown_label_body(body) is not None:
            return _resolution(
                raw_text=raw_value,
                raw_normalized_text=raw_normalized,
                status="unknown_by_design",
                resolution_method="drafter_marked_pole_unknown",
                candidate_variants=[body],
            )

        variants = self._base_variants(body=body, prefix_transformation=prefix_transformation)
        return self._resolve_variants(
            raw_value=raw_value,
            raw_normalized=raw_normalized,
            body=body,
            variants=variants,
            allow_terminal_f=True,
        )

    def _remove_plan_prefix(self, normalized_text: str) -> tuple[str, str] | None:
        """Separate a pole marker from the identifier it labels.

        Markers are tried from most to least certain: forms already seen in
        the field, then any short letter run joined by punctuation, then a
        marker written flush against the digits. Each step costs more
        confidence than the one before it, and the catalog still decides
        whether the surviving body is a real pole.
        """

        recognized = _remove_recognized_plan_prefix(normalized_text)
        if recognized is not None:
            return recognized

        separated = _remove_separated_plan_prefix(normalized_text)
        if separated is not None:
            return separated

        return _remove_unspaced_plan_prefix(normalized_text, self._catalog_leading_letters)

    def neighborhood(self, pole_id: str) -> list[str]:
        """Return catalog IDs one digit substitution from ``pole_id``."""

        return sorted(self._neighborhood.get(pole_id, set()))

    def _base_variants(self, body: str, prefix_transformation: str) -> list[_Variant]:
        variants: list[_Variant] = [{"text": body, "transformations": [prefix_transformation]}]
        leading = body[:1]

        for letter, penalty in LEADING_DIGIT_HOMOGLYPHS.get(leading, ()):
            if letter not in self._catalog_leading_letters:
                continue

            variants.append(
                {
                    "text": f"{letter}{body[1:]}",
                    "transformations": [
                        prefix_transformation,
                        _homoglyph_transformation(digit=leading, letter=letter, penalty=penalty),
                    ],
                }
            )

        return _deduplicate_variants(variants)

    def _resolve_variants(
        self,
        *,
        raw_value: str,
        raw_normalized: str,
        body: str,
        variants: list[_Variant],
        allow_terminal_f: bool,
    ) -> CandidateResolution:
        matches = self._matching_variants(variants)

        if not matches and allow_terminal_f:
            terminal_variants = [_toggle_terminal_f(variant) for variant in variants]
            variants = _deduplicate_variants([*variants, *terminal_variants])
            matches = self._matching_variants(variants)

        candidate_variants = [variant["text"] for variant in variants if variant["text"]]
        best_by_id: dict[str, ScoredCandidate] = {}

        for variant in matches:
            candidate = _scored(
                pole_id=variant["text"], transformations=variant["transformations"], raw_body=body
            )
            existing = best_by_id.get(candidate["pole_id"])
            if existing is None or candidate["confidence"] > existing["confidence"]:
                best_by_id[candidate["pole_id"]] = candidate

        ranked = sorted(best_by_id.values(), key=lambda item: (-item["confidence"], item["pole_id"]))

        if not ranked:
            return _resolution(
                raw_text=raw_value,
                raw_normalized_text=raw_normalized,
                status="not_found",
                resolution_method="catalog_constrained_not_found",
                candidate_variants=candidate_variants,
            )

        if len(ranked) == 1 or (ranked[0]["confidence"] - ranked[1]["confidence"] >= MINIMUM_SCORE_MARGIN):
            return self._finalize_single(
                raw_value=raw_value,
                raw_normalized=raw_normalized,
                status="reference_resolved",
                resolution_method="catalog_constrained_normalization",
                candidate=ranked[0],
                candidate_variants=candidate_variants,
                scored_candidates=ranked,
                records=self._exact_records(ranked[0]["pole_id"]),
            )

        records = _deduplicate_records(
            [record for candidate in ranked for record in self._exact_records(candidate["pole_id"])]
        )
        neighbours = sorted(
            {
                neighbour
                for candidate in ranked
                for neighbour in self._neighborhood.get(candidate["pole_id"], set())
            }
        )
        return _resolution(
            raw_text=raw_value,
            raw_normalized_text=raw_normalized,
            status="reference_ambiguous",
            resolution_method="catalog_constrained_ambiguous",
            candidate_variants=candidate_variants,
            match_count=len(ranked),
            records=records,
            resolution_confidence=ranked[0]["confidence"],
            scored_candidates=ranked,
            id_neighborhood_count=len(neighbours),
            id_neighborhood_ids=neighbours[:MAXIMUM_REPORTED_NEIGHBOURS],
            requires_spatial_confirmation=True,
        )

    def _finalize_single(
        self,
        *,
        raw_value: str,
        raw_normalized: str,
        status: ResolutionStatus,
        resolution_method: str,
        candidate: ScoredCandidate,
        candidate_variants: list[str],
        scored_candidates: list[ScoredCandidate],
        records: list[PoleRecord],
    ) -> CandidateResolution:
        pole_id = candidate["pole_id"]
        neighbours = sorted(self._neighborhood.get(pole_id, set()))
        toggled_f = any(
            name in {"added_terminal_f", "removed_terminal_f"} for name in candidate["transformations"]
        )
        return _resolution(
            raw_text=raw_value,
            raw_normalized_text=raw_normalized,
            status=status,
            resolved_pole_id=pole_id,
            resolution_method=resolution_method,
            transformations=list(candidate["transformations"]),
            candidate_variants=candidate_variants,
            match_count=len(records),
            records=records,
            resolution_confidence=candidate["confidence"],
            scored_candidates=scored_candidates or [candidate],
            f_suffix_agrees=candidate["f_suffix_agrees"],
            id_neighborhood_count=len(neighbours),
            id_neighborhood_ids=neighbours[:MAXIMUM_REPORTED_NEIGHBOURS],
            requires_spatial_confirmation=bool(neighbours or toggled_f),
        )

    def _matching_variants(self, variants: Sequence[_Variant]) -> list[_Variant]:
        return [variant for variant in variants if variant["text"] and self._exact_records(variant["text"])]

    def _exact_records(self, pole_id: str) -> list[PoleRecord]:
        return list(self._pole_catalog.match_exact(pole_id)["records"])


def _build_neighborhood_index(catalog_ids: Sequence[str]) -> dict[str, set[str]]:
    """Index catalog IDs that differ by one digit, ignoring the F flag.

    The F flag is ignored deliberately -- when the model mis-reads a digit
    the resolver's terminal-F toggle will happily add or drop the flag to
    reach a real ID, so "O994F" is genuinely reachable from a reading of
    "0995". Counting it keeps the risk measure honest.
    """

    buckets: defaultdict[str, set[str]] = defaultdict(set)
    parsed: list[tuple[str, str, str]] = []

    for pole_id in catalog_ids:
        match = re.fullmatch(r"([A-Z]+)(\d+)([A-Z]*)", pole_id)
        if match is None:
            continue

        letters, digits, suffix = match.groups()
        if suffix not in {"", "F"}:
            continue

        parsed.append((pole_id, letters, digits))
        for position in range(len(digits)):
            key = f"{letters}|{len(digits)}|{position}|{digits[:position]}|{digits[position + 1:]}"
            buckets[key].add(pole_id)

    neighborhood: dict[str, set[str]] = {}
    for pole_id, letters, digits in parsed:
        related: set[str] = set()
        for position in range(len(digits)):
            key = f"{letters}|{len(digits)}|{position}|{digits[:position]}|{digits[position + 1:]}"
            related.update(buckets[key])
        related.discard(pole_id)
        neighborhood[pole_id] = related

    return neighborhood


def _unknown_label_body(normalized_text: str) -> str | None:
    """Return the token when the drafter marked the pole unknown."""

    candidate = normalized_text
    for prefix in ("PP-", "PP#", "PP", "P-", "P#", "POLE#", "POLE", "P"):
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix) :]
            break

    candidate = candidate.lstrip(PLAN_PREFIX_SEPARATORS)
    return candidate if candidate in UNKNOWN_LABEL_TOKENS else None


def _remove_separators(normalized_text: str) -> str:
    """Return the reading with drafter punctuation closed up."""

    return normalized_text.translate({ord(character): None for character in PLAN_PREFIX_SEPARATORS})


def _remove_recognized_plan_prefix(normalized_text: str) -> tuple[str, str] | None:
    """Strip a pole marker this project has already seen in the field."""

    if normalized_text.startswith("PP-"):
        return (normalized_text[3:], "removed_ocr_plan_prefix")

    prefix_match = re.match(r"^(?:P-|P#|PP#|POLE-?NO:?|POLE#?:?)(.+)$", normalized_text)
    if prefix_match:
        return (prefix_match.group(1).lstrip(PLAN_PREFIX_SEPARATORS), "removed_pole_number_prefix")

    return None


def _remove_separated_plan_prefix(normalized_text: str) -> tuple[str, str] | None:
    """Strip any short letter marker joined to the ID by punctuation.

    Drafters invent their own pole markers, so a recognized-prefix list can
    never be complete. A catalog FacilityID is always alphanumeric, which
    makes the separator itself the reliable signal: whatever sits to its
    left is a marker, not part of the ID. The body must still contain a
    digit so road names and note text are left alone.
    """

    prefix_match = _SEPARATED_PREFIX_PATTERN.match(normalized_text)
    if prefix_match is None:
        return None

    body = prefix_match.group("body")
    if not any(character.isdigit() for character in body):
        return None

    return (body, "removed_generic_plan_prefix")


def _remove_unspaced_plan_prefix(
    normalized_text: str, catalog_leading_letters: frozenset[str]
) -> tuple[str, str] | None:
    """Strip a marker written flush against the ID, such as "P0131F".

    Without a separator the only defence against eating a real leading
    letter is the catalog itself: a marker is only removed when none of
    its letters ever begin a FacilityID.
    """

    prefix_match = _UNSPACED_PREFIX_PATTERN.match(normalized_text)
    if prefix_match is None:
        return None

    marker = prefix_match.group("marker")
    if set(marker) & catalog_leading_letters:
        return None

    return (prefix_match.group("body"), "removed_unspaced_plan_prefix")


def _homoglyph_transformation(*, digit: str, letter: str, penalty: float) -> str:
    if digit == "0" and letter == "O":
        return "leading_zero_to_letter_o"
    return f"leading_digit_{digit}_to_letter_{letter.lower()}"


def _homoglyph_penalty(transformation: str) -> float:
    if transformation == "leading_zero_to_letter_o":
        return LEADING_DIGIT_HOMOGLYPHS["0"][0][1]

    match = re.fullmatch(r"leading_digit_(\d)_to_letter_([a-z])", transformation)
    if match is None:
        return 0.0

    digit, letter = match.group(1), match.group(2).upper()
    for candidate_letter, penalty in LEADING_DIGIT_HOMOGLYPHS.get(digit, ()):
        if candidate_letter == letter:
            return penalty

    return 0.25


def _scored(*, pole_id: str, transformations: Sequence[str], raw_body: str) -> ScoredCandidate:
    penalty = 0.0
    for transformation in transformations:
        penalty += TRANSFORMATION_PENALTIES.get(transformation, 0.0)
        penalty += _homoglyph_penalty(transformation)

    return {
        "pole_id": pole_id,
        "confidence": round(max(0.0, 1.0 - penalty), 4),
        "transformations": list(transformations),
        "f_suffix_agrees": raw_body.endswith("F") == pole_id.endswith("F"),
    }


def _toggle_terminal_f(variant: _Variant) -> _Variant:
    text = variant["text"]
    if text.endswith("F"):
        toggled_text, transformation = text[:-1], "removed_terminal_f"
    else:
        toggled_text, transformation = f"{text}F", "added_terminal_f"

    return {"text": toggled_text, "transformations": [*variant["transformations"], transformation]}


def _deduplicate_variants(variants: Sequence[_Variant]) -> list[_Variant]:
    unique_variants: dict[str, _Variant] = {}
    for variant in variants:
        if variant["text"] and variant["text"] not in unique_variants:
            unique_variants[variant["text"]] = variant
    return list(unique_variants.values())


def _deduplicate_records(records: Sequence[PoleRecord]) -> list[PoleRecord]:
    unique_records: dict[tuple[object, ...], PoleRecord] = {}
    for record in records:
        key = (record.get("pole_id_normalized"), record.get("source_id"), record.get("x"), record.get("y"))
        unique_records.setdefault(key, record)
    return list(unique_records.values())


def _resolution(
    *,
    raw_text: str,
    raw_normalized_text: str,
    status: ResolutionStatus,
    resolved_pole_id: str | None = None,
    resolution_method: str,
    transformations: list[str] | None = None,
    candidate_variants: list[str] | None = None,
    match_count: int = 0,
    records: list[PoleRecord] | None = None,
    resolution_confidence: float = 0.0,
    scored_candidates: list[ScoredCandidate] | None = None,
    f_suffix_agrees: bool | None = None,
    id_neighborhood_count: int = 0,
    id_neighborhood_ids: list[str] | None = None,
    requires_spatial_confirmation: bool = False,
) -> CandidateResolution:
    return {
        "raw_text": raw_text,
        "raw_normalized_text": raw_normalized_text,
        "status": status,
        "resolved_pole_id": resolved_pole_id,
        "resolution_method": resolution_method,
        "transformations": list(transformations or []),
        "candidate_variants": list(candidate_variants or []),
        "match_count": match_count,
        "records": list(records or []),
        "resolution_confidence": resolution_confidence,
        "scored_candidates": list(scored_candidates or []),
        "f_suffix_agrees": f_suffix_agrees,
        "id_neighborhood_count": id_neighborhood_count,
        "id_neighborhood_ids": list(id_neighborhood_ids or []),
        "requires_spatial_confirmation": requires_spatial_confirmation,
    }


def _character_shape(value: str) -> str:
    return "".join("A" if c.isalpha() else "9" if c.isdigit() else c for c in value)
