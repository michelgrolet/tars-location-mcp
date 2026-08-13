"""Weather at a position the archive already knows about.

A location archive knows where you are, where you were, and where you are going to be on
day four of a trip. That is exactly the missing half of every weather question a person
actually asks: *will it rain here today*, not *what is the forecast for a pair of
coordinates*. So the forecast lives next to the coordinates rather than in a separate tool
that has to be told where you are.

Everything here talks to **Open-Meteo**, which is free for non-commercial use, needs no key,
and is the only aggregator that exposes the national weather services' raw model output
rather than one blended number. Standard library only, like the rest of this package.

Three of its endpoints, used for three different jobs:

* the **forecast API** for what it is doing now and the days ahead, through `best_match`,
  Open-Meteo's own per-location choice of the best available model;
* the **ensemble API** for anything phrased as a chance. A probability is a count over
  perturbed runs, so `will_it_rain` reads ~120 members from three independent centres and
  counts them. Nothing here invents a percentage from a single deterministic run;
* the **archive API** (ERA5 reanalysis) for what the weather actually was on a day you were
  somewhere, which is the one weather question the location archive alone can ask.

**"Best models" means a panel of independent forecasting centres, not a list of names.**
Each entry in `MODELS` is a distinct national service running its own global model, and each
`_seamless` id chains that centre's own high-resolution regional model over its domain into
its global one outside it: in France `meteofrance_seamless` is AROME at 1.3 km, over the US
`gfs_seamless` is HRRR at 3 km, elsewhere both fall back to the centre's global run. Models
that are *only* regional with someone else's global model behind them (KNMI, DMI, MET Norway)
are deliberately out: outside their domain they return ECMWF again, and a panel that counts
the same forecast twice reports false agreement.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from datetime import timezone as _tz

# The free hosts. With a commercial key the same paths live under customer-* instead, which
# is the only thing an API key changes here.
FORECAST_HOST = "https://api.open-meteo.com/v1/forecast"
ENSEMBLE_HOST = "https://ensemble-api.open-meteo.com/v1/ensemble"
ARCHIVE_HOST = "https://archive-api.open-meteo.com/v1/archive"
# Place names go here rather than to Nominatim. Nominatim's policy is one call a second and
# this package already spends that budget resolving every fix the phone sends; a forecast for
# a city the user has never been to is not worth competing with that.
GEOCODE_HOST = "https://geocoding-api.open-meteo.com/v1/search"
CUSTOMER = {
    FORECAST_HOST: "https://customer-api.open-meteo.com/v1/forecast",
    ENSEMBLE_HOST: "https://customer-ensemble-api.open-meteo.com/v1/ensemble",
    ARCHIVE_HOST: "https://customer-archive-api.open-meteo.com/v1/archive",
    GEOCODE_HOST: "https://customer-geocoding-api.open-meteo.com/v1/search",
}

MODELS = [
    ("ecmwf_ifs025", "ECMWF IFS", "European Centre, Reading"),
    ("gfs_seamless", "NOAA GFS / HRRR", "United States"),
    ("icon_seamless", "DWD ICON", "Germany"),
    ("meteofrance_seamless", "Météo-France ARPEGE / AROME", "France"),
    ("ukmo_seamless", "Met Office UM", "United Kingdom"),
    ("gem_seamless", "ECCC GEM", "Canada"),
    ("jma_seamless", "JMA GSM / MSM", "Japan"),
]

# The three ensembles with global coverage and enough members to count against: 51 + 40 + 31.
# Their spread is what a probability is made of, and their disagreement with each other is
# the honest confidence interval on top of it.
# Third column is what the centre's series are actually tagged with in the response, which is
# not the id you ask for: `gfs_seamless` comes back as `ncep_gefs_seamless`.
ENSEMBLES = [
    ("ecmwf_ifs025", "ECMWF ENS", "ecmwf"),
    ("icon_seamless", "DWD ICON-EPS", "icon"),
    ("gfs_seamless", "NOAA GEFS", "gefs"),
]

# WMO 4677, the codes every one of these models reports its sky state in.
WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog",
    51: "light drizzle", 53: "moderate drizzle", 55: "dense drizzle",
    56: "light freezing drizzle", 57: "dense freezing drizzle",
    61: "light rain", 63: "moderate rain", 65: "heavy rain",
    66: "light freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "moderate snow", 75: "heavy snow", 77: "snow grains",
    80: "light rain showers", 81: "moderate rain showers", 82: "violent rain showers",
    85: "light snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with light hail",
    99: "thunderstorm with heavy hail",
}

# A forecast is reissued every hour at best, so a cache this short costs nothing and keeps a
# chatty agent asking four questions in a row down to one call per endpoint. The reanalysis
# of a past day does not change at all, hence its own ttl.
FORECAST_TTL_S = 600
ARCHIVE_TTL_S = 86400
CACHE_ENTRIES = 64

_CACHE: dict[str, tuple[float, dict]] = {}


class WeatherError(RuntimeError):
    """Open-Meteo said no, or could not be reached. Carries their own reason when there is
    one: "cannot find model X" is a fixable message and "network unreachable" is not."""


# ------------------------------------------------------------------------------ the wire

def _options(settings=None):
    """Units, timeout and key, from Settings when the caller has one."""
    if settings is None:
        return {"units": "metric", "timeout": 15.0, "api_key": ""}
    return {"units": getattr(settings, "weather_units", "metric"),
            "timeout": float(getattr(settings, "weather_timeout_s", 15.0)),
            "api_key": getattr(settings, "weather_api_key", "") or ""}


def _units(unit_system):
    if (unit_system or "metric").lower().startswith("imp"):
        return {"temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
                "precipitation_unit": "inch"}
    return {}


def rain_threshold(unit_system, given=None):
    """What counts as "it rained" over a window, in whatever unit the answer is in.

    0.2 mm is the smallest total a person notices on the way to the car. Below it the
    ensemble is full of members with a hundredth of a millimetre of numerical drizzle, and
    counting those turns every overcast afternoon into a 90 % chance of rain.
    """
    if given is not None:
        return float(given)
    return 0.008 if (unit_system or "metric").lower().startswith("imp") else 0.2


def rain_rate_bar(unit_system):
    """The rate at which rain is actually falling rather than accumulating, per hour.

    A member counts as wet only if it clears this *and* the window total, and the two bars
    catch different failures. Total alone lets a hundredth of a millimetre an hour add up to
    a certainty of rain across a whole day. Rate alone lets one wet hour out of forty-eight
    stand for the weekend. This one stays fixed when a caller raises the total, because
    "at least two millimetres" is a statement about how much, not about how fast.
    """
    return 0.004 if (unit_system or "metric").lower().startswith("imp") else 0.1


def _fetch(host, params, ttl, timeout=15.0, api_key=""):
    url = CUSTOMER[host] if api_key else host
    query = dict(params)
    if api_key:
        query["apikey"] = api_key
    full = url + "?" + urllib.parse.urlencode(query, doseq=True)
    hit = _CACHE.get(full)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    try:
        with urllib.request.urlopen(full, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as err:
        # Open-Meteo puts the useful sentence in the body of a 400, not in the status line.
        detail = ""
        try:
            detail = (json.load(err) or {}).get("reason") or ""
        except Exception:
            pass
        raise WeatherError("open-meteo refused the request: %s" % (detail or err)) from None
    except Exception as err:
        raise WeatherError("cannot reach open-meteo: %s" % err) from None
    if payload.get("error"):
        raise WeatherError("open-meteo: %s" % payload.get("reason", "unknown error"))
    if len(_CACHE) >= CACHE_ENTRIES:
        _CACHE.pop(min(_CACHE, key=lambda k: _CACHE[k][0]), None)
    _CACHE[full] = (time.monotonic(), payload)
    return payload


# -------------------------------------------------------------------------- time slicing

def local_now(payload):
    """The wall clock at the point being forecast, as a naive datetime.

    Open-Meteo timestamps are naive local strings when `timezone=auto`, so comparing them to
    anything needs the same shape. The offset comes from the response rather than from a
    timezone we resolved ourselves: one source of truth per answer, and it is already right
    across a DST boundary because the API computed it for that instant.
    """
    return datetime.now(_tz.utc).replace(tzinfo=None) + timedelta(
        seconds=int(payload.get("utc_offset_seconds") or 0))


def day_window(payload, date=None, hours=None):
    """The window a rain question means, in local time: (start, end, label).

    No arguments means the rest of today, which is what "will it rain today" asks at 4 pm —
    not midnight to midnight, half of which has already happened. A date means that whole
    local day. `hours` means the next N hours whatever the date rolls over.
    """
    now = local_now(payload)
    if hours:
        start = now.replace(minute=0, second=0, microsecond=0)
        return start, start + timedelta(hours=int(hours)), "next %d hours" % int(hours)
    if date:
        start = datetime.fromisoformat(str(date)[:10])
        return start, start + timedelta(days=1), str(date)[:10]
    start = now.replace(minute=0, second=0, microsecond=0)
    midnight = start.replace(hour=0) + timedelta(days=1)
    return start, midnight, "rest of today"


def window_index(times, start, end):
    """Positions of the hourly steps inside [start, end). Empty when the window is past."""
    out = []
    for i, stamp in enumerate(times or []):
        moment = datetime.fromisoformat(stamp)
        if start <= moment < end:
            out.append(i)
    return out


# ------------------------------------------------------------------------------- reading

def series(payload, variable):
    """Every series for one variable, keyed by whatever follows its name.

    The forecast API suffixes each variable with the model (`precipitation_icon_seamless`);
    the ensemble API suffixes it with member and model
    (`precipitation_member07_icon_seamless_eps`) and leaves the control run unsuffixed. One
    reader handles both, because the shape is the same: the variable, then who produced it.
    """
    hourly = payload.get("hourly") or {}
    found = {}
    for key, values in hourly.items():
        if key == "time":
            continue
        if key == variable:
            found["control"] = values
        elif key.startswith(variable + "_"):
            found[key[len(variable) + 1:]] = values
    return found


def split_member(tag):
    """`member07_icon_seamless_eps` -> ("icon_seamless_eps", 7). Control runs come back 0."""
    if tag.startswith("member") and "_" in tag:
        number, _, rest = tag.partition("_")
        try:
            return rest, int(number[6:])
        except ValueError:
            return tag, 0
    return tag, 0


def ensemble_name(model_tag):
    """The centre behind an ensemble series tag, or the tag itself when it is not one of ours.

    The tag a series carries is not the id it was asked for: `icon_seamless` answers as
    `icon_seamless_eps` and `gfs_seamless` as `ncep_gefs_seamless`, which no amount of prefix
    matching turns back into the id. Hence the third column in ENSEMBLES.
    """
    for _, label, tag in ENSEMBLES:
        if tag in model_tag:
            return label
    return model_tag


def unit_of(payload, variable, default=""):
    """The unit a variable came back in, whatever suffix the series carries.

    Asking for one model or one ensemble member renames every key, so a plain lookup on the
    variable finds nothing and silently falls back to a default that is wrong the moment
    someone sets imperial units.
    """
    units = payload.get("hourly_units") or payload.get("daily_units") or {}
    for key, unit in units.items():
        if key == variable or key.startswith(variable + "_"):
            return unit
    return default


def covered(values, index):
    """A series is present at this point when it has at least one number inside the window.

    A model outside its domain answers with nulls rather than an error, so this is the check
    that keeps it out of a mean instead of letting it read as a forecast of zero.
    """
    return any(values[i] is not None for i in index if i < len(values))


def total(values, index):
    return sum(values[i] or 0.0 for i in index if i < len(values))


def percentile(sorted_values, p):
    """Linear-interpolated percentile over an already-sorted list."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * p
    low = math.floor(pos)
    high = math.ceil(pos)
    if low == high:
        return sorted_values[low]
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (pos - low)


