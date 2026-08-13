"""Distance between the monitored point and an alert's warning polygon.

Storm-based warnings (tornado, severe thunderstorm, flash flood) are issued as
a polygon drawn around the threat. The county list attached to such a warning
is legacy dissemination metadata for NOAA Weather Radio and EAS, which can only
address whole counties; it is not the warned area.

That matters because alerts are fetched by county, and counties are not a
uniform unit. Arlington VA is about 12 km across. Washoe NV, which this program
was written for, is 315 km from end to end and reaches the Oregon border. A
county query alone would therefore mean something wildly different depending on
where the operator lives. This module supplies the distance test that makes the
county's size irrelevant, so the fetch stays broad enough to include every
polygon warning while relevance stays local.
"""

from __future__ import annotations

import math
from typing import Any

# Mean length of a degree. Latitude varies from 110.57 km at the equator to
# 111.69 km at the poles, so a single mean value is off by well under one
# percent anywhere in the US. That is far finer than the tens-of-kilometres
# radius this is used to test, and it avoids dragging in a geodesic library
# for a comparison whose threshold the operator picks by feel anyway.
KM_PER_DEGREE_LATITUDE = 111.132
KM_PER_DEGREE_LONGITUDE_AT_EQUATOR = 111.320


def _projection_scale(latitude: float) -> tuple[float, float]:
    """Return km per degree of longitude and latitude near a given latitude.

    An equirectangular projection centred on the query point. Meridians
    converge toward the poles, so longitude is scaled by cos(latitude); at
    Reno's 39.5 degrees a degree of longitude is about 86 km rather than 111.
    """
    return (
        KM_PER_DEGREE_LONGITUDE_AT_EQUATOR * math.cos(math.radians(latitude)),
        KM_PER_DEGREE_LATITUDE,
    )


def _polygons(geometry: Any) -> list[list[list[Any]]]:
    """Normalize GeoJSON geometry into a list of polygons, each a list of rings.

    NWS emits Polygon for essentially every storm-based warning, but a product
    covering disjoint areas can arrive as a MultiPolygon, and anything else
    (Point, LineString, or a missing geometry) has no area to measure.
    """
    if not isinstance(geometry, dict):
        return []

    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")

    if not isinstance(coordinates, list):
        return []

    if kind == "Polygon":
        return [coordinates]

    if kind == "MultiPolygon":
        return [polygon for polygon in coordinates if isinstance(polygon, list)]

    return []


def _ring_contains(ring: list[Any], longitude: float, latitude: float) -> bool:
    """Ray-casting containment test, performed in degrees.

    Whether a point falls inside a ring is a topological question, so it is
    unaffected by the projection; only the distance calculation needs metres.
    """
    inside = False
    count = len(ring)

    for index in range(count):
        current = ring[index]
        previous = ring[index - 1]

        if len(current) < 2 or len(previous) < 2:
            continue

        x1, y1 = float(current[0]), float(current[1])
        x2, y2 = float(previous[0]), float(previous[1])

        # Does the edge straddle the horizontal ray extending from the point?
        if (y1 > latitude) != (y2 > latitude):
            crossing = (x2 - x1) * (latitude - y1) / (y2 - y1) + x1
            if longitude < crossing:
                inside = not inside

    return inside


def _polygon_contains(
    rings: list[Any],
    longitude: float,
    latitude: float,
) -> bool:
    """Inside the outer ring and outside every hole.

    NWS warning polygons are simple and have never carried a hole, but
    honouring them costs one extra loop and avoids reporting a distance of
    zero for a point sitting in a gap.
    """
    if not rings or not _ring_contains(rings[0], longitude, latitude):
        return False

    return not any(
        _ring_contains(hole, longitude, latitude) for hole in rings[1:]
    )


def _segment_distance_km(
    ax: float,
    ay: float,
    bx: float,
    by: float,
) -> float:
    """Distance in km from the origin to segment AB, both already projected."""
    dx = bx - ax
    dy = by - ay

    if dx == 0.0 and dy == 0.0:
        return math.hypot(ax, ay)

    # Projection of the origin onto the infinite line, clamped to the segment
    # so the nearest point is an endpoint when the perpendicular falls outside.
    position = -(ax * dx + ay * dy) / (dx * dx + dy * dy)
    position = max(0.0, min(1.0, position))

    return math.hypot(ax + position * dx, ay + position * dy)


def _ring_distance_km(
    ring: list[Any],
    longitude: float,
    latitude: float,
    scale: tuple[float, float],
) -> float:
    """Shortest distance in km from the point to any edge of the ring."""
    lon_scale, lat_scale = scale
    nearest = math.inf

    projected = [
        (
            (float(vertex[0]) - longitude) * lon_scale,
            (float(vertex[1]) - latitude) * lat_scale,
        )
        for vertex in ring
        if isinstance(vertex, (list, tuple)) and len(vertex) >= 2
    ]

    for index in range(1, len(projected)):
        ax, ay = projected[index - 1]
        bx, by = projected[index]
        nearest = min(nearest, _segment_distance_km(ax, ay, bx, by))

    # A degenerate ring of a single vertex still has a meaningful distance.
    if nearest is math.inf and projected:
        ax, ay = projected[0]
        nearest = math.hypot(ax, ay)

    return nearest


def geometry_distance_km(
    geometry: Any,
    latitude: float,
    longitude: float,
) -> float | None:
    """Distance in km from a point to a GeoJSON geometry.

    Returns 0.0 when the point is inside, and None when the geometry is
    missing or is not an area. None is deliberately distinct from a large
    distance: "this alert has no polygon" is a different fact from "this
    alert's polygon is far away", and only the caller knows that a zone
    product without geometry should be kept rather than discarded.
    """
    polygons = _polygons(geometry)
    if not polygons:
        return None

    try:
        scale = _projection_scale(latitude)
        nearest = math.inf

        for rings in polygons:
            if _polygon_contains(rings, longitude, latitude):
                return 0.0

            for ring in rings:
                if isinstance(ring, list):
                    nearest = min(
                        nearest,
                        _ring_distance_km(ring, longitude, latitude, scale),
                    )
    except (TypeError, ValueError):
        # Malformed coordinates must not take down a delivery cycle. Treating
        # the geometry as absent lets the caller apply its keep-by-default
        # rule, which errs toward delivering rather than silently dropping.
        return None

    return None if nearest is math.inf else nearest
