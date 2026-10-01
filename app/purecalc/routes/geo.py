"""Geospatial pure-compute routes - stdlib math only, no dependency."""

import math

from pydantic import BaseModel, Field

from app.purecalc.registry import ComputeError, ComputeSpec, register

EARTH_RADIUS_KM = 6371.0088  # IUGG mean radius (WGS84 authalic approximation)
UNIT_FACTORS_FROM_KM = {"km": 1.0, "mi": 0.621371, "nm": 0.539957}


class LatLon(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


# ---------------------------------------------------------------- distance
class DistanceInput(BaseModel):
    lat1: float = Field(ge=-90, le=90)
    lon1: float = Field(ge=-180, le=180)
    lat2: float = Field(ge=-90, le=90)
    lon2: float = Field(ge=-180, le=180)
    unit: str = Field(default="km", pattern="^(km|mi|nm)$")


class DistanceOutput(BaseModel):
    distance: float
    unit: str


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def compute_distance(inp: DistanceInput) -> DistanceOutput:
    km = _haversine_km(inp.lat1, inp.lon1, inp.lat2, inp.lon2)
    return DistanceOutput(distance=round(km * UNIT_FACTORS_FROM_KM[inp.unit], 4), unit=inp.unit)


register(ComputeSpec(
    slug="geo/distance", price="$0.001", service_name="geo-distance",
    description="Great-circle distance between two lat/lon points (haversine formula). Returns km, mi or nm.",
    tags=["geospatial", "distance", "haversine", "great circle", "latitude longitude", "gps"],
    input_model=DistanceInput, output_model=DistanceOutput, compute=compute_distance,
    sample_input={"lat1": 48.8566, "lon1": 2.3522, "lat2": 51.5074, "lon2": -0.1278, "unit": "km"},
    sample_output={"distance": 343.5577, "unit": "km"},
))


# ----------------------------------------------------------------- bearing
class BearingInput(BaseModel):
    lat1: float = Field(ge=-90, le=90)
    lon1: float = Field(ge=-180, le=180)
    lat2: float = Field(ge=-90, le=90)
    lon2: float = Field(ge=-180, le=180)


class BearingOutput(BaseModel):
    bearing_deg: float


def compute_bearing(inp: BearingInput) -> BearingOutput:
    p1, p2 = math.radians(inp.lat1), math.radians(inp.lat2)
    dlambda = math.radians(inp.lon2 - inp.lon1)
    x = math.sin(dlambda) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlambda)
    theta = math.atan2(x, y)
    return BearingOutput(bearing_deg=round((math.degrees(theta) + 360) % 360, 4))


register(ComputeSpec(
    slug="geo/bearing", price="$0.001", service_name="geo-bearing",
    description="Initial compass bearing (0-360 degrees, clockwise from true north) from point 1 to point 2.",
    tags=["geospatial", "bearing", "compass", "heading", "navigation", "gps"],
    input_model=BearingInput, output_model=BearingOutput, compute=compute_bearing,
    sample_input={"lat1": 48.8566, "lon1": 2.3522, "lat2": 51.5074, "lon2": -0.1278},
    sample_output={"bearing_deg": 337.9043},
))


# ---------------------------------------------------------------- midpoint
class MidpointInput(BaseModel):
    lat1: float = Field(ge=-90, le=90)
    lon1: float = Field(ge=-180, le=180)
    lat2: float = Field(ge=-90, le=90)
    lon2: float = Field(ge=-180, le=180)


class MidpointOutput(BaseModel):
    lat: float
    lon: float


def compute_midpoint(inp: MidpointInput) -> MidpointOutput:
    p1, l1 = math.radians(inp.lat1), math.radians(inp.lon1)
    p2 = math.radians(inp.lat2)
    dl = math.radians(inp.lon2 - inp.lon1)
    bx, by = math.cos(p2) * math.cos(dl), math.cos(p2) * math.sin(dl)
    p3 = math.atan2(
        math.sin(p1) + math.sin(p2),
        math.sqrt((math.cos(p1) + bx) ** 2 + by ** 2),
    )
    l3 = l1 + math.atan2(by, math.cos(p1) + bx)
    return MidpointOutput(
        lat=round(math.degrees(p3), 6),
        lon=round((math.degrees(l3) + 540) % 360 - 180, 6),
    )


register(ComputeSpec(
    slug="geo/midpoint", price="$0.001", service_name="geo-midpoint",
    description="Geographic (great-circle) midpoint between two lat/lon points.",
    tags=["geospatial", "midpoint", "great circle", "latitude longitude", "gps"],
    input_model=MidpointInput, output_model=MidpointOutput, compute=compute_midpoint,
    sample_input={"lat1": 48.8566, "lon1": 2.3522, "lat2": 51.5074, "lon2": -0.1278},
    sample_output={"lat": 50.1918, "lon": 1.0999},
))


# ---------------------------------------------------------------- geohash
_GEOHASH_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"