def describe(code):
    if code is None:
        return None
    return WMO.get(int(code), "code %s" % int(code))


def verdict(chance_pct):
    """A percentage nobody has to interpret. The bands are deliberately wide: the difference
    between 44 % and 51 % is noise in any ensemble this size, and a word that changes there
    would be reporting precision the forecast does not have."""
    if chance_pct is None:
        return "unknown"
    if chance_pct < 10:
        return "no"
    if chance_pct < 30:
        return "unlikely"
    if chance_pct < 60:
        return "maybe"
    if chance_pct < 85:
        return "likely"
    return "yes"


# --------------------------------------------------------------------------------- tools

def geocode(name, settings=None):
    """Coordinates for a place named in words, or None when there is no such place.

    Only ever the fallback: a place the user has actually been is looked up in the archive
    first, and the spot they stood on beats the centroid of the city it is in. Ranking is by
    population, so "Paris" is the French one and not the one in Texas.
    """
    opt = _options(settings)
    payload = _fetch(GEOCODE_HOST, {"name": name, "count": 1, "language": "en",
                                    "format": "json"},
                     ARCHIVE_TTL_S, opt["timeout"], opt["api_key"])
    hits = payload.get("results") or []
    if not hits:
        return None
    hit = hits[0]
    return {"lat": hit["latitude"], "lon": hit["longitude"],
            "place": ", ".join(p for p in (hit.get("name"), hit.get("admin1"),
                                           hit.get("country")) if p),
            "timezone": hit.get("timezone")}


