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
    "&temperature_unit=fahrenheit"
    "&timezone=America%2FNew_York&forecast_days=1"
)

UMBRELLA_AT = 40  # % max precipitation probability


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
            daily = (fetch(timeout) or {}).get("daily") or {}
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
    return {
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
