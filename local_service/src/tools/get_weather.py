from __future__ import annotations

import asyncio
import json
import urllib.parse
import urllib.request
from typing import Any

from .. import profile
from .base import ToolParam, ToolResult, tool

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HTTP_TIMEOUT = 6.0

WEATHER_CODES = {
    0: "clear",
    1: "mostly clear",
    2: "partly cloudy",
    3: "cloudy",
    45: "foggy",
    48: "foggy",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "freezing drizzle",
    57: "freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow",
    80: "rain showers",
    81: "rain showers",
    82: "heavy showers",
    85: "snow showers",
    86: "snow showers",
    95: "thunderstorms",
    96: "thunderstorms with hail",
    99: "thunderstorms with hail",
}


@tool(
    name="get_weather",
    description=(
        "Get current weather and today's forecast for a location. If no location "
        "is given, uses the user's saved home_city. Use for questions like "
        "'what's the weather' or 'will it rain today'."
    ),
    parameters=[
        ToolParam(
            name="location",
            type="string",
            description="City name (e.g. 'Chicago' or 'London, UK'). Omit for the user's default.",
            required=False,
        ),
        ToolParam(
            name="range",
            type="string",
            description="'now' for current conditions, 'today' for the daily forecast. Defaults to 'now'.",
            required=False,
        ),
    ],
    permission="read_only",
)
async def get_weather(
    location: str | None = None, range: str | None = None
) -> ToolResult:
    place = location or _default_location()
    if not place:
        return ToolResult(
            spoken="I don't know your location yet. Tell me a city or set your home city.",
            data={"error": "no_location"},
        )

    units = _preferred_units()
    temp_unit = "fahrenheit" if units == "imperial" else "celsius"
    deg_label = "°F" if units == "imperial" else "°C"

    try:
        geo = await asyncio.to_thread(_geocode, place)
    except Exception as e:
        return ToolResult(
            spoken=f"I couldn't look up the weather for {place}.",
            data={"error": "geocode_failed", "detail": str(e)},
        )
    if not geo:
        return ToolResult(
            spoken=f"I couldn't find a place called {place}.",
            data={"error": "unknown_location", "location": place},
        )

    try:
        forecast = await asyncio.to_thread(
            _forecast, geo["latitude"], geo["longitude"], temp_unit
        )
    except Exception as e:
        return ToolResult(
            spoken=f"The weather service didn't respond for {geo['name']}.",
            data={"error": "forecast_failed", "detail": str(e)},
        )

    pretty = _pretty_place(geo)
    mode = (range or "now").lower()
    if mode in ("today", "day", "daily", "forecast"):
        spoken = _format_today(forecast, pretty, deg_label)
    else:
        spoken = _format_now(forecast, pretty, deg_label)

    return ToolResult(
        spoken=spoken,
        data={
            "location": pretty,
            "latitude": geo["latitude"],
            "longitude": geo["longitude"],
            "units": units,
            "current": forecast.get("current"),
            "daily": forecast.get("daily"),
        },
    )


def _default_location() -> str | None:
    facts = profile.load().get("facts", {}) or {}
    for key in ("home_city", "default_location", "city", "location"):
        v = facts.get(key)
        if isinstance(v, str) and v.strip():
            return v
    return None


def _preferred_units() -> str:
    facts = profile.load().get("facts", {}) or {}
    v = facts.get("units") or facts.get("temperature_unit")
    if isinstance(v, str) and v.strip().lower() in ("metric", "celsius", "c", "°c"):
        return "metric"
    return "imperial"


def _http_get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{qs}", headers={"User-Agent": "Friday/0.1"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _geocode(name: str) -> dict[str, Any] | None:
    # Open-Meteo's geocoder matches the full `name` literally and won't accept
    # "City, State". Try the full string first, then progressively narrower
    # comma-separated prefixes, picking the result whose admin1/country also
    # matches a later segment when possible.
    segments = [s.strip() for s in name.split(",") if s.strip()]
    queries = [name] + ([segments[0]] if len(segments) > 1 else [])
    hint = " ".join(segments[1:]).lower() if len(segments) > 1 else ""
    for q in queries:
        data = _http_get_json(
            GEOCODE_URL, {"name": q, "count": 5, "language": "en", "format": "json"}
        )
        results = data.get("results") or []
        if not results:
            continue
        if hint:
            for r in results:
                blob = " ".join(
                    str(r.get(k) or "")
                    for k in ("admin1", "admin2", "country", "country_code")
                ).lower()
                if hint in blob:
                    return r
        return results[0]
    return None