def conditions(lat, lon, settings=None):
    """What it is doing right now, plus the next twelve hours, from `best_match`.

    `best_match` is Open-Meteo's own per-location pick of the best available model, which is
    the right default for "what is it like outside". The chance of rain it carries is the
    single-model one; anything phrased as a probability should go through `rain_outlook`.
    """
    opt = _options(settings)
    payload = _fetch(FORECAST_HOST, {
        "latitude": lat, "longitude": lon, "timezone": "auto",
        "current": ("temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,"
                    "weather_code,cloud_cover,wind_speed_10m,wind_gusts_10m,wind_direction_10m,"
                    "pressure_msl,is_day"),
        "hourly": "temperature_2m,precipitation,precipitation_probability,weather_code",
        "daily": "sunrise,sunset,temperature_2m_max,temperature_2m_min,uv_index_max",
        "forecast_days": 2, **_units(opt["units"]),
    }, FORECAST_TTL_S, opt["timeout"], opt["api_key"])

    cur = payload.get("current") or {}
    units = payload.get("current_units") or {}
    now = local_now(payload)
    times = (payload.get("hourly") or {}).get("time") or []
    ahead = window_index(times, now.replace(minute=0, second=0, microsecond=0),
                         now + timedelta(hours=12))
    hourly = payload.get("hourly") or {}
    out = {
        "local_time": now.strftime("%Y-%m-%d %H:%M"),
        "timezone": payload.get("timezone"),
        "elevation_m": payload.get("elevation"),
        "sky": describe(cur.get("weather_code")),
        "temperature": cur.get("temperature_2m"),
        "feels_like": cur.get("apparent_temperature"),
        "humidity_pct": cur.get("relative_humidity_2m"),
        "cloud_cover_pct": cur.get("cloud_cover"),
        "precipitation_now": cur.get("precipitation"),
        "wind": cur.get("wind_speed_10m"),
        "gusts": cur.get("wind_gusts_10m"),
        "wind_direction_deg": cur.get("wind_direction_10m"),
        "pressure_hpa": cur.get("pressure_msl"),
        "daylight": bool(cur.get("is_day")),
        "units": {"temperature": units.get("temperature_2m"),
                  "wind": units.get("wind_speed_10m"),
                  "precipitation": units.get("precipitation")},
        "observed_at": cur.get("time"),
        "next_12h": [{
            "time": times[i][11:16],
            "temperature": (hourly.get("temperature_2m") or [None] * len(times))[i],
            "chance_of_rain_pct": (hourly.get("precipitation_probability")
                                   or [None] * len(times))[i],
            "precipitation": (hourly.get("precipitation") or [None] * len(times))[i],
            "sky": describe((hourly.get("weather_code") or [None] * len(times))[i]),
        } for i in ahead],
        "model": "best_match, Open-Meteo's per-location pick",
        "note": ("the chance of rain here is one model's own number. for a probability "
                 "counted over ensemble members, use will_it_rain."),
    }
    daily = payload.get("daily") or {}
    if daily.get("time"):
        out["today"] = {"sunrise": (daily.get("sunrise") or [None])[0],
                        "sunset": (daily.get("sunset") or [None])[0],
                        "high": (daily.get("temperature_2m_max") or [None])[0],
                        "low": (daily.get("temperature_2m_min") or [None])[0],
                        "uv_index_max": (daily.get("uv_index_max") or [None])[0]}
    return out


