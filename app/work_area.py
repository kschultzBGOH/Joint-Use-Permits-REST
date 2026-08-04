"""Builds the work-area polygon from discovered pole locations.

Computed locally rather than via the Portal geometry service. The pole
coordinates are already in a projected coordinate system whose linear unit
is feet (POLE_COORDINATE_WKID, default 3734 -- NAD 1983 StatePlane Ohio
North), so buffering is plain planar math. Doing it here avoids a network
round-trip, a dependency on the geometry service being reachable and
permissioned, and any ambiguity in how that service's client wrapper wants
its geometry arguments shaped.
"""

from __future__ import annotations

import math

#: Circle approximation resolution. 64 segments puts the buffered area
#: within ~0.2% of a true circle, which is far finer than a work-area
#: boundary needs.
CIRCLE_SEGMENTS = 64


def _convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Andrew's monotone chain. Returns hull vertices counter-clockwise."""

    unique = sorted(set(points))
    if len(unique) <= 2:
        return unique

    def cross(o, a, b) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for point in unique:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)

    upper: list[tuple[float, float]] = []
    for point in reversed(unique):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)

    return lower[:-1] + upper[:-1]


def buffered_hull_ring(
    points: list[tuple[float, float]],
    buffer_distance: float,
    segments: int = CIRCLE_SEGMENTS,
) -> list[list[float]]:
    """Returns the outward-buffered convex hull as a closed clockwise ring.

    Works by hulling a ring of circle points placed around every input
    point: the convex hull of those circles is exactly the convex hull of
    the points expanded outward by the radius. That identity means one
    code path covers every case -- a single pole yields a circle, two
    yield a capsule, collinear poles yield a stadium, and duplicates
    collapse harmlessly -- with no degenerate special-casing.

    Clockwise because Esri treats a polygon's outer ring as clockwise; a
    counter-clockwise ring would be interpreted as a hole.
    """

    if not points:
        raise ValueError("At least one point is required to build a work area.")
    if buffer_distance <= 0:
        raise ValueError(f"buffer_distance must be positive, got {buffer_distance}.")

    circle_points = [
        (
            x + buffer_distance * math.cos(2 * math.pi * index / segments),
            y + buffer_distance * math.sin(2 * math.pi * index / segments),
        )
        for (x, y) in points
        for index in range(segments)
    ]

    hull = _convex_hull(circle_points)
    hull.reverse()
    return [[x, y] for (x, y) in hull] + [[hull[0][0], hull[0][1]]]


def build_work_area_polygon(
    points: list[tuple[float, float]], buffer_distance: float, wkid: int
) -> dict:
    """Esri JSON polygon covering every pole, buffered by buffer_distance."""

    return {
        "rings": [buffered_hull_ring(points, buffer_distance)],
        "spatialReference": {"wkid": wkid},
    }
