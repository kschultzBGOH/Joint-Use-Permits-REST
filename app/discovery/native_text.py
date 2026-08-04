"""Discover pole-ID candidates in a PDF's embedded native text.

Does not run OCR. Extracts PyMuPDF words with page coordinates, checks
exact matches against the pole catalog, and retains unmatched tokens that
share a character-class pattern (e.g. "A999A") with real catalog IDs, so
those pages get flagged for the visual pass even when nothing matched
exactly.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypedDict

import pymupdf

from .pole_catalog import PoleCatalog, normalize_pole_id

CandidateStatus = Literal["reference_exact", "reference_pattern"]

_WRAPPER_CHARACTERS = " \t\r\n" ".,;:!?" "()[]{}<>" "\"'`"


class NativeWord(TypedDict):
    page_number: int
    page_index: int
    text: str
    normalized_text: str
    bbox: list[float]
    block_number: int
    line_number: int
    word_number: int


class NativeCandidate(TypedDict):
    page_number: int
    page_index: int
    raw_text: str
    normalized_text: str
    bbox: list[float]
    match_status: CandidateStatus
    match_count: int
    matched_source_ids: list[str]


class PageNativeTextSummary(TypedDict):
    page_number: int
    page_index: int
    native_word_count: int
    candidate_count: int
    exact_match_count: int
    pattern_match_count: int


class NativeTextDiscoveryResult(TypedDict):
    pdf_path: str
    analyzed_at: str
    page_count: int
    native_word_count: int
    pages_without_native_text: list[int]
    exact_match_occurrence_count: int
    exact_unique_pole_ids: list[str]
    pattern_candidate_count: int
    page_summaries: list[PageNativeTextSummary]
    candidates: list[NativeCandidate]


class NativeTextDiscoveryError(RuntimeError):
    """Raised when a PDF cannot be analyzed for native text."""


def extract_native_words(pdf_path: str | Path) -> list[NativeWord]:
    resolved_path = Path(pdf_path).expanduser().resolve()
    extracted_words: list[NativeWord] = []

    with pymupdf.open(resolved_path) as document:
        if document.needs_pass:
            raise NativeTextDiscoveryError(f"PDF requires a password: {resolved_path}")

        for page_index, page in enumerate(document):
            page_number = page_index + 1
            for word in page.get_text("words", sort=False):
                if len(word) < 8:
                    continue

                raw_text = str(word[4]).strip()
                if not raw_text:
                    continue

                extracted_words.append(
                    {
                        "page_number": page_number,
                        "page_index": page_index,
                        "text": raw_text,
                        "normalized_text": normalize_pole_id(raw_text),
                        "bbox": [round(float(word[i]), 3) for i in range(4)],
                        "block_number": int(word[5]),
                        "line_number": int(word[6]),
                        "word_number": int(word[7]),
                    }
                )

    return extracted_words


def identify_candidate_tokens(
    words: Iterable[NativeWord], pole_catalog: PoleCatalog
) -> list[NativeCandidate]:
    """Identify exact reference IDs and reference-shaped unknown IDs.

    Checks single words, then adjacent word pairs on the same line (a
    drafter's ID is sometimes split by a space, e.g. "P# O131F").
    """

    word_list = list(words)
    reference_patterns = {_character_pattern(pole_id) for pole_id in pole_catalog.get_all_ids() if pole_id}

    candidates: list[NativeCandidate] = []
    candidate_keys: set[tuple[int, tuple[float, ...], str, str]] = set()

    for word in word_list:
        candidate = _evaluate_candidate(
            raw_text=word["text"],
            page_number=word["page_number"],
            page_index=word["page_index"],
            bbox=word["bbox"],
            reference_patterns=reference_patterns,
            pole_catalog=pole_catalog,
        )
        _append_unique_candidate(candidate, candidates, candidate_keys)

    words_by_line: defaultdict[tuple[int, int, int], list[NativeWord]] = defaultdict(list)
    for word in word_list:
        line_key = (word["page_index"], word["block_number"], word["line_number"])
        words_by_line[line_key].append(word)

    for line_words in words_by_line.values():
        ordered_words = sorted(line_words, key=lambda word: word["word_number"])
        for first_word, second_word in zip(ordered_words, ordered_words[1:]):
            if second_word["word_number"] != first_word["word_number"] + 1:
                continue

            joined_text = f"{first_word['text']} {second_word['text']}"
            joined_bbox = _combine_bboxes(first_word["bbox"], second_word["bbox"])

            candidate = _evaluate_candidate(
                raw_text=joined_text,
                page_number=first_word["page_number"],
                page_index=first_word["page_index"],
                bbox=joined_bbox,
                reference_patterns=reference_patterns,
                pole_catalog=pole_catalog,
            )
            _append_unique_candidate(candidate, candidates, candidate_keys)

    candidates.sort(
        key=lambda candidate: (
            candidate["page_index"],
            candidate["bbox"][1],
            candidate["bbox"][0],
            candidate["normalized_text"],
        )
    )
    return candidates


def discover_native_text(pdf_path: str | Path, pole_catalog: PoleCatalog) -> NativeTextDiscoveryResult:
    resolved_path = Path(pdf_path).expanduser().resolve()
    words = extract_native_words(resolved_path)
    candidates = identify_candidate_tokens(words=words, pole_catalog=pole_catalog)

    with pymupdf.open(resolved_path) as document:
        page_count = document.page_count

    word_counts_by_page: defaultdict[int, int] = defaultdict(int)
    for word in words:
        word_counts_by_page[word["page_number"]] += 1

    candidates_by_page: defaultdict[int, list[NativeCandidate]] = defaultdict(list)
    for candidate in candidates:
        candidates_by_page[candidate["page_number"]].append(candidate)

    page_summaries: list[PageNativeTextSummary] = []
    for page_number in range(1, page_count + 1):
        page_candidates = candidates_by_page[page_number]
        page_summaries.append(
            {
                "page_number": page_number,
                "page_index": page_number - 1,
                "native_word_count": word_counts_by_page[page_number],
                "candidate_count": len(page_candidates),
                "exact_match_count": sum(
                    candidate["match_status"] == "reference_exact" for candidate in page_candidates
                ),
                "pattern_match_count": sum(
                    candidate["match_status"] == "reference_pattern" for candidate in page_candidates
                ),
            }
        )

    pages_without_native_text = [
        page_number for page_number in range(1, page_count + 1) if word_counts_by_page[page_number] == 0
    ]
    exact_candidates = [c for c in candidates if c["match_status"] == "reference_exact"]
    pattern_candidates = [c for c in candidates if c["match_status"] == "reference_pattern"]

    return {
        "pdf_path": str(resolved_path),
        "analyzed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "page_count": page_count,
        "native_word_count": len(words),
        "pages_without_native_text": pages_without_native_text,
        "exact_match_occurrence_count": len(exact_candidates),
        "exact_unique_pole_ids": sorted({c["normalized_text"] for c in exact_candidates}),
        "pattern_candidate_count": len(pattern_candidates),
        "page_summaries": page_summaries,
        "candidates": candidates,
    }


def _evaluate_candidate(
    raw_text: str,
    page_number: int,
    page_index: int,
    bbox: list[float],
    reference_patterns: set[str],
    pole_catalog: PoleCatalog,
) -> NativeCandidate | None:
    candidate_text = str(raw_text).strip(_WRAPPER_CHARACTERS)
    if not candidate_text:
        return None

    normalized_text = normalize_pole_id(candidate_text)
    if not normalized_text:
        return None

    pattern = _character_pattern(normalized_text)
    exact_matches = pole_catalog.exact_matches(normalized_text)

    if exact_matches:
        match_status: CandidateStatus = "reference_exact"
    elif pattern in reference_patterns and _contains_letter_and_digit(normalized_text):
        match_status = "reference_pattern"
    else:
        return None

    return {
        "page_number": page_number,
        "page_index": page_index,
        "raw_text": raw_text,
        "normalized_text": normalized_text,
        "bbox": [round(float(value), 3) for value in bbox],
        "match_status": match_status,
        "match_count": len(exact_matches),
        "matched_source_ids": sorted({record["source_id"] for record in exact_matches}),
    }


def _append_unique_candidate(
    candidate: NativeCandidate | None,
    candidates: list[NativeCandidate],
    candidate_keys: set[tuple[int, tuple[float, ...], str, str]],
) -> None:
    if candidate is None:
        return

    candidate_key = (
        candidate["page_index"],
        tuple(candidate["bbox"]),
        candidate["normalized_text"],
        candidate["match_status"],
    )
    if candidate_key in candidate_keys:
        return

    candidate_keys.add(candidate_key)
    candidates.append(candidate)


def _character_pattern(value: str) -> str:
    return "".join("A" if c.isalpha() else "9" if c.isdigit() else c for c in value)


def _contains_letter_and_digit(value: str) -> bool:
    return any(c.isalpha() for c in value) and any(c.isdigit() for c in value)


def _combine_bboxes(first_bbox: list[float], second_bbox: list[float]) -> list[float]:
    return [
        round(min(first_bbox[0], second_bbox[0]), 3),
        round(min(first_bbox[1], second_bbox[1]), 3),
        round(max(first_bbox[2], second_bbox[2]), 3),
        round(max(first_bbox[3], second_bbox[3]), 3),
    ]