def rain_outlook(lat, lon, date=None, hours=None, threshold=None, settings=None):
    """The chance of rain over a window, counted over ~120 ensemble members.

    A probability is a fraction of runs, not a number a single forecast can produce. Three
    centres each run their model dozens of times from slightly different starting states;
    the share of those runs that end up wet *is* the chance of rain, and the disagreement
    between the three is the honest confidence on it.

    Each system is counted separately and then averaged, rather than pooling all members into
    one bucket. ECMWF ships 51 members and GEFS 31, so a pooled count would quietly weight
    the answer towards whoever runs the most members.
    """
    opt = _options(settings)
    cut = rain_threshold(opt["units"], threshold)
    payload = _fetch(ENSEMBLE_HOST, {
        "latitude": lat, "longitude": lon, "timezone": "auto",
        "hourly": "precipitation",
        "models": [m for m, _, _ in ENSEMBLES],
        "forecast_days": 4 if (date or hours) else 2, **_units(opt["units"]),
    }, FORECAST_TTL_S, opt["timeout"], opt["api_key"])

    times = (payload.get("hourly") or {}).get("time") or []
    start, end, label = day_window(payload, date, hours)
    index = window_index(times, start, end)
    meta = {"window": {"from": start.strftime("%Y-%m-%d %H:%M"),
                       "to": end.strftime("%Y-%m-%d %H:%M"),
                       "hours": len(index), "label": label,
                       "timezone": payload.get("timezone"),
                       "local_now": local_now(payload).strftime("%Y-%m-%d %H:%M")}}
    if not index:
        return {**meta, "found": False,
                "note": ("nothing forecast for that window. the ensemble runs about four "
                         "days out and cannot look backwards; use weather_history for a "
                         "date that has already happened.")}

    bar = rain_rate_bar(opt["units"])
    by_system, member_totals, per_hour = {}, [], [[] for _ in index]
    for tag, values in series(payload, "precipitation").items():
        model_tag, _ = split_member(tag)
        if not covered(values, index):
            continue
        system = ensemble_name(model_tag)
        inside = [values[i] or 0.0 for i in index if i < len(values)]
        run = {"total": sum(inside), "peak": max(inside, default=0.0)}
        by_system.setdefault(system, []).append(run)
        member_totals.append(run["total"])
        for slot, i in enumerate(index):
            if i < len(values) and values[i] is not None:
                per_hour[slot].append(values[i])

    if not member_totals:
        return {**meta, "found": False, "note": "no ensemble member covers this point"}

    def wet(run):
        return run["total"] >= cut and run["peak"] >= bar

    systems = {name: {"members": len(runs),
                      "chance_pct": round(100.0 * sum(1 for r in runs if wet(r)) / len(runs)),
                      "median": round(percentile(sorted(r["total"] for r in runs), 0.5), 2)}
               for name, runs in sorted(by_system.items())}
    # Equal weight per centre, not per member: see the docstring.
    chance = round(sum(s["chance_pct"] for s in systems.values()) / len(systems))
    spread = max(s["chance_pct"] for s in systems.values()) \
        - min(s["chance_pct"] for s in systems.values())
    ordered = sorted(member_totals)

    hours_out = []
    for slot, i in enumerate(index):
        runs = per_hour[slot]
        if not runs:
            continue
        falling = sum(1 for r in runs if r >= bar)
        hours_out.append({"time": times[i][11:16],
                          "chance_pct": round(100.0 * falling / len(runs)),
                          "median": round(percentile(sorted(runs), 0.5), 2),
                          "worst_case": round(percentile(sorted(runs), 0.9), 2)})

    peak = max(hours_out, key=lambda h: (h["chance_pct"], h["median"]), default=None)
    unit = unit_of(payload, "precipitation", "mm")
    return {
        **meta,
        "chance_of_rain_pct": chance,
        "verdict": verdict(chance),
        "expected": round(percentile(ordered, 0.5), 2),
        "range": {"p10": round(percentile(ordered, 0.1), 2),
                  "p50": round(percentile(ordered, 0.5), 2),
                  "p90": round(percentile(ordered, 0.9), 2),
                  "max": round(ordered[-1], 2)},
        "unit": unit,
        "threshold": cut,
        "hourly": hours_out,
        "wettest_hour": peak["time"] if peak and peak["chance_pct"] else None,
        "longest_dry_window": dry_window(hours_out),
        "by_system": systems,
        "members": len(member_totals),
        "agreement": ("the three ensembles agree" if spread <= 15
                      else "the ensembles disagree by %d points, treat the number as soft"
                           % spread),
        "method": ("share of ensemble members reaching %s %s over the window with at least "
                   "%s %s in one hour, averaged over the three centres so the one with the "
                   "most members does not dominate" % (cut, unit, bar, unit)),
    }


