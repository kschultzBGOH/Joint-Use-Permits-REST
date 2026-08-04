"""Discover electric-pole IDs from visually rendered PDF pages.

Split into two passes:

1. Qwen3-VL assesses inexpensive whole-page previews (routing).
2. Only relevant pages are rendered as overlapping high-resolution tiles
   and read for candidate IDs.

The model reads candidate text; Python remains responsible for
normalization, authoritative catalog validation, evidence review, and
overlap deduplication.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypedDict

from PIL import Image

from .candidate_resolver import CatalogCandidateResolver
from .pole_catalog import PoleCatalog
from .renderer import RenderedImage, VisualPageSelection, VisualRenderResult, render_page_tiles
from .vision_model import VisionModelBundle

logger = logging.getLogger(__name__)

CandidateAcceptanceStatus = Literal["pending", "accepted", "review"]


class PreviewAssessment(TypedDict):
    page_number: int
    page_index: int
    preview_path: str
    contains_pole_drawing: bool
    contains_facility_ids: bool
    contains_utility_plan_sheet: bool
    contains_highlighted_pole_labels: bool
    confidence: float
    relevant_for_tiles: bool
    reason: str
    raw_response: str
    error: str | None


class VisualPoleCandidate(TypedDict):
    page_number: int
    page_index: int
    tile_index: int
    tile_path: str
    tile_pdf_bbox: list[float]
    raw_text: str
    raw_normalized_text: str
    normalized_text: str
    candidate_variants: list[str]
    resolution_method: str
    resolution_transformations: list[str]
    label_text: str
    context_type: str
    explicit_pole_context: bool
    model_confidence: float
    model_reports_complete: bool
    model_reports_edge_clipped: bool
    catalog_match_status: str
    catalog_match_count: int
    matched_source_ids: list[str]
    resolution_confidence: float
    f_suffix_agrees: bool | None
    id_neighborhood_count: int
    id_neighborhood_ids: list[str]
    requires_spatial_confirmation: bool
    acceptance_status: CandidateAcceptanceStatus
    review_reasons: list[str]


class TileAnalysis(TypedDict):
    page_number: int
    page_index: int
    tile_index: int
    tile_path: str
    candidate_count: int
    ignored_candidate_count: int
    ignored_candidates: list[dict[str, object]]
    raw_response: str
    error: str | None


class VisualPoleMatchSummary(TypedDict):
    pole_id: str
    pages: list[int]
    tile_observation_count: int
    maximum_model_confidence: float
    catalog_match_count: int
    matched_source_ids: list[str]


class VisualDiscoveryResult(TypedDict):
    pdf_path: str
    model_dir: str
    analyzed_at: str
    evidence_file_path: str
    minimum_candidate_confidence: float
    preview_assessment_count: int
    preview_error_count: int
    relevant_pages: list[int]
    skipped_pages: list[int]
    tile_count: int
    tile_error_count: int
    candidate_observation_count: int
    ignored_candidate_observation_count: int
    exact_candidate_observation_count: int
    accepted_candidate_observation_count: int
    review_candidate_observation_count: int
    exact_unique_pole_ids: list[str]
    unmatched_unique_candidate_ids: list[str]
    review_unique_candidate_ids: list[str]
    page_assessments: list[PreviewAssessment]
    tile_analyses: list[TileAnalysis]
    candidates: list[VisualPoleCandidate]
    exact_matches: list[VisualPoleMatchSummary]


class VisualDiscoveryError(RuntimeError):
    """Raised when visual model analysis cannot continue safely."""


_PAGE_ROUTING_PROMPT = """
You are routing a page from an electric utility construction plan set.

Inspect the complete page image conservatively. Determine whether the page
contains either:
1. an electric-pole drawing, pole layout, pole schedule, or pole callout; or
2. visible alphanumeric Facility IDs or pole IDs, including an ID table;
3. a utility construction-plan drawing sheet where small pole labels may be
   difficult to read at preview resolution; or
4. highlighted pole symbols or highlighted labels.

Do not transcribe the IDs during this routing step.
Return only this JSON object:
{
  "contains_pole_drawing": true,
  "contains_facility_ids": true,
  "contains_utility_plan_sheet": true,
  "contains_highlighted_pole_labels": true,
  "confidence": 0.95,
  "reason": "short explanation"
}

