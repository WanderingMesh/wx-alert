"""Distance from the monitored point to a warning polygon."""

from __future__ import annotations

import pytest
from conftest import RENO_LATITUDE, RENO_LONGITUDE

from wx_alert.geo import geometry_distance_km
from wx_alert.nws import alert_geometry

# A square roughly 1 degree on a side, centred well away from anything real.
SQUARE = {
    "type": "Polygon",
    "coordinates": [[[-100.0, 40.0], [-100.0, 41.0], [-99.0, 41.0], [-99.0, 40.0],
                     [-100.0, 40.0]]],
}


class TestContainment:
    def test_a_point_inside_is_zero_away(self):
        assert geometry_distance_km(SQUARE, 40.5, -99.5) == 0.0

    def test_a_point_on_the_far_side_is_measured_from_the_edge(self):
        # One degree of longitude at 40.5 north is about 85 km, so a point one
        # degree west of the western edge should land near that.
        distance = geometry_distance_km(SQUARE, 40.5, -101.0)
        assert 80 < distance < 90

    def test_distance_north_uses_the_latitude_scale(self):
        # A degree of latitude is about 111 km everywhere.
        distance = geometry_distance_km(SQUARE, 42.0, -99.5)
        assert 105 < distance < 115

    def test_the_nearest_corner_is_used_diagonally(self):
        # Off the corner, the answer must be the corner distance rather than
        # the distance to either edge's perpendicular, which does not exist.
        distance = geometry_distance_km(SQUARE, 42.0, -101.0)
        assert distance > geometry_distance_km(SQUARE, 42.0, -99.5)


class TestGeometryHandling:
    def test_a_missing_geometry_is_none_not_infinity(self):
        # None means "no polygon", which the caller treats as keep. A large
        # number would mean "far away", which would drop every zone product.
        assert geometry_distance_km(None, 40.0, -100.0) is None

    @pytest.mark.parametrize(
        "geometry",
        [
            {},
            {"type": "Point", "coordinates": [-100.0, 40.0]},
            {"type": "Polygon"},
            {"type": "Polygon", "coordinates": "not a list"},
            "not a dict",
        ],
    )
    def test_unusable_geometry_is_none(self, geometry):
        assert geometry_distance_km(geometry, 40.0, -100.0) is None

    def test_malformed_coordinates_do_not_raise(self):
        # A delivery cycle must never die on a bad coordinate.
        broken = {"type": "Polygon", "coordinates": [[["x", "y"], [1, 2]]]}
        assert geometry_distance_km(broken, 40.0, -100.0) is None

    def test_multipolygon_uses_the_nearest_part(self):
        near = [[[-99.1, 40.0], [-99.1, 40.1], [-99.0, 40.1], [-99.0, 40.0],
                 [-99.1, 40.0]]]
        far = [[[-90.0, 40.0], [-90.0, 40.1], [-89.9, 40.1], [-89.9, 40.0],
                [-90.0, 40.0]]]
        multi = {"type": "MultiPolygon", "coordinates": [far, near]}

        distance = geometry_distance_km(multi, 40.05, -99.2)
        assert distance < 20

    def test_a_hole_is_not_inside(self):
        outer = [[-100.0, 40.0], [-100.0, 41.0], [-99.0, 41.0], [-99.0, 40.0],
                 [-100.0, 40.0]]
        hole = [[-99.7, 40.3], [-99.7, 40.7], [-99.3, 40.7], [-99.3, 40.3],
                [-99.7, 40.3]]
        donut = {"type": "Polygon", "coordinates": [outer, hole]}

        assert geometry_distance_km(donut, 40.5, -99.5) > 0
        assert geometry_distance_km(donut, 40.1, -99.5) == 0.0


class TestTheWarningThatWasMissed:
    """The regression this whole feature exists to prevent."""

    def test_the_polygon_does_not_cover_the_monitored_point(self, tornado_warning):
        # This is why a point query returned nothing: the warned area is east
        # of Reno, and the API intersects the coordinate with the polygon.
        distance = geometry_distance_km(
            alert_geometry(tornado_warning), RENO_LATITUDE, RENO_LONGITUDE
        )
        assert distance > 0

    def test_it_is_still_near_enough_to_matter_to_the_mesh(self, tornado_warning):
        # Roughly 15 km away: outside the polygon, well inside the range of
        # the radio network, and exactly the alert the operator wanted.
        distance = geometry_distance_km(
            alert_geometry(tornado_warning), RENO_LATITUDE, RENO_LONGITUDE
        )
        assert distance < 50

    def test_a_point_inside_the_warned_area_is_zero(self, tornado_warning):
        # Silver Springs, which the polygon does cover.
        distance = geometry_distance_km(
            alert_geometry(tornado_warning), 39.4088, -119.2263
        )
        assert distance == 0.0
