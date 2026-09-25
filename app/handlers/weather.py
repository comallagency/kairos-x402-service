"""POST /weather — current conditions + short forecast for a city or lat/lon."""

from __future__ import annotations

import json

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import config, db
from app.receipts import Timer, effective_price, extract_payer_address, make_receipt, price_float
from app.x402_setup import ROUTE_DESCRIPTIONS

router = APIRouter()

_GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT = 20.0

_WMO = {
    0: "clear",
    1: "mainly_clear",
    2: "partly_cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing_rime_fog",
    51: "light_drizzle",
    53: "drizzle",
    55: "dense_drizzle",
    61: "slight_rain",
    63: "rain",
    65: "heavy_rain",
    71: "slight_snow",
    73: "snow",
    75: "heavy_snow",
    80: "rain_showers",
    81: "rain_showers",
    82: "violent_rain_showers",
    95: "thunderstorm",
    96: "thunderstorm_hail",
    99: "thunderstorm_heavy_hail",
}

SAMPLE_RESPONSE = {
    "query": "Paris",
    "location": {
        "name": "Paris",
        "country": "France",
        "country_code": "FR",
        "latitude": 48.85341,
        "longitude": 2.3488,
        "timezone": "Europe/Paris",
    },
    "current": {
        "time": "2026-09-19T13:45",
        "temperature_c": 23.8,
        "humidity_pct": 48,
        "wind_speed_kmh": 12.2,
        "weather_code": 1,
        "conditions": "mainly_clear",
    },
    "daily": [
        {
            "date": "2026-09-19",
            "temperature_max_c": 25.1,
            "temperature_min_c": 15.2,
            "precipitation_sum_mm": 0.0,
            "weather_code": 1,
            "conditions": "mainly_clear",
        }
    ],
}


class WeatherError(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


async def _geocode(city: str) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(_GEOCODE, params={"name": city, "count": 1, "language": "en"})
        resp.raise_for_status()
        data = resp.json()
    results = data.get("results") or []
    if not results:
        raise WeatherError("location_not_found")
    hit = results[0]
    return {
        "name": hit.get("name") or city,
        "country": hit.get("country"),
        "country_code": hit.get("country_code"),
        "latitude": hit["latitude"],
        "longitude": hit["longitude"],
        "timezone": hit.get("timezone") or "UTC",
    }


async def _forecast(lat: float, lon: float, timezone: str) -> dict:
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum",
        "forecast_days": 3,
        "timezone": timezone or "auto",
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(_FORECAST, params=params)
        resp.raise_for_status()
        return resp.json()


async def _lookup(body: dict) -> dict:
    city = (body.get("city") or body.get("q") or body.get("location") or "").strip()
    lat = body.get("lat") if body.get("lat") is not None else body.get("latitude")
    lon = body.get("lon") if body.get("lon") is not None else body.get("longitude")

    if city:
        location = await _geocode(city)
        query = city
    elif lat is not None and lon is not None:
        try:
            lat_f, lon_f = float(lat), float(lon)
        except (TypeError, ValueError) as exc:
            raise WeatherError("invalid_coordinates") from exc
        location = {
            "name": None,
            "country": None,
            "country_code": None,
            "latitude": lat_f,
            "longitude": lon_f,
            "timezone": "auto",
        }
        query = f"{lat_f},{lon_f}"
    else:
        raise WeatherError("missing_location")

    raw = await _forecast(location["latitude"], location["longitude"], location["timezone"])
    cur = raw.get("current") or {}
    code = cur.get("weather_code")
    daily_raw = raw.get("daily") or {}
    days = []
    times = daily_raw.get("time") or []
    for i, date in enumerate(times):
        dcode = (daily_raw.get("weather_code") or [None])[i]
        days.append(
            {
                "date": date,
                "temperature_max_c": (daily_raw.get("temperature_2m_max") or [None])[i],
                "temperature_min_c": (daily_raw.get("temperature_2m_min") or [None])[i],
                "precipitation_sum_mm": (daily_raw.get("precipitation_sum") or [None])[i],
                "weather_code": dcode,
                "conditions": _WMO.get(dcode, "unknown"),
            }
        )
    if location.get("timezone") == "auto":
        location["timezone"] = raw.get("timezone") or "UTC"
    return {
        "query": query,
        "location": location,
        "current": {
            "time": cur.get("time"),
            "temperature_c": cur.get("temperature_2m"),
            "humidity_pct": cur.get("relative_humidity_2m"),
            "wind_speed_kmh": cur.get("wind_speed_10m"),
            "weather_code": code,
            "conditions": _WMO.get(code, "unknown"),
        },
        "daily": days,
    }


@router.get("/weather/sample", openapi_extra={"security": []})
async def weather_sample():
    return {**SAMPLE_RESPONSE, "x402_receipt": make_receipt(None, "weather", 1, 0.0)}


async def _weather_paid(request: Request, body: dict):
    payer = extract_payer_address(request)
    user_agent = request.headers.get("user-agent")
    body_excerpt = json.dumps(body)[:2000]
    method = request.method

    try:
        with Timer() as t:
            result = await _lookup(body)
    except WeatherError as exc:
        db.log_request(
            route="weather", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=exc.reason,
        )
        code = 404 if exc.reason == "location_not_found" else 400
        return JSONResponse({"error": {"reason": exc.reason}}, status_code=code)
    except Exception as exc:
        db.log_request(
            route="weather", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=str(exc)[:200],
        )
        return JSONResponse(
            {"error": {"reason": "upstream_error", "detail": str(exc)[:200]}}, status_code=502
        )

    price = effective_price(payer, price_float(config.PRICE_WEATHER))
    db.log_request(
        route="weather", method=method, status="paid", latency_ms=t.elapsed_ms,
        amount_usdc=price, payer=payer, user_agent=user_agent, body_excerpt=body_excerpt,
    )
    receipt = make_receipt(None, "weather", t.elapsed_ms, price)
    return {**result, "x402_receipt": receipt}


@router.get("/weather", description=ROUTE_DESCRIPTIONS["weather"])
async def weather_get(
    request: Request,
    city: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    q: str | None = None,
):
    body = {k: v for k, v in (("city", city or q), ("lat", lat), ("lon", lon)) if v is not None}
    return await _weather_paid(request, body)


@router.post("/weather", description=ROUTE_DESCRIPTIONS["weather"])
async def weather(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _weather_paid(request, body)