Use false only when the content is clearly absent. Confidence must be from
0 through 1. Do not use Markdown or code fences.
""".strip()


def discover_visual_poles(
    pdf_path: str | Path,
    visual_renders: VisualRenderResult,
    pole_catalog: PoleCatalog,
    vision_model: VisionModelBundle,
    output_directory: str | Path,
    *,
    page_confidence_threshold: float = 0.75,
    preview_max_edge_pixels: int = 1400,
    tile_dpi: int = 225,
    tile_pixels: int = 1800,
    tile_overlap_ratio: float = 0.15,
    tile_model_max_edge_pixels: int = 1800,
    minimum_candidate_confidence: float = 0.80,
    strict: bool = False,
) -> VisualDiscoveryResult:
    """Run page routing, targeted tile rendering, and pole-ID extraction."""

    resolved_pdf = Path(pdf_path).expanduser().resolve()
    output_root = Path(output_directory).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    threshold = _validate_confidence_threshold(page_confidence_threshold)
    candidate_confidence_threshold = _validate_confidence_threshold(minimum_candidate_confidence)
    previews = list(visual_renders["previews"])
    selections = list(visual_renders["selections"])

    page_assessments = assess_visual_previews(
        previews=previews,
        vision_model=vision_model,
        confidence_threshold=threshold,
        maximum_image_edge=preview_max_edge_pixels,
        strict=strict,
    )

    relevant_pages = sorted(
        {assessment["page_number"] for assessment in page_assessments if assessment["relevant_for_tiles"]}
    )
    assessed_pages = {assessment["page_number"] for assessment in page_assessments}
    skipped_pages = sorted(assessed_pages - set(relevant_pages))

    selections_by_page = {selection["page_number"]: selection for selection in selections}
    relevant_selections: list[VisualPageSelection] = []
    for page_number in relevant_pages:
        selection = selections_by_page.get(page_number)
        if selection is None:
            raise VisualDiscoveryError(
                f"Visual page {page_number} has a preview but no corresponding page selection."
            )
        relevant_selections.append(selection)

    rendered_tiles: list[RenderedImage] = []
    if relevant_selections:
        rendered_tiles = render_page_tiles(
            pdf_path=resolved_pdf,
            selections=relevant_selections,
            output_directory=output_root / "tiles",
            dpi=tile_dpi,
            tile_pixels=tile_pixels,
            overlap_ratio=tile_overlap_ratio,
        )

    tile_prompt = _tile_extraction_prompt(catalog_shapes=_catalog_character_shapes(pole_catalog))
    tile_analyses, candidates = analyze_visual_tiles(
        tiles=rendered_tiles,
        pole_catalog=pole_catalog,
        vision_model=vision_model,
        prompt=tile_prompt,
        maximum_image_edge=tile_model_max_edge_pixels,
        strict=strict,
    )

    apply_candidate_evidence_rules(candidates=candidates, minimum_confidence=candidate_confidence_threshold)

    exact_candidates = [c for c in candidates if c["catalog_match_status"] == "reference_exact"]
    accepted_candidates = [c for c in exact_candidates if c["acceptance_status"] == "accepted"]
    review_candidates = [c for c in candidates if c["acceptance_status"] == "review"]
    exact_matches = deduplicate_exact_candidates(accepted_candidates)
    exact_unique_pole_ids = [match["pole_id"] for match in exact_matches]
    unmatched_unique_candidate_ids = sorted(
        {c["normalized_text"] for c in candidates if c["catalog_match_status"] != "reference_exact" and c["normalized_text"]}
    )
    review_unique_candidate_ids = sorted(
        {c["normalized_text"] for c in review_candidates if c["normalized_text"]}
    )

    evidence_file_path = output_root / "visual_discovery.json"
    result: VisualDiscoveryResult = {
        "pdf_path": str(resolved_pdf),
        "model_dir": str(vision_model.model_dir),
        "analyzed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "evidence_file_path": str(evidence_file_path),
        "minimum_candidate_confidence": candidate_confidence_threshold,
        "preview_assessment_count": len(page_assessments),
        "preview_error_count": sum(a["error"] is not None for a in page_assessments),
        "relevant_pages": relevant_pages,
        "skipped_pages": skipped_pages,
        "tile_count": len(rendered_tiles),
        "tile_error_count": sum(a["error"] is not None for a in tile_analyses),
        "candidate_observation_count": len(candidates),
        "ignored_candidate_observation_count": sum(a["ignored_candidate_count"] for a in tile_analyses),
        "exact_candidate_observation_count": len(exact_candidates),
        "accepted_candidate_observation_count": len(accepted_candidates),
        "review_candidate_observation_count": len(review_candidates),
        "exact_unique_pole_ids": exact_unique_pole_ids,
        "unmatched_unique_candidate_ids": unmatched_unique_candidate_ids,
        "review_unique_candidate_ids": review_unique_candidate_ids,
        "page_assessments": page_assessments,
        "tile_analyses": tile_analyses,
        "candidates": candidates,
        "exact_matches": exact_matches,
    }
    _write_visual_evidence(result=result, output_path=evidence_file_path)
    return result


def assess_visual_previews(
    previews: Sequence[RenderedImage],
    vision_model: VisionModelBundle,
    *,
    confidence_threshold: float = 0.75,
    maximum_image_edge: int = 1400,
    strict: bool = False,
) -> list[PreviewAssessment]:
    """Ask Qwen which preview pages require high-resolution analysis."""

    threshold = _validate_confidence_threshold(confidence_threshold)
    assessments: list[PreviewAssessment] = []

    for preview in sorted(previews, key=lambda item: item["page_number"]):
        page_number = preview["page_number"]
        image_path = Path(preview["image_path"])
        raw_response = ""

        logger.info("Assessing visual preview for page %s.", page_number)

        try:
            raw_response = _generate_image_response(
                vision_model=vision_model,
                image_path=image_path,
                prompt=_PAGE_ROUTING_PROMPT,
                max_new_tokens=180,
                maximum_image_edge=maximum_image_edge,
            )
            parsed = _parse_page_assessment(raw_response)
            relevant = (
                parsed["contains_pole_drawing"]
                or parsed["contains_facility_ids"]
                or parsed["contains_utility_plan_sheet"]
                or parsed["contains_highlighted_pole_labels"]
                or parsed["confidence"] < threshold
            )

            assessment: PreviewAssessment = {
                "page_number": page_number,
                "page_index": preview["page_index"],
                "preview_path": str(image_path),
                "contains_pole_drawing": parsed["contains_pole_drawing"],
                "contains_facility_ids": parsed["contains_facility_ids"],
                "contains_utility_plan_sheet": parsed["contains_utility_plan_sheet"],
                "contains_highlighted_pole_labels": parsed["contains_highlighted_pole_labels"],
                "confidence": parsed["confidence"],
                "relevant_for_tiles": relevant,
                "reason": parsed["reason"],
                "raw_response": raw_response,
                "error": None,
            }

        except Exception as exc:
            if strict:
                raise VisualDiscoveryError(f"Preview analysis failed for page {page_number}: {exc}") from exc

            logger.warning(
                "Preview analysis failed for page %s; including the page in tile analysis: %s",
                page_number,
                exc,
            )
            assessment = {
                "page_number": page_number,
                "page_index": preview["page_index"],
                "preview_path": str(image_path),
                "contains_pole_drawing": False,
                "contains_facility_ids": False,
                "contains_utility_plan_sheet": False,
                "contains_highlighted_pole_labels": False,
                "confidence": 0.0,
                "relevant_for_tiles": True,
                "reason": "Preview response could not be parsed; page retained to protect recall.",
                "raw_response": raw_response,
                "error": str(exc),
            }

        assessments.append(assessment)

    return assessments


def analyze_visual_tiles(
    tiles: Sequence[RenderedImage],
    pole_catalog: PoleCatalog,
    vision_model: VisionModelBundle,
    prompt: str,
    *,
    maximum_image_edge: int = 1800,
    strict: bool = False,
) -> tuple[list[TileAnalysis], list[VisualPoleCandidate]]:
    """Read candidate IDs from tiles and validate each against the catalog."""

    analyses: list[TileAnalysis] = []
    candidates: list[VisualPoleCandidate] = []
    candidate_resolver = CatalogCandidateResolver(pole_catalog)

    for tile in sorted(tiles, key=lambda item: (item["page_number"], item["tile_index"] or 0)):
        tile_index = tile["tile_index"]
        if tile_index is None:
            raise VisualDiscoveryError("A tile render is missing its tile index.")

        image_path = Path(tile["image_path"])
        raw_response = ""

        logger.info("Analyzing page %s visual tile %s.", tile["page_number"], tile_index)

        try:
            raw_response = _generate_image_response(
                vision_model=vision_model,
                image_path=image_path,
                prompt=prompt,
                max_new_tokens=600,
                maximum_image_edge=maximum_image_edge,
            )
            parsed_candidates = _parse_tile_candidates(raw_response)
            tile_candidates: list[VisualPoleCandidate] = []
            seen_normalized_ids: set[str] = set()
            ignored_candidates: list[dict[str, object]] = []

            for (
                raw_text,
                confidence,
                reports_complete,
                reports_edge_clipped,
                context_type,
                label_text,
            ) in parsed_candidates:
                explicit_pole_context = _has_explicit_pole_label(label_text) or _has_explicit_pole_label(
                    raw_text
                )
                resolution = candidate_resolver.resolve(
                    raw_text, explicit_pole_context=explicit_pole_context
                )
                raw_normalized_text = resolution["raw_normalized_text"]

                if not _valid_candidate_text(raw_normalized_text):
                    continue

                if resolution["status"] == "ignored":
                    ignored_candidates.append(
                        {
                            "raw_text": raw_text,
                            "label_text": label_text,
                            "context_type": context_type,
                            "explicit_pole_context": explicit_pole_context,
                            "raw_normalized_text": raw_normalized_text,
                            "resolution_method": resolution["resolution_method"],
                            "candidate_variants": list(resolution["candidate_variants"]),
                        }
                    )
                    continue

                normalized_text = resolution["resolved_pole_id"] or raw_normalized_text
                if normalized_text in seen_normalized_ids:
                    continue

                seen_normalized_ids.add(normalized_text)
                matching_records = resolution["records"]

                catalog_match_status = (
                    "reference_exact"
                    if resolution["status"] in {"reference_exact", "reference_resolved"}
                    else resolution["status"]
                )

                candidate: VisualPoleCandidate = {
                    "page_number": tile["page_number"],
                    "page_index": tile["page_index"],
                    "tile_index": tile_index,
                    "tile_path": str(image_path),
                    "tile_pdf_bbox": list(tile["pdf_bbox"]),
                    "raw_text": raw_text,
                    "raw_normalized_text": raw_normalized_text,
                    "normalized_text": normalized_text,
                    "candidate_variants": list(resolution["candidate_variants"]),
                    "resolution_method": resolution["resolution_method"],
                    "resolution_transformations": list(resolution["transformations"]),
                    "label_text": label_text,
                    "context_type": context_type,
                    "explicit_pole_context": explicit_pole_context,
                    "model_confidence": confidence,
                    "model_reports_complete": reports_complete,
                    "model_reports_edge_clipped": reports_edge_clipped,
                    "catalog_match_status": catalog_match_status,
                    "catalog_match_count": resolution["match_count"],
                    "matched_source_ids": sorted({record["source_id"] for record in matching_records}),
                    "resolution_confidence": resolution["resolution_confidence"],
                    "f_suffix_agrees": resolution["f_suffix_agrees"],
                    "id_neighborhood_count": resolution["id_neighborhood_count"],
                    "id_neighborhood_ids": list(resolution["id_neighborhood_ids"]),
                    "requires_spatial_confirmation": resolution["requires_spatial_confirmation"],
                    "acceptance_status": "pending",
                    "review_reasons": [],
                }
                tile_candidates.append(candidate)

            candidates.extend(tile_candidates)
            analysis: TileAnalysis = {
                "page_number": tile["page_number"],
                "page_index": tile["page_index"],
                "tile_index": tile_index,
                "tile_path": str(image_path),
                "candidate_count": len(tile_candidates),
                "ignored_candidate_count": len(ignored_candidates),
                "ignored_candidates": ignored_candidates,
                "raw_response": raw_response,
                "error": None,
            }

        except Exception as exc:
            if strict:
                raise VisualDiscoveryError(
                    f"Tile analysis failed for page {tile['page_number']}, tile {tile_index}: {exc}"
                ) from exc

            logger.warning("Tile analysis failed for page %s, tile %s: %s", tile["page_number"], tile_index, exc)
            analysis = {
                "page_number": tile["page_number"],
                "page_index": tile["page_index"],
                "tile_index": tile_index,
                "tile_path": str(image_path),
                "candidate_count": 0,
                "ignored_candidate_count": 0,
                "ignored_candidates": [],
                "raw_response": raw_response,
                "error": str(exc),
            }

        analyses.append(analysis)

    return analyses, candidates


def apply_candidate_evidence_rules(
    candidates: Sequence[VisualPoleCandidate], *, minimum_confidence: float = 0.80
) -> None:
    """Classify visual observations as accepted or requiring review.

    Exact catalog membership is necessary but not sufficient. Accepted
    observations must also be complete, clear of the tile edge, and meet
    the confidence threshold. Prefix ambiguity is handled across the whole
    page so overlapping tiles can't promote a truncated ID merely by
    repeating it.
    """

    threshold = _validate_confidence_threshold(minimum_confidence)
    exact_ids_by_page: defaultdict[int, set[str]] = defaultdict(set)

    for candidate in candidates:
        if candidate["catalog_match_status"] != "reference_exact":
            continue
        exact_ids_by_page[candidate["page_number"]].add(candidate["normalized_text"])

    for candidate in candidates:
        review_reasons: list[str] = []

        if candidate["catalog_match_status"] == "reference_ambiguous":
            review_reasons.append("catalog_ambiguous")
        elif candidate["catalog_match_status"] == "unknown_by_design":
            review_reasons.append("drafter_marked_pole_unknown")
        elif candidate["catalog_match_status"] != "reference_exact":
            review_reasons.append("catalog_not_found")

        if not candidate["model_reports_complete"]:
            review_reasons.append("model_did_not_confirm_complete_id")

        if candidate["model_reports_edge_clipped"]:
            review_reasons.append("model_reported_tile_edge_clipping")

        if candidate["model_confidence"] < threshold:
            review_reasons.append("below_minimum_confidence")

        if candidate["catalog_match_status"] == "reference_exact":
            page_number = candidate["page_number"]
            pole_id = candidate["normalized_text"]
            longer_prefix_ids = {
                other_id
                for other_id in exact_ids_by_page[page_number]
                if other_id != pole_id and len(other_id) > len(pole_id) and other_id.startswith(pole_id)
            }
            if len(longer_prefix_ids) >= 2:
                review_reasons.append("prefix_of_multiple_longer_ids_on_page")
            elif longer_prefix_ids:
                review_reasons.append("prefix_of_longer_id_on_page")

        candidate["review_reasons"] = review_reasons
        candidate["acceptance_status"] = "review" if review_reasons else "accepted"


def deduplicate_exact_candidates(
    candidates: Sequence[VisualPoleCandidate],
) -> list[VisualPoleMatchSummary]:
    """Merge repeated exact IDs from overlapping tiles and multiple pages."""

    candidates_by_id: defaultdict[str, list[VisualPoleCandidate]] = defaultdict(list)
    for candidate in candidates:
        if candidate["catalog_match_status"] == "reference_exact" and candidate["acceptance_status"] == "accepted":
            candidates_by_id[candidate["normalized_text"]].append(candidate)

    summaries: list[VisualPoleMatchSummary] = []
    for pole_id in sorted(candidates_by_id):
        observations = candidates_by_id[pole_id]
        summaries.append(
            {
                "pole_id": pole_id,
                "pages": sorted({observation["page_number"] for observation in observations}),
                "tile_observation_count": len(observations),
                "maximum_model_confidence": round(
                    max(observation["model_confidence"] for observation in observations), 4
                ),
                "catalog_match_count": max(observation["catalog_match_count"] for observation in observations),
                "matched_source_ids": sorted(
                    {sid for observation in observations for sid in observation["matched_source_ids"]}
                ),
            }
        )

    return summaries


def _generate_image_response(
    vision_model: VisionModelBundle,
    image_path: Path,
    prompt: str,
    max_new_tokens: int,
    maximum_image_edge: int,
) -> str:
    if vision_model.model is None or vision_model.processor is None:
        raise VisualDiscoveryError("The vision model has been unloaded.")

    if not image_path.is_file():
        raise FileNotFoundError(f"Rendered image not found: {image_path}")

    import torch

    with Image.open(image_path) as source_image:
        image = source_image.convert("RGB")
        try:
            image.thumbnail((maximum_image_edge, maximum_image_edge), Image.Resampling.LANCZOS)

            messages = [{"role": "user", "content": [{"type": "image", "image": image}, {"type": "text", "text": prompt}]}]
            inputs = vision_model.processor.apply_chat_template(
                messages, tokenize=True, add_generation_prompt=True, return_dict=True, return_tensors="pt"
            )
        finally:
            image.close()

    inputs.pop("token_type_ids", None)
    inputs = inputs.to(vision_model.device)
    input_ids = inputs["input_ids"]

    with torch.inference_mode():
        generated_ids = vision_model.model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False, use_cache=True)

    generated_ids_trimmed = [
        output_ids[len(source_ids) :] for source_ids, output_ids in zip(input_ids, generated_ids)
    ]
    decoded = vision_model.processor.batch_decode(
        generated_ids_trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False
    )

    if not decoded:
        raise VisualDiscoveryError("The vision model returned no decoded response.")

    return str(decoded[0]).strip()


def _parse_page_assessment(response_text: str) -> dict[str, object]:
    payload = _extract_json_payload(response_text)
    if not isinstance(payload, Mapping):
        raise VisualDiscoveryError("Page assessment was not a JSON object.")

    return {
        "contains_pole_drawing": _as_bool(payload.get("contains_pole_drawing")),
        "contains_facility_ids": _as_bool(payload.get("contains_facility_ids")),
        "contains_utility_plan_sheet": _as_bool(payload.get("contains_utility_plan_sheet")),
        "contains_highlighted_pole_labels": _as_bool(payload.get("contains_highlighted_pole_labels")),
        "confidence": _as_confidence(payload.get("confidence")),
        "reason": str(payload.get("reason") or "").strip()[:500],
    }


def _parse_tile_candidates(response_text: str) -> list[tuple[str, float, bool, bool, str, str]]:
    payload = _extract_json_payload(response_text)

    if isinstance(payload, Mapping):
        raw_candidates = payload.get("candidates") or payload.get("facility_ids") or payload.get("pole_ids") or []
    elif isinstance(payload, list):
        raw_candidates = payload
    else:
        raise VisualDiscoveryError("Tile response did not contain a candidate list.")

    if not isinstance(raw_candidates, list):
        raise VisualDiscoveryError("Tile candidates were not returned as a JSON list.")

    parsed_candidates: list[tuple[str, float, bool, bool, str, str]] = []
    for item in raw_candidates:
        if isinstance(item, str):
            raw_text, confidence, reports_complete, reports_edge_clipped = item, 0.0, False, False
            context_type, label_text = "unknown", item
        elif isinstance(item, Mapping):
            raw_text = str(item.get("text") or item.get("id") or item.get("facility_id") or item.get("pole_id") or "")
            confidence = _as_confidence(item.get("confidence"))
            reports_complete = _as_bool(item.get("complete", item.get("is_complete")))
            reports_edge_clipped = _as_bool(item.get("touches_tile_edge", item.get("edge_clipped")))
            context_type = str(item.get("context_type") or "unknown").strip().lower()
            label_text = str(item.get("label_text") or raw_text).strip()
        else:
            continue

        raw_text = raw_text.strip()
        if raw_text:
            parsed_candidates.append(
                (raw_text, confidence, reports_complete, reports_edge_clipped, context_type, label_text)
            )

    return parsed_candidates


def _extract_json_payload(response_text: str) -> object:
    cleaned = response_text.strip()
    if not cleaned:
        raise VisualDiscoveryError("The vision model returned an empty response.")

    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    decoder = json.JSONDecoder()

    try:
        return decoder.decode(cleaned)
    except json.JSONDecodeError:
        pass

    for opening_match in re.finditer(r"[\{\[]", cleaned):
        try:
            payload, _ = decoder.raw_decode(cleaned[opening_match.start() :])
            return payload
        except json.JSONDecodeError:
            continue

    raise VisualDiscoveryError("The vision model response did not contain valid JSON.")


def _tile_extraction_prompt(catalog_shapes: Sequence[str]) -> str:
    shape_text = ", ".join(catalog_shapes) if catalog_shapes else "alphanumeric IDs"

    return f"""