class GeohashInput(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    precision: int = Field(default=9, ge=1, le=12)


class GeohashOutput(BaseModel):
    geohash: str


def compute_geohash(inp: GeohashInput) -> GeohashOutput:
    lat_range = [-90.0, 90.0]
    lon_range = [-180.0, 180.0]
    geohash = []
    bits = 0
    bit_count = 0
    even_bit = True
    while len(geohash) < inp.precision:
        if even_bit:
            mid = (lon_range[0] + lon_range[1]) / 2
            if inp.lon >= mid:
                bits = (bits << 1) | 1
                lon_range[0] = mid
            else:
                bits = bits << 1
                lon_range[1] = mid
        else:
            mid = (lat_range[0] + lat_range[1]) / 2
            if inp.lat >= mid:
                bits = (bits << 1) | 1
                lat_range[0] = mid
            else:
                bits = bits << 1
                lat_range[1] = mid
        even_bit = not even_bit
        bit_count += 1
        if bit_count == 5:
            geohash.append(_GEOHASH_BASE32[bits])
            bits = 0
            bit_count = 0
    return GeohashOutput(geohash="".join(geohash))


register(ComputeSpec(
    slug="geo/geohash", price="$0.001", service_name="geo-geohash",
    description="Encode a lat/lon point into a geohash string (precision 1-12 characters).",
    tags=["geospatial", "geohash", "encode", "latitude longitude", "gps", "spatial index"],
    input_model=GeohashInput, output_model=GeohashOutput, compute=compute_geohash,
    sample_input={"lat": 48.8566, "lon": 2.3522, "precision": 9},
    sample_output={"geohash": "u09tvw0g2"},
))


# ---------------------------------------------------------- point-in-polygon
class PointInPolygonInput(BaseModel):
    point: LatLon
    polygon: list[LatLon] = Field(min_length=3)


class PointInPolygonOutput(BaseModel):
    inside: bool


def compute_point_in_polygon(inp: PointInPolygonInput) -> PointInPolygonOutput:
    x, y = inp.point.lon, inp.point.lat
    pts = [(p.lon, p.lat) for p in inp.polygon]
    inside = False
    n = len(pts)
    x1, y1 = pts[-1]
    for x2, y2 in pts:
        if ((y1 > y) != (y2 > y)) and (x < (x2 - x1) * (y - y1) / (y2 - y1) + x1):
            inside = not inside
        x1, y1 = x2, y2
    return PointInPolygonOutput(inside=inside)


register(ComputeSpec(
    slug="geo/point-in-polygon", price="$0.001", service_name="geo-point-in-polygon",
    description="Ray-casting point-in-polygon test (planar approximation - fine for city/region-scale polygons).",
    tags=["geospatial", "point in polygon", "geofence", "ray casting", "latitude longitude"],
    input_model=PointInPolygonInput, output_model=PointInPolygonOutput, compute=compute_point_in_polygon,
    sample_input={
        "point": {"lat": 1, "lon": 1},
        "polygon": [{"lat": 0, "lon": 0}, {"lat": 0, "lon": 4}, {"lat": 4, "lon": 4}, {"lat": 4, "lon": 0}],
    },
    sample_output={"inside": True},
))


# ---------------------------------------------------------------------- bbox
class BboxInput(BaseModel):
    points: list[LatLon] = Field(min_length=1)


class BboxOutput(BaseModel):
    min_lat: float
    max_lat: float
    min_lon: float
    max_lon: float


def compute_bbox(inp: BboxInput) -> BboxOutput:
    lats = [p.lat for p in inp.points]
    lons = [p.lon for p in inp.points]
    return BboxOutput(min_lat=min(lats), max_lat=max(lats), min_lon=min(lons), max_lon=max(lons))


register(ComputeSpec(
    slug="geo/bbox", price="$0.001", service_name="geo-bbox",
    description="Bounding box (min/max lat and lon) enclosing a list of points.",
    tags=["geospatial", "bounding box", "bbox", "extent", "latitude longitude"],
    input_model=BboxInput, output_model=BboxOutput, compute=compute_bbox,
    sample_input={"points": [{"lat": 48.8566, "lon": 2.3522}, {"lat": 51.5074, "lon": -0.1278}]},
    sample_output={"min_lat": 48.8566, "max_lat": 51.5074, "min_lon": -0.1278, "max_lon": 2.3522},
))


# ----------------------------------------------------------------------- dms
class DmsParts(BaseModel):
    degrees: int = Field(ge=0, le=180)
    minutes: int = Field(ge=0, le=59)
    seconds: float = Field(ge=0, lt=60)
    direction: str = Field(pattern="^(N|S|E|W)$")


class DmsInput(BaseModel):
    decimal: float | None = Field(default=None, ge=-180, le=180)
    dms: DmsParts | None = None


class DmsOutput(BaseModel):
    decimal: float | None = None
    degrees: int | None = None
    minutes: int | None = None
    seconds: float | None = None
    direction: str | None = None


def compute_dms(inp: DmsInput) -> DmsOutput:
    if (inp.decimal is None) == (inp.dms is None):
        raise ComputeError("ambiguous_input", "provide exactly one of decimal or dms")

    if inp.decimal is not None:
        direction = "W" if inp.decimal < 0 else "E"
        value = abs(inp.decimal)
        degrees = int(value)
        minutes_full = (value - degrees) * 60
        minutes = int(minutes_full)
        seconds = round((minutes_full - minutes) * 60, 4)
        return DmsOutput(degrees=degrees, minutes=minutes, seconds=seconds, direction=direction)

    sign = -1 if inp.dms.direction in ("S", "W") else 1
    decimal = sign * (inp.dms.degrees + inp.dms.minutes / 60 + inp.dms.seconds / 3600)
    return DmsOutput(decimal=round(decimal, 8))


register(ComputeSpec(
    slug="geo/dms", price="$0.001", service_name="geo-dms",
    description="Convert decimal degrees to degrees/minutes/seconds, or DMS back to decimal degrees.",
    tags=["geospatial", "dms", "degrees minutes seconds", "coordinate conversion", "latitude longitude"],
    input_model=DmsInput, output_model=DmsOutput, compute=compute_dms,
    sample_input={"decimal": 2.3522},
    sample_output={"degrees": 2, "minutes": 21, "seconds": 7.92, "direction": "E"},
))
