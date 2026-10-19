"""Today's College Park forecast (Open-Meteo, no credential needed).

Returns the numbers STEP 5 needs for the masthead's weather line -- high,
low, rain chance -- plus the umbrella verdict. Never raises: any failure
comes back as {"error": ...} so the run degrades to "Weather not available
this morning" instead of stopping.
"""

import json
import time
import urllib.request

COLLEGE_PARK_URL = (
    "https://api.open-meteo.com/v1/forecast?"
    "latitude=38.9897&longitude=-76.9378"
    "&daily=precipitation_probability_max,temperature_2m_max,temperature_2m_min"
    "&current=temperature_2m,apparent_temperature,weather_code"
    "&temperature_unit=fahrenheit"
    "&timezone=America%2FNew_York&forecast_days=1"
)

UMBRELLA_AT = 40  # % max precipitation probability

# WMO weather codes -> words (Open-Meteo's `weather_code`).
CONDITIONS = {0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
              45: "Fog", 48: "Fog", 51: "Light drizzle", 53: "Drizzle",
              55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
              61: "Light rain", 63: "Rain", 65: "Heavy rain",
              66: "Freezing rain", 67: "Freezing rain", 71: "Light snow",
              73: "Snow", 75: "Heavy snow", 77: "Snow grains",
              80: "Light showers", 81: "Showers", 82: "Heavy showers",
              85: "Snow showers", 86: "Snow showers", 95: "Thunderstorms",
              96: "Thunderstorms with hail", 99: "Thunderstorms with hail"}


def _fetch(timeout):
    req = urllib.request.Request(
        COLLEGE_PARK_URL, headers={"User-Agent": "DailyBriefing/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _first(daily, key):
    vals = daily.get(key) or []
    v = vals[0] if vals else None
    return v if isinstance(v, (int, float)) else None


def get_today_weather(fetch=_fetch, retries=1, timeout=8) -> dict:
    last_exc = None
    for attempt in range(retries + 1):
        try:
            payload = fetch(timeout) or {}
            daily = payload.get("daily") or {}
            break
        except Exception as exc:  # noqa: BLE001 -- network: degrade, never raise
            last_exc = exc
            if attempt < retries:
                time.sleep(2)
    else:
        return {"error": str(last_exc), "umbrella_needed": False,
                "umbrella_note": None}

    precip = _first(daily, "precipitation_probability_max")
    hi = _first(daily, "temperature_2m_max")
    lo = _first(daily, "temperature_2m_min")
    if precip is None and hi is None:
        return {"error": "forecast response carried no daily values",
                "umbrella_needed": False, "umbrella_note": None}
    umbrella = precip is not None and precip >= UMBRELLA_AT
    cur = (payload.get("current") or {})
    now_t, feels = cur.get("temperature_2m"), cur.get("apparent_temperature")
    return {
        "temp_now": now_t if isinstance(now_t, (int, float)) else None,
        "feels_like": feels if isinstance(feels, (int, float)) else None,
        "conditions": CONDITIONS.get(cur.get("weather_code")),
        "precip_probability": precip,
        "temp_max": hi,
        "temp_min": lo,
        "umbrella_needed": umbrella,
        "umbrella_note": ("Bring an umbrella (%d%% rain chance)." % precip
                          if umbrella else None),
    }


def weather_line(forecast):
    """Masthead eyebrow: "High 78° · low 61° · bring an umbrella (60% rain)".

    None/{"error"} -> "Weather not available this morning" -- never a
    made-up forecast."""
    if not forecast or forecast.get("error"):
        return "Weather not available this morning"
    bits = []
    hi, lo = forecast.get("temp_max"), forecast.get("temp_min")
    if hi is not None:
        bits.append("High %d°" % round(hi))
    if lo is not None:
        bits.append("low %d°" % round(lo))
    p = forecast.get("precip_probability")
    if forecast.get("umbrella_needed"):
        bits.append("bring an umbrella (%d%% rain)" % p)
    elif p is not None:
        bits.append("%d%% rain · no umbrella needed" % p)
    else:
        bits.append("no umbrella needed")
    line = " · ".join(bits)
    return line[:1].upper() + line[1:]


def push_text(forecast):
    """Phone-notification body: the umbrella verdict first, then what a
    person checks on the way out the door. None when there is no forecast."""
    if not forecast or forecast.get("error"):
        return None
    p = forecast.get("precip_probability")
    if forecast.get("umbrella_needed"):
        head = "Bring an umbrella (%d%% rain)" % p
    elif p is not None:
        head = "No umbrella (%d%% rain)" % p
    else:
        head = "No umbrella needed"
    bits = [head]
    now_t, feels = forecast.get("temp_now"), forecast.get("feels_like")
    if now_t is not None:
        t = "%d° now" % round(now_t)
        if feels is not None and abs(feels - now_t) >= 5:
            t += " (feels %d°)" % round(feels)
        bits.append(t)
    hi, lo = forecast.get("temp_max"), forecast.get("temp_min")
    if hi is not None and lo is not None:
        bits.append("high %d° / low %d°" % (round(hi), round(lo)))
    if forecast.get("conditions"):
        bits.append(forecast["conditions"])
    return " · ".join(bits)
