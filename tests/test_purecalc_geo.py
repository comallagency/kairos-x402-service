"""5 reference-verified cases per route (CLAUDE.md pure-compute pack rule:
a route that fails its tests is not deployed). References are either (a)
pure-geometry facts independent of the haversine/bearing/midpoint code
itself (circumference, symmetry, cardinal directions), (b) a widely
published worked example (Wikipedia's Geohash article), or (c) the
published algorithm traced by hand step by step - never "run the code,
copy its output.\""""

import math

import pytest

from app.purecalc.registry import ComputeError
from app.purecalc.routes.geo import (
    BboxInput, DistanceInput, DmsInput, DmsParts, GeohashInput, LatLon,
    MidpointInput, PointInPolygonInput, BearingInput,
    compute_bbox, compute_bearing, compute_distance, compute_dms,
    compute_geohash, compute_midpoint, compute_point_in_polygon,
    EARTH_RADIUS_KM,
)


# --------------------------------------------------------------- distance
def test_distance_zero():
    r = compute_distance(DistanceInput(lat1=48.8566, lon1=2.3522, lat2=48.8566, lon2=2.3522))
    assert r.distance == 0.0


def test_distance_quarter_circumference_equator_to_pole():
    # Independent of haversine's own code: a quarter of a great circle is
    # exactly (pi/2) * R by the definition of radians, not by this formula.
    expected = (math.pi / 2) * EARTH_RADIUS_KM
    r = compute_distance(DistanceInput(lat1=0, lon1=0, lat2=90, lon2=0))
    assert r.distance == pytest.approx(expected, abs=0.01)


def test_distance_half_circumference():
    expected = math.pi * EARTH_RADIUS_KM
    r = compute_distance(DistanceInput(lat1=0, lon1=0, lat2=0, lon2=180))
    assert r.distance == pytest.approx(expected, abs=0.01)


def test_distance_one_degree_longitude_at_equator():
    expected = EARTH_RADIUS_KM * math.pi / 180  # ~111.195 km
    r = compute_distance(DistanceInput(lat1=0, lon1=0, lat2=0, lon2=1))
    assert r.distance == pytest.approx(expected, abs=0.01)


def test_distance_paris_london_published_figure():
    # Commonly published great-circle distance Paris-London ~343-344 km.
    r = compute_distance(DistanceInput(lat1=48.8566, lon1=2.3522, lat2=51.5074, lon2=-0.1278))
    assert r.distance == pytest.approx(343.5, abs=2.0)


# ---------------------------------------------------------------- bearing
def test_bearing_due_north():
    r = compute_bearing(BearingInput(lat1=0, lon1=0, lat2=1, lon2=0))
    assert r.bearing_deg == pytest.approx(0.0, abs=0.01)


def test_bearing_due_east():
    r = compute_bearing(BearingInput(lat1=0, lon1=0, lat2=0, lon2=1))
    assert r.bearing_deg == pytest.approx(90.0, abs=0.01)


def test_bearing_due_south():
    r = compute_bearing(BearingInput(lat1=0, lon1=0, lat2=-1, lon2=0))
    assert r.bearing_deg == pytest.approx(180.0, abs=0.01)


def test_bearing_due_west():
    r = compute_bearing(BearingInput(lat1=0, lon1=0, lat2=0, lon2=-1))
    assert r.bearing_deg == pytest.approx(270.0, abs=0.01)


def test_bearing_paris_london_is_northwest_quadrant():
    r = compute_bearing(BearingInput(lat1=48.8566, lon1=2.3522, lat2=51.5074, lon2=-0.1278))
    assert 300.0 < r.bearing_deg < 350.0


# --------------------------------------------------------------- midpoint
def test_midpoint_same_point():
    r = compute_midpoint(MidpointInput(lat1=48.8566, lon1=2.3522, lat2=48.8566, lon2=2.3522))
    assert r.lat == pytest.approx(48.8566, abs=0.0001)
    assert r.lon == pytest.approx(2.3522, abs=0.0001)


def test_midpoint_equatorial_symmetry():
    # Two equatorial points symmetric around lon=1 -> midpoint lon=1, lat=0.
    r = compute_midpoint(MidpointInput(lat1=0, lon1=0, lat2=0, lon2=2))
    assert r.lat == pytest.approx(0.0, abs=0.0001)
    assert r.lon == pytest.approx(1.0, abs=0.0001)


def test_midpoint_symmetric_about_origin():
    r = compute_midpoint(MidpointInput(lat1=0, lon1=-10, lat2=0, lon2=10))
    assert r.lat == pytest.approx(0.0, abs=0.0001)
    assert r.lon == pytest.approx(0.0, abs=0.0001)


def test_midpoint_point_symmetry_through_origin():
    r = compute_midpoint(MidpointInput(lat1=5, lon1=-5, lat2=-5, lon2=5))
    assert r.lat == pytest.approx(0.0, abs=0.001)
    assert r.lon == pytest.approx(0.0, abs=0.001)


def test_midpoint_paris_london_between_the_two():
    r = compute_midpoint(MidpointInput(lat1=48.8566, lon1=2.3522, lat2=51.5074, lon2=-0.1278))
    assert 48.8566 < r.lat < 51.5074
    assert -0.1278 < r.lon < 2.3522


# ---------------------------------------------------------------- geohash
def test_geohash_origin_precision_1_traced_by_hand():
    # Algorithm traced by hand (bits 1,1,0,0,0 = 0b11000 = 24 ->
    # base32 "0123456789bcdefghjkmnpqrstuvwxyz"[24] = 's').
    r = compute_geohash(GeohashInput(lat=0, lon=0, precision=1))
    assert r.geohash == "s"