You are reading one high-resolution tile from an electric utility plan.

Extract only visible electric-pole Facility IDs or pole-number labels.
Put only the candidate ID portion in "text", and preserve the complete visible
label in "label_text". For example, for "POLE#: 01135", return
"text": "01135" and "label_text": "POLE#: 01135". For "P#O999F", return
"text": "O999F" and "label_text": "P#O999F". Do not silently change O to 0,
change 0 to O, or invent an ID from an allowed pattern.

Set "context_type" to exactly one of:
- "pole_number_label" when the text is explicitly labeled POLE, POLE#, POLE
  NO., or P#;
- "facility_id_label" when it is explicitly labeled FacilityID;
- "pole_callout" when it is visibly attached to a pole symbol or leader;
- "unknown" otherwise.

The complete "label_text" is important. Python will only consider a leading
zero-to-letter-O catalog correction when the returned label itself visibly
contains an explicit POLE/P# prefix. Do not classify stationing, dimensions,
addresses, sheet numbers, or equipment quantities as a pole label.

Only treat an ID as complete when its first and last characters are fully
visible. If a label is cut off by any image edge, or another character may
exist outside the tile, do not return it as a complete candidate. Never turn
a visible prefix such as "N143" into an ID when the remaining characters may
be outside the tile.

If an edge-clipped or otherwise incomplete reading is useful as evidence, it
may be returned only with "complete": false and "touches_tile_edge": true.
These observations will be held for review and will not become accepted IDs.