def dry_window(hours_out, max_chance=25):
    """The longest run of hours nobody expects rain in, which is the answer to the question
    behind the question: when do I go out. Returns None when there is no such run."""
    best, run = None, None
    for hour in hours_out:
        if hour["chance_pct"] <= max_chance:
            run = run or {"from": hour["time"], "hours": 0}
            run["to"] = hour["time"]
            run["hours"] += 1
        else:
            if run and (best is None or run["hours"] > best["hours"]):
                best = run
            run = None
    if run and (best is None or run["hours"] > best["hours"]):
        best = run
    if not best or best["hours"] < 2:
        return None
    return {**best, "at_most_pct": max_chance}


def daily(lat, lon, days=7, settings=None):
    """The days ahead: highs, lows, rain, wind, sun, one row per local day."""
    opt = _options(settings)
    payload = _fetch(FORECAST_HOST, {
        "latitude": lat, "longitude": lon, "timezone": "auto",
        "daily": ("weather_code,temperature_2m_max,temperature_2m_min,"
                  "apparent_temperature_max,precipitation_sum,precipitation_probability_max,"
                  "precipitation_hours,wind_speed_10m_max,wind_gusts_10m_max,sunrise,sunset,"
                  "uv_index_max"),
        "forecast_days": max(1, min(16, int(days))), **_units(opt["units"]),
    }, FORECAST_TTL_S, opt["timeout"], opt["api_key"])
    d = payload.get("daily") or {}
    units = payload.get("daily_units") or {}

    def col(name):
        return d.get(name) or [None] * len(d.get("time") or [])

    return {
        "timezone": payload.get("timezone"),
        "units": {"temperature": units.get("temperature_2m_max"),
                  "precipitation": units.get("precipitation_sum"),
                  "wind": units.get("wind_speed_10m_max")},
        "days": [{
            "date": day, "sky": describe(col("weather_code")[i]),
            "high": col("temperature_2m_max")[i], "low": col("temperature_2m_min")[i],
            "feels_like_high": col("apparent_temperature_max")[i],
            "precipitation": col("precipitation_sum")[i],
            "chance_of_rain_pct": col("precipitation_probability_max")[i],
            "rain_hours": col("precipitation_hours")[i],
            "wind_max": col("wind_speed_10m_max")[i], "gusts_max": col("wind_gusts_10m_max")[i],
            "uv_index_max": col("uv_index_max")[i],
            "sunrise": (col("sunrise")[i] or "")[11:16],
            "sunset": (col("sunset")[i] or "")[11:16],
        } for i, day in enumerate(d.get("time") or [])],
        "model": "best_match, Open-Meteo's per-location pick",
        "note": ("skill drops off sharply past about a week. for how much the centres "
                 "disagree on a given day, use weather_models."),
    }