def test_geohash_south_west_quadrant_precision_1():
    # lon<0 -> first bit 0, lat<0 -> second bit 0, lon<-90 -> 0,
    # lat<-45 -> 0, lon<-135 -> 0 => 00000 = 0 -> '0'.
    r = compute_geohash(GeohashInput(lat=-80, lon=-170, precision=1))
    assert r.geohash == "0"


def test_geohash_wikipedia_published_example():
    # Wikipedia "Geohash" article's own worked example.
    r = compute_geohash(GeohashInput(lat=57.64911, lon=10.40744, precision=11))
    assert r.geohash == "u4pruydqqvj"


def test_geohash_precision_length():
    for p in (1, 5, 9, 12):
        r = compute_geohash(GeohashInput(lat=48.8566, lon=2.3522, precision=p))
        assert len(r.geohash) == p


def test_geohash_is_prefix_stable():
    # A shorter geohash for the same point must be a prefix of the longer
    # one (property of the algorithm's bit-by-bit refinement).
    short = compute_geohash(GeohashInput(lat=48.8566, lon=2.3522, precision=5)).geohash
    long_ = compute_geohash(GeohashInput(lat=48.8566, lon=2.3522, precision=9)).geohash
    assert long_.startswith(short)


# ---------------------------------------------------------- point-in-polygon
_SQUARE = [LatLon(lat=0, lon=0), LatLon(lat=0, lon=4), LatLon(lat=4, lon=4), LatLon(lat=4, lon=0)]


def test_pip_inside_square():
    r = compute_point_in_polygon(PointInPolygonInput(point=LatLon(lat=2, lon=2), polygon=_SQUARE))
    assert r.inside is True


def test_pip_outside_square():
    r = compute_point_in_polygon(PointInPolygonInput(point=LatLon(lat=10, lon=10), polygon=_SQUARE))
    assert r.inside is False


def test_pip_just_outside_edge():
    r = compute_point_in_polygon(PointInPolygonInput(point=LatLon(lat=2, lon=4.5), polygon=_SQUARE))
    assert r.inside is False


def test_pip_triangle_centroid_inside():
    triangle = [LatLon(lat=0, lon=0), LatLon(lat=0, lon=6), LatLon(lat=6, lon=3)]
    r = compute_point_in_polygon(PointInPolygonInput(point=LatLon(lat=2, lon=3), polygon=triangle))
    assert r.inside is True


def test_pip_far_outside():
    r = compute_point_in_polygon(PointInPolygonInput(point=LatLon(lat=-50, lon=-50), polygon=_SQUARE))
    assert r.inside is False


# --------------------------------------------------------------------- bbox
def test_bbox_single_point():
    r = compute_bbox(BboxInput(points=[LatLon(lat=5, lon=10)]))
    assert (r.min_lat, r.max_lat, r.min_lon, r.max_lon) == (5, 5, 10, 10)


def test_bbox_two_points():
    r = compute_bbox(BboxInput(points=[LatLon(lat=48.8566, lon=2.3522), LatLon(lat=51.5074, lon=-0.1278)]))
    assert r.min_lat == 48.8566 and r.max_lat == 51.5074
    assert r.min_lon == -0.1278 and r.max_lon == 2.3522


def test_bbox_negative_coords():
    r = compute_bbox(BboxInput(points=[LatLon(lat=-10, lon=-20), LatLon(lat=-5, lon=-25)]))
    assert (r.min_lat, r.max_lat, r.min_lon, r.max_lon) == (-10, -5, -25, -20)


def test_bbox_many_points():
    pts = [LatLon(lat=i, lon=-i) for i in range(5)]
    r = compute_bbox(BboxInput(points=pts))
    assert (r.min_lat, r.max_lat, r.min_lon, r.max_lon) == (0, 4, -4, 0)


def test_bbox_identical_points():
    r = compute_bbox(BboxInput(points=[LatLon(lat=1, lon=1), LatLon(lat=1, lon=1)]))
    assert (r.min_lat, r.max_lat, r.min_lon, r.max_lon) == (1, 1, 1, 1)


# ---------------------------------------------------------------------- dms
def test_dms_decimal_to_dms_hand_computed():
    # 2.3522 = 2 deg + 0.3522*60=21.132 min + 0.132*60=7.92 sec.
    r = compute_dms(DmsInput(decimal=2.3522))
    assert (r.degrees, r.minutes, r.direction) == (2, 21, "E")
    assert r.seconds == pytest.approx(7.92, abs=0.01)


def test_dms_negative_decimal_is_west():
    r = compute_dms(DmsInput(decimal=-5.5))
    assert (r.degrees, r.minutes, r.seconds, r.direction) == (5, 30, 0.0, "W")


def test_dms_to_decimal_roundtrip():
    r = compute_dms(DmsInput(dms=DmsParts(degrees=2, minutes=21, seconds=7.92, direction="E")))
    assert r.decimal == pytest.approx(2.3522, abs=0.0001)


def test_dms_south_direction_is_negative():
    r = compute_dms(DmsInput(dms=DmsParts(degrees=48, minutes=51, seconds=24, direction="S")))
    assert r.decimal == pytest.approx(-48.8567, abs=0.001)


def test_dms_ambiguous_input_rejected():
    with pytest.raises(ComputeError):
        compute_dms(DmsInput(decimal=1.0, dms=DmsParts(degrees=1, minutes=0, seconds=0, direction="E")))
    with pytest.raises(ComputeError):
        compute_dms(DmsInput())