The authoritative catalog contains character-shape patterns such as:
{shape_text}

In these patterns, A means any letter and 9 means any digit. Ignore sheet
numbers, dimensions, street names, station numbers, dates, quantities, and
equipment labels that are not pole identifiers. In particular, do not return
standalone street addresses or building numbers such as "304" or "318".

Return only this JSON object:
{{
  "candidates": [
    {{
      "text": "visible ID",
      "label_text": "complete visible label",
      "context_type": "pole_number_label",
      "confidence": 0.95,
      "complete": true,
      "touches_tile_edge": false
    }}
  ]
}}

Return an empty candidates list when no pole ID is visible. Confidence must be
from 0 through 1. Every candidate must include all six fields. Do not use
Markdown or code fences.
""".strip()


def _has_explicit_pole_label(value: str) -> bool:
    """Return true only when the visible text has a pole-label prefix."""

    compact = re.sub(r"\s+", "", str(value).strip().upper())
    return bool(re.match(r"^(?:P#|PP-|POLE(?:#|:|NO\.?|NUMBER))", compact))


def _catalog_character_shapes(pole_catalog: PoleCatalog, maximum_shapes: int = 20) -> list[str]:
    shape_counts = Counter(_character_shape(pole_id) for pole_id in pole_catalog.get_all_ids() if pole_id)
    ordered_shapes = sorted(shape_counts, key=lambda shape: (-shape_counts[shape], shape))
    return ordered_shapes[:maximum_shapes]


def _character_shape(value: str) -> str:
    return "".join("A" if c.isalpha() else "9" if c.isdigit() else c for c in value)


def _valid_candidate_text(normalized_text: str) -> bool:
    if not normalized_text or len(normalized_text) > 32:
        return False
    return any(character.isalnum() for character in normalized_text)


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized_value = value.strip().lower()
        if normalized_value in {"true", "yes", "1"}:
            return True
        if normalized_value in {"false", "no", "0"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def _as_confidence(value: object) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(min(max(confidence, 0.0), 1.0), 4)


def _validate_confidence_threshold(threshold: float) -> float:
    try:
        numeric_threshold = float(threshold)
    except (TypeError, ValueError) as exc:
        raise VisualDiscoveryError("The confidence threshold must be numeric.") from exc

    if numeric_threshold < 0.0 or numeric_threshold > 1.0:
        raise VisualDiscoveryError("The confidence threshold must be from 0 through 1.")

    return numeric_threshold


def _write_visual_evidence(result: VisualDiscoveryResult, output_path: Path) -> None:
    """Write the complete visual result using an atomic file replacement."""

    temporary_path = output_path.with_suffix(f"{output_path.suffix}.tmp")

    with temporary_path.open("w", encoding="utf-8") as evidence_file:
        json.dump(result, evidence_file, indent=2, ensure_ascii=True)
        evidence_file.write("\n")

    temporary_path.replace(output_path)