def model_panel(lat, lon, variable="precipitation", hours=24, settings=None):
    """The same forecast from seven national weather services, side by side.

    This is the tool for "how sure is this really". Agreement between independent centres is
    the only cheap evidence a forecast is solid, and their disagreement is the only honest
    warning that it is not. A model with no data at this point is reported as not covering it
    rather than folded into the mean as a zero.
    """
    opt = _options(settings)
    variable = variable if variable in ("precipitation", "temperature_2m", "wind_speed_10m",
                                        "cloud_cover", "relative_humidity_2m") else "precipitation"
    payload = _fetch(FORECAST_HOST, {
        "latitude": lat, "longitude": lon, "timezone": "auto",
        "hourly": variable, "models": [m for m, _, _ in MODELS],
        "forecast_days": max(1, min(7, math.ceil(int(hours) / 24) + 1)), **_units(opt["units"]),
    }, FORECAST_TTL_S, opt["timeout"], opt["api_key"])

    times = (payload.get("hourly") or {}).get("time") or []
    start = local_now(payload).replace(minute=0, second=0, microsecond=0)
    index = window_index(times, start, start + timedelta(hours=int(hours)))
    found = series(payload, variable)
    cumulative = variable == "precipitation"

    rows, values = [], []
    for model_id, name, centre in MODELS:
        run = found.get(model_id)
        if run is None or not covered(run, index):
            rows.append({"model": name, "centre": centre, "covers_this_point": False})
            continue
        inside = [run[i] for i in index if i < len(run) and run[i] is not None]
        value = total(run, index) if cumulative else sum(inside) / len(inside)
        values.append(value)
        row = {"model": name, "centre": centre, "covers_this_point": True,
               ("total" if cumulative else "mean"): round(value, 2),
               "max": round(max(inside), 2)}
        # A flat series has no peak. Reporting the first hour as the peak of a dry day is a
        # number that looks like a finding and is an artefact of argmax over equal values.
        if max(inside) > min(inside):
            peak = max((i for i in index if i < len(run) and run[i] is not None),
                       key=lambda i: run[i])
            row["peak_hour"] = times[peak][11:16]
        rows.append(row)

    out = {"variable": variable, "unit": unit_of(payload, variable), "hours": len(index),
           "from": start.strftime("%Y-%m-%d %H:%M"),
           "timezone": payload.get("timezone"), "models": rows}
    if values:
        low, high = min(values), max(values)
        out["consensus"] = {"mean": round(sum(values) / len(values), 2),
                            "lowest": round(low, 2), "highest": round(high, 2),
                            "spread": round(high - low, 2),
                            "models_covering": len(values)}
        if cumulative:
            wet = sum(1 for v in values if v >= rain_threshold(opt["units"]))
            out["consensus"]["models_expecting_rain"] = "%d of %d" % (wet, len(values))
    return out