def _forecast(lat: float, lon: float, temp_unit: str) -> dict[str, Any]:
    return _http_get_json(
        FORECAST_URL,
        {
            "latitude": lat,
            "longitude": lon,
            "temperature_unit": temp_unit,
            "wind_speed_unit": "mph" if temp_unit == "fahrenheit" else "kmh",
            "current": (
                "temperature_2m,apparent_temperature,weather_code,"
                "relative_humidity_2m,precipitation,wind_speed_10m,wind_gusts_10m,is_day"
            ),
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": "auto",
            "forecast_days": 1,
        },
    )


def _pretty_place(geo: dict[str, Any]) -> str:
    parts = [geo.get("name")]
    admin = geo.get("admin1")
    country = geo.get("country_code") or geo.get("country")
    if admin and admin != geo.get("name"):
        parts.append(admin)
    elif country:
        parts.append(country)
    return ", ".join(p for p in parts if p)


def _condition(code: Any) -> str:
    try:
        return WEATHER_CODES.get(int(code), "unclear conditions")
    except (TypeError, ValueError):
        return "unclear conditions"


def _format_now(forecast: dict[str, Any], place: str, deg: str) -> str:
    cur = forecast.get("current") or {}
    daily = forecast.get("daily") or {}
    temp = cur.get("temperature_2m")
    feels = cur.get("apparent_temperature")
    cond = _condition(cur.get("weather_code"))
    precip_now = cur.get("precipitation")
    wind = cur.get("wind_speed_10m")
    gusts = cur.get("wind_gusts_10m")
    wind_unit = "mph" if deg == "°F" else "km/h"
    high_threshold = 15 if wind_unit == "mph" else 25

    if temp is None:
        return f"It's {cond} in {place}."

    parts = [f"It's {round(temp)}{deg} and {cond} in {place}"]

    # "Feels like" only when materially different from actual temp.
    if isinstance(feels, (int, float)) and abs(feels - temp) >= 3:
        parts.append(f"feels like {round(feels)}{deg}")

    # Mention rain right now only if it's actually falling.
    if isinstance(precip_now, (int, float)) and precip_now > 0:
        parts.append("with rain falling now")

    # Wind only when it's notable enough to matter.
    if isinstance(wind, (int, float)) and wind >= high_threshold:
        if isinstance(gusts, (int, float)) and gusts >= wind + 5:
            parts.append(f"wind {round(wind)} {wind_unit} gusting to {round(gusts)}")
        else:
            parts.append(f"wind {round(wind)} {wind_unit}")

    sentence = ", ".join(parts) + "."

    # Tail with today's high/low + precip chance for context, when available.
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    pops = daily.get("precipitation_probability_max") or []
    if highs and lows:
        tail = f" High {round(highs[0])}{deg}, low {round(lows[0])}{deg}"
        if pops and isinstance(pops[0], (int, float)) and pops[0] >= 20:
            tail += f", {int(pops[0])}% chance of precipitation"
        sentence += tail + "."

    return sentence


def _format_today(forecast: dict[str, Any], place: str, deg: str) -> str:
    daily = forecast.get("daily") or {}
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    codes = daily.get("weather_code") or []
    pops = daily.get("precipitation_probability_max") or []
    if not highs or not codes:
        return _format_now(forecast, place, deg)
    cond = _condition(codes[0])
    high = round(highs[0])
    low = round(lows[0]) if lows else None
    pop = pops[0] if pops else None
    parts = [f"Today in {place}: {cond}, high {high}{deg}"]
    if low is not None:
        parts.append(f"low {low}{deg}")
    if isinstance(pop, (int, float)) and pop >= 20:
        parts.append(f"{int(pop)}% chance of precipitation")
    return ", ".join(parts) + "."
