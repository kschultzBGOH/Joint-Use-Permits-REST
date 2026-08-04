"""Render PDF pages for visual pole discovery.

Selects pages that need a visual fallback, creates inexpensive whole-page
previews, and renders overlapping high-resolution tiles directly from the
PDF without first allocating one very large page image.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypedDict

import pymupdf

RenderKind = Literal["preview", "tile"]

_VISUAL_CONTENT_TYPES = {"mixed", "likely_scanned", "image_only", "vector_or_empty"}


class VisualPageSelection(TypedDict):
    page_number: int
    page_index: int
    content_type: str
    reasons: list[str]


class RenderedImage(TypedDict):
    page_number: int
    page_index: int
    render_kind: RenderKind
    image_path: str
    dpi: int
    pixel_width: int
    pixel_height: int
    pdf_bbox: list[float]
    tile_index: int | None
    tile_row: int | None
    tile_column: int | None


class VisualRenderResult(TypedDict):
    pdf_path: str
    rendered_at: str
    selected_page_count: int
    preview_count: int
    tile_count: int
    selections: list[VisualPageSelection]
    previews: list[RenderedImage]
    tiles: list[RenderedImage]


class VisualRenderError(RuntimeError):
    """Raised when PDF page selection or rendering fails."""


def select_visual_pages(
    page_inspections: Iterable[Mapping[str, object]],
    native_discovery: Mapping[str, object],
    *,
    include_embedded_text_without_matches: bool = False,
) -> list[VisualPageSelection]:
    """Select pages that should continue to visual discovery.

    A page is selected when any of these is true: it has no native PDF
    text; intake classified it as mixed/likely_scanned/image_only/
    vector_or_empty; native discovery found a reference-shaped but
    non-exact candidate; or (when enabled) it has embedded text but no
    exact match.
    """

    pages_without_native_text = _integer_set(native_discovery.get("pages_without_native_text"))
    native_summaries = _native_summaries_by_page(native_discovery.get("page_summaries"))

    selections: list[VisualPageSelection] = []

    for position, page_inspection in enumerate(page_inspections, start=1):
        page_number = _positive_int(page_inspection.get("page_number"), default=position)
        page_index = _nonnegative_int(page_inspection.get("page_index"), default=page_number - 1)
        content_type = str(page_inspection.get("content_type") or "unknown").strip().lower()
        native_summary = native_summaries.get(page_number, {})
        exact_match_count = _nonnegative_int(native_summary.get("exact_match_count"), default=0)
        pattern_match_count = _nonnegative_int(native_summary.get("pattern_match_count"), default=0)

        reasons: list[str] = []

        if page_number in pages_without_native_text:
            reasons.append("no_native_text")
        if bool(page_inspection.get("likely_scanned_page")):
            reasons.append("likely_scanned_page")
        if content_type in _VISUAL_CONTENT_TYPES:
            reasons.append(f"content_type:{content_type}")
        if pattern_match_count > 0:
            reasons.append("native_pattern_candidate")
        if (
            include_embedded_text_without_matches
            and content_type == "embedded_text"
            and exact_match_count == 0
        ):
            reasons.append("embedded_text_without_exact_match")

        if reasons:
            selections.append(
                {
                    "page_number": page_number,
                    "page_index": page_index,
                    "content_type": content_type,
                    "reasons": reasons,
                }
            )

    selections.sort(key=lambda selection: selection["page_number"])
    return selections


def render_page_previews(
    pdf_path: str | Path,
    selections: Sequence[VisualPageSelection],
    output_directory: str | Path,
    *,
    dpi: int = 110,
) -> list[RenderedImage]:
    """Render inexpensive whole-page PNG previews for selected pages."""

    resolved_pdf = Path(pdf_path).expanduser().resolve()
    preview_directory = Path(output_directory).expanduser().resolve()
    preview_directory.mkdir(parents=True, exist_ok=True)

    rendered_images: list[RenderedImage] = []

    with pymupdf.open(resolved_pdf) as document:
        for selection in selections:
            page = document.load_page(selection["page_index"])
            page_rect = page.rect
            image_path = preview_directory / f"page_{selection['page_number']:04d}_preview.png"
            pixmap = page.get_pixmap(
                matrix=_render_matrix(dpi), colorspace=pymupdf.csRGB, alpha=False, annots=True
            )
            pixmap.save(image_path)
            rendered_images.append(
                _rendered_image_record(
                    selection=selection,
                    render_kind="preview",
                    image_path=image_path,
                    dpi=dpi,
                    pixmap=pixmap,
                    pdf_bbox=page_rect,
                )
            )

    return rendered_images


def render_page_tiles(
    pdf_path: str | Path,
    selections: Sequence[VisualPageSelection],
    output_directory: str | Path,
    *,
    dpi: int = 225,
    tile_pixels: int = 1800,
    overlap_ratio: float = 0.15,
) -> list[RenderedImage]:
    """Render overlapping high-resolution PNG tiles for selected pages.

    Tiles are rendered directly from PDF page regions -- this avoids
    creating one full high-DPI bitmap for a large sheet before dividing it.
    """

    resolved_pdf = Path(pdf_path).expanduser().resolve()
    tile_root = Path(output_directory).expanduser().resolve()
    tile_root.mkdir(parents=True, exist_ok=True)

    rendered_tiles: list[RenderedImage] = []

    with pymupdf.open(resolved_pdf) as document:
        for selection in selections:
            page = document.load_page(selection["page_index"])
            page_directory = tile_root / f"page_{selection['page_number']:04d}"
            page_directory.mkdir(parents=True, exist_ok=True)

            tile_rectangles = _page_tile_rectangles(
                page_rect=page.rect, dpi=dpi, tile_pixels=tile_pixels, overlap_ratio=overlap_ratio
            )

            for tile_index, (row, column, tile_rect) in enumerate(tile_rectangles, start=1):
                image_path = page_directory / f"tile_{tile_index:04d}_r{row:02d}_c{column:02d}.png"
                pixmap = page.get_pixmap(
                    matrix=_render_matrix(dpi),
                    clip=tile_rect,
                    colorspace=pymupdf.csRGB,
                    alpha=False,
                    annots=True,
                )
                pixmap.save(image_path)

                record = _rendered_image_record(
                    selection=selection,
                    render_kind="tile",
                    image_path=image_path,
                    dpi=dpi,
                    pixmap=pixmap,
                    pdf_bbox=tile_rect,
                )
                record["tile_index"] = tile_index
                record["tile_row"] = row
                record["tile_column"] = column
                rendered_tiles.append(record)

    return rendered_tiles


def prepare_visual_images(
    pdf_path: str | Path,
    page_inspections: Iterable[Mapping[str, object]],
    native_discovery: Mapping[str, object],
    output_directory: str | Path,
    *,
    preview_dpi: int = 110,
    include_embedded_text_without_matches: bool = False,
) -> VisualRenderResult:
    """Select visual pages and create previews (tiles come later, for the
    pages the preview pass actually flags as relevant)."""

    resolved_pdf = Path(pdf_path).expanduser().resolve()
    output_root = Path(output_directory).expanduser().resolve()

    selections = select_visual_pages(
        page_inspections=page_inspections,
        native_discovery=native_discovery,
        include_embedded_text_without_matches=include_embedded_text_without_matches,
    )

    previews = render_page_previews(
        pdf_path=resolved_pdf,
        selections=selections,
        output_directory=output_root / "previews",
        dpi=preview_dpi,
    )

    return {
        "pdf_path": str(resolved_pdf),
        "rendered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "selected_page_count": len(selections),
        "preview_count": len(previews),
        "tile_count": 0,
        "selections": selections,
        "previews": previews,
        "tiles": [],
    }


def _page_tile_rectangles(
    page_rect: pymupdf.Rect, dpi: int, tile_pixels: int, overlap_ratio: float
) -> list[tuple[int, int, pymupdf.Rect]]:
    tile_points = tile_pixels * 72.0 / dpi
    step_points = tile_points * (1.0 - overlap_ratio)

    x_starts = _axis_starts(length=page_rect.width, tile_length=tile_points, step_length=step_points)
    y_starts = _axis_starts(length=page_rect.height, tile_length=tile_points, step_length=step_points)

    rectangles: list[tuple[int, int, pymupdf.Rect]] = []
    for row, y_start in enumerate(y_starts, start=1):
        for column, x_start in enumerate(x_starts, start=1):
            x0 = page_rect.x0 + x_start
            y0 = page_rect.y0 + y_start
            x1 = min(x0 + tile_points, page_rect.x1)
            y1 = min(y0 + tile_points, page_rect.y1)
            rectangles.append((row, column, pymupdf.Rect(x0, y0, x1, y1)))

    return rectangles


def _axis_starts(length: float, tile_length: float, step_length: float) -> list[float]:
    if length <= tile_length:
        return [0.0]

    final_start = length - tile_length
    starts = [0.0]

    while True:
        next_start = starts[-1] + step_length
        if next_start >= final_start:
            if final_start - starts[-1] > 0.01:
                starts.append(final_start)
            break
        starts.append(next_start)

    return starts


def _rendered_image_record(
    selection: VisualPageSelection,
    render_kind: RenderKind,
    image_path: Path,
    dpi: int,
    pixmap: pymupdf.Pixmap,
    pdf_bbox: pymupdf.Rect,
) -> RenderedImage:
    return {
        "page_number": selection["page_number"],
        "page_index": selection["page_index"],
        "render_kind": render_kind,
        "image_path": str(image_path),
        "dpi": dpi,
        "pixel_width": pixmap.width,
        "pixel_height": pixmap.height,
        "pdf_bbox": [round(float(v), 3) for v in (pdf_bbox.x0, pdf_bbox.y0, pdf_bbox.x1, pdf_bbox.y1)],
        "tile_index": None,
        "tile_row": None,
        "tile_column": None,
    }


def _native_summaries_by_page(value: object) -> dict[int, Mapping[str, object]]:
    summaries: dict[int, Mapping[str, object]] = {}
    if not isinstance(value, list):
        return summaries

    for item in value:
        if not isinstance(item, Mapping):
            continue
        page_number = _positive_int(item.get("page_number"), default=0)
        if page_number > 0:
            summaries[page_number] = item

    return summaries


def _integer_set(value: object) -> set[int]:
    if not isinstance(value, list):
        return set()

    integers: set[int] = set()
    for item in value:
        try:
            integer = int(item)
        except (TypeError, ValueError):
            continue
        if integer > 0:
            integers.add(integer)

    return integers


def _render_matrix(dpi: int) -> pymupdf.Matrix:
    scale = dpi / 72.0
    return pymupdf.Matrix(scale, scale)


def _positive_int(value: object, default: int) -> int:
    try:
        integer = int(value)
    except (TypeError, ValueError):
        return default
    return integer if integer > 0 else default


def _nonnegative_int(value: object, default: int) -> int:
    try:
        integer = int(value)
    except (TypeError, ValueError):
        return default
    return integer if integer >= 0 else default