def history(lat, lon, date, settings=None):
    """What the weather actually was on a past day, from the ERA5 reanalysis.

    Reanalysis, not a saved forecast: the model is re-run after the fact with the
    observations that came in, so this is the closest thing to a record of the day. It lags
    real time by about five days, and the last few are filled by ERA5T, which is provisional.
    """
    opt = _options(settings)
    day = str(date)[:10]
    payload = _fetch(ARCHIVE_HOST, {
        "latitude": lat, "longitude": lon, "timezone": "auto",
        "start_date": day, "end_date": day,
        # No sunrise or sunset here, on purpose. The archive endpoint stamps a past date with
        # *today's* UTC offset, so a February day in Paris comes back an hour late: measured
        # 2026-08-13, sunrise 09:05 for a day it was 08:05. Daylight is a duration and does
        # not care about the offset, so that one is safe.
        "daily": ("weather_code,temperature_2m_max,temperature_2m_min,temperature_2m_mean,"
                  "apparent_temperature_max,precipitation_sum,rain_sum,snowfall_sum,"
                  "precipitation_hours,wind_speed_10m_max,wind_gusts_10m_max,"
                  "daylight_duration"),
        **_units(opt["units"]),
    }, ARCHIVE_TTL_S, opt["timeout"], opt["api_key"])
    d = payload.get("daily") or {}
    if not (d.get("time") or []):
        return {"date": day, "found": False,
                "note": "the reanalysis has nothing for that date yet; it lags about 5 days"}

    def first(name):
        return (d.get(name) or [None])[0]

    return {
        "date": day, "timezone": payload.get("timezone"),
        "sky": describe(first("weather_code")),
        "high": first("temperature_2m_max"), "low": first("temperature_2m_min"),
        "mean": first("temperature_2m_mean"),
        "feels_like_high": first("apparent_temperature_max"),
        "precipitation": first("precipitation_sum"), "rain": first("rain_sum"),
        "snow": first("snowfall_sum"), "rain_hours": first("precipitation_hours"),
        "wind_max": first("wind_speed_10m_max"), "gusts_max": first("wind_gusts_10m_max"),
        "daylight": ("%dh %02dm" % divmod(int((first("daylight_duration") or 0) // 60), 60)
                     if first("daylight_duration") else None),
        "units": {"temperature": (payload.get("daily_units") or {}).get("temperature_2m_max"),
                  "precipitation": (payload.get("daily_units") or {}).get("precipitation_sum")},
        "source": "ERA5 reanalysis, ~9 km, re-run with the observations that came in",
        "note": ("the day is cut at local midnight using today's UTC offset, so for a date on "
                 "the other side of a DST change the boundary sits an hour off."),
    }
