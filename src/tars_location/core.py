"""Geocoding, time windows, and the stay maths. Everything above this file reads it.

The poller writes pings and the MCP reads history, and both need the same rules about what
counts as the same place and what "last week" means, so those rules live here once.

History is read from `location_v_stays` rather than from the ping table. An imported point
arrives with no place attached, so a ping-only reader sees the last few weeks of an archive
that may go back years, and says nothing about the difference.
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from .config import Settings
from .db import Database

_settings: Settings | None = None
_db: Database | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.load()
    return _settings


def db() -> Database:
    global _db
    if _db is None:
        _db = Database(settings())
    return _db


def use(new_settings: Settings) -> None:
    """Point the process at a different database. Tests and the CLI use this."""
    global _settings, _db
    _settings, _db = new_settings, Database(new_settings)


def execute(sql, params=None):
    db().execute(sql, params or ())


# --------------------------------------------------------------------------- geography

def haversine_m(lat0, lon0, lat1, lon1):
    """Metres between two positions. Infinite when either side is missing, so a caller
    comparing against a radius treats an unknown position as "not here" rather than "here"."""
    if lat0 is None or lon0 is None or lat1 is None or lon1 is None:
        return float("inf")
    r, p = 6371000.0, math.pi / 180
    a = (math.sin((lat1 - lat0) * p / 2) ** 2
         + math.cos(lat0 * p) * math.cos(lat1 * p) * math.sin((lon1 - lon0) * p / 2) ** 2)
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def reverse_geocode(lat, lon):
    """Nominatim, zoom 18, English. Returns a place dict, or None when the call fails.

    Both parameters are load-bearing and were learned the hard way. zoom=14 stops at the
    commune, which turns a position known to 17 metres into the name of a village three
    kilometres away. Without accept-language, a multilingual country answers in all of its
    languages at once and the country comes back as "Belgie / Belgique / Belgien".
    """
    url = ("https://nominatim.openstreetmap.org/reverse?"
           + urllib.parse.urlencode({"lat": lat, "lon": lon, "format": "jsonv2",
                                     "zoom": "18", "accept-language": "en"}))
    req = urllib.request.Request(url, headers={"User-Agent": settings().user_agent()})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            payload = json.load(r) or {}
    except Exception:
        return None
    a = payload.get("address") or {}
    city = (a.get("city") or a.get("town") or a.get("village")
            or a.get("municipality") or a.get("hamlet") or "")
    area = a.get("suburb") or a.get("city_district") or a.get("neighbourhood") or ""
    admin = a.get("state") or a.get("region") or a.get("county") or ""
    country = a.get("country") or ""
    code = (a.get("country_code") or "").lower()
    road = a.get("road") or a.get("pedestrian") or a.get("footway") or ""
    num = a.get("house_number") or ""
    # Out in the countryside or inside a park there is no street, and the name of the place
    # says more than the commune on its own.
    spot = a.get("amenity") or a.get("building") or a.get("shop") or ""
    street = " ".join(p for p in (num, road) if p) or spot
    post = a.get("postcode") or ""
    town = " ".join(p for p in (post, city) if p)
    # A neighbourhood often carries its commune's own name, and dedup by element misses it
    # because the postcode is glued to the other copy.
    if area and city and area.strip().lower() == city.strip().lower():
        area = ""
    label = ", ".join(dict.fromkeys([p for p in (area, city, country) if p]))
    address = ", ".join(dict.fromkeys([p for p in (street, area, town, country) if p]))
    return {"lat": lat, "lon": lon, "address": address, "label": label or address,
            "city": city, "admin": admin, "country": country, "country_code": code}


def lookup_tz(lat, lon):
    """IANA timezone from coordinates. A phone's payload does not carry one, and without it
    every local date in the archive would be computed in the wrong offset."""
    url = ("https://timeapi.io/api/TimeZone/coordinate?"
           + urllib.parse.urlencode({"latitude": lat, "longitude": lon}))
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return (json.load(r) or {}).get("timeZone") or ""
    except Exception:
        return ""


def nearest_place(lat, lon, radius_m=None):
    """The cached place covering these coordinates, or None.

    A bounding box on the indexed columns first, the real distance in Python after: the table
    has no PostGIS and a few hundred candidates cost nothing to measure here.
    """
    radius_m = settings().place_radius_m if radius_m is None else radius_m
    d = radius_m / 111_320.0
    span = d / max(0.2, math.cos(lat * math.pi / 180))
    rows = db().fetch_all(
        "select * from location_places "
        "where lat between %s and %s and lon between %s and %s",
        (lat - d, lat + d, lon - span, lon + span))
    best, best_m = None, radius_m
    for row in rows:
        m = haversine_m(lat, lon, row["lat"], row["lon"])
        if m <= best_m:
            best, best_m = row, m
    return best


def resolve_place(lat, lon):
    """The place for these coordinates, from the cache or freshly geocoded and stored.

    Returns (place_row, called_out) so a caller running a budget knows whether it just spent
    one of its outbound calls.
    """
    hit = nearest_place(lat, lon)
    if hit:
        return hit, False
    geo = reverse_geocode(lat, lon)
    if not geo:
        return None, True
    geo["tz"] = lookup_tz(lat, lon)
    row = db().fetch_one(
        "insert into location_places (lat, lon, address, label, city, admin, country, "
        "country_code, tz) values (%s, %s, %s, %s, %s, %s, %s, %s, %s) returning *",
        (lat, lon, geo["address"], geo["label"], geo["city"], geo["admin"],
         geo["country"], geo["country_code"], geo["tz"]))
    return row, True


def enrich(budget=5):
    """Attach a place to pings that still have none. Returns (resolved, geocoded).

    Newest first, because the question asked most often is where you are now and a backlog of
    old points must never delay it. `budget` counts outbound geocodes rather than rows, so a
    run that only hits the cache drains the whole backlog in one pass.
    """
    rows = db().fetch_all(
        "select id, lat, lon from location_pings where place_id is null "
        "order by captured_at desc nulls last, id desc limit 400")
    resolved = geocoded = 0
    for row in rows:
        if geocoded >= budget:
            break
        place, called = resolve_place(row["lat"], row["lon"])
        if called:
            geocoded += 1
        if not place:
            continue
        execute(
            "update location_pings set place_id = %s, resolved_at = now(), "
            # A label the ping arrived with wins: an export carries "Home" or "Work", which
            # says more than the street address a geocoder hands back.
            "label = coalesce(nullif(label, ''), %s), "
            "tz = coalesce(nullif(tz, ''), %s) where id = %s",
            (place["id"], place["address"], place["tz"], row["id"]))
        resolved += 1
        if called:
            time.sleep(settings().geocode_sleep_s)
    return resolved, geocoded


# ----------------------------------------------------------------------------- reading

PING_SELECT = (
    "select p.id, p.captured_at, p.created_at, p.lat, p.lon, p.accuracy_m, p.source, "
    "coalesce(pl.address, p.label) as address, pl.label as place, pl.city, pl.admin, "
    "pl.country, pl.country_code, coalesce(p.tz, pl.tz) as tz, p.place_id "
    "from location_pings p left join location_places pl on pl.id = p.place_id ")


def latest_ping():
    rows = db().fetch_all(
        PING_SELECT + "order by p.captured_at desc nulls last, p.id desc limit 1")
    return rows[0] if rows else None


def current_tz():
    ping = latest_ping()
    tz = (ping or {}).get("tz") or settings().fallback_tz
    try:
        ZoneInfo(tz)
    except Exception:
        tz = settings().fallback_tz
    return tz


PERIODS = ("today", "yesterday", "this_week", "last_week", "this_month", "last_month",
           "last_7_days", "last_30_days", "last_90_days", "this_year", "all")


def resolve_window(period=None, since=None, until=None, tz=None):
    """Turn a period word or a pair of dates into a UTC window.

    Boundaries are local to where you are, not to UTC. "Yesterday" has to mean your yesterday,
    and from California that is an eight-hour difference on both edges of it.
    """
    zone = ZoneInfo(tz or current_tz())
    now = datetime.now(zone)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def out(a, b, label):
        return (a.astimezone(timezone.utc) if a else None,
                b.astimezone(timezone.utc) if b else None, label, str(zone))

    if since or until:
        return out(_parse_edge(since, zone, False), _parse_edge(until, zone, True),
                   f"{since or 'start'} to {until or 'now'}")
    period = (period or "all").strip().lower().replace("-", "_").replace(" ", "_")
    if period in ("", "all", "ever", "always"):
        return out(None, None, "all time")
    if period == "today":
        return out(midnight, now, "today")
    if period == "yesterday":
        return out(midnight - timedelta(days=1), midnight, "yesterday")
    if period == "this_week":
        return out(midnight - timedelta(days=now.weekday()), now, "this week")
    if period == "last_week":
        start = midnight - timedelta(days=now.weekday() + 7)
        return out(start, start + timedelta(days=7), "last week")
    if period == "this_month":
        return out(midnight.replace(day=1), now, "this month")
    if period == "last_month":
        first = midnight.replace(day=1)
        return out((first - timedelta(days=1)).replace(day=1), first, "last month")
    if period == "this_year":
        return out(midnight.replace(month=1, day=1), now, "this year")
    m = re.fullmatch(r"last_(\d+)_days?", period)
    if m:
        n = int(m.group(1))
        return out(midnight - timedelta(days=n - 1), now, f"last {n} days")
    raise ValueError("unknown period %r, expected one of %s or a last_N_days"
                     % (period, ", ".join(PERIODS)))


def _parse_edge(value, zone, is_end):
    if not value:
        return None
    text = str(value).strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            d = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=zone)
            return d + timedelta(days=1) if is_end else d
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            "cannot read date %r, use YYYY-MM-DD or an ISO timestamp" % value) from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)


# ------------------------------------------------------------------------------- stays

STAY_SELECT = (
    "select source, started_at, ended_at, seconds, semantic_type, fixes, lat, lon, off_m, "
    "place_id, label, address, city, admin, country, country_code, tz, day, "
    "trip_id, trip_slug, trip_name from location_v_stays ")


def stay_rows(start=None, end=None, country=None, city=None, place_id=None,
              min_minutes=0, limit=5000):
    """Raw stays overlapping a window, oldest first.

    Overlap, not containment: a week somewhere has to show up when the question is about the
    Tuesday in the middle of it.
    """
    where, args = [], []
    if start:
        where.append("ended_at > %s")
        args.append(start)
    if end:
        where.append("started_at < %s")
        args.append(end)
    if country:
        where.append("(lower(country) = lower(%s) or lower(country_code) = lower(%s))")
        args += [country, country]
    if city:
        where.append("lower(city) = lower(%s)")
        args.append(city)
    if place_id:
        where.append("place_id = %s")
        args.append(int(place_id))
    if min_minutes:
        where.append("seconds >= %s")
        args.append(float(min_minutes) * 60)
    sql = STAY_SELECT + ("where " + " and ".join(where) + " " if where else "")
    sql += "order by started_at asc limit %s"
    args.append(limit)
    return db().fetch_all(sql, args)


def _key(row, by):
    if by == "country":
        return row.get("country") or None
    if by == "place":
        return row.get("place_id")
    return row.get("city") or row.get("country") or None


def merge(rows, by="city", max_gap_h=72):
    """Collapse consecutive stays sharing a key into one run in time.

    Two visits to the same city an hour apart are one time there, and that is what "how many
    times did I go" has to count. The gap between them is travel and belongs to neither, so it
    is left out of the total rather than folded in.

    A long silence breaks the run whatever the key says. Otherwise a hole in the archive
    leaves the last stop before it sitting next to the first one after, and four separate
    trips to the same place read as one very long visit.
    """
    gap = timedelta(hours=max_gap_h)
    out = []
    for row in rows:
        key = _key(row, by)
        if key is None:
            continue
        if out and out[-1]["key"] == key and row["started_at"] - out[-1]["end"] <= gap:
            cur = out[-1]
            cur["seconds"] += float(row["seconds"] or 0)
            cur["end"] = max(cur["end"], row["ended_at"])
            cur["stops"] += 1
            cur["days"] |= days_touched(row)
            if row.get("city"):
                cur["cities"].add(row["city"])
            if row.get("label"):
                cur["places"].add(row["label"])
        else:
            out.append({"key": key, "start": row["started_at"], "end": row["ended_at"],
                        "seconds": float(row["seconds"] or 0), "stops": 1,
                        "country": row.get("country"), "city": row.get("city"),
                        "country_code": row.get("country_code"),
                        "address": row.get("address"), "label": row.get("label"),
                        "tz": row.get("tz"), "trip": row.get("trip_name"),
                        "days": days_touched(row),
                        "cities": {row["city"]} if row.get("city") else set(),
                        "places": {row["label"]} if row.get("label") else set()})
    return out


def days_touched(row):
    """Every local date a stay covers, not only the one it started on.

    A night counts for two days and a flight across a date line for two as well, so a reader
    that only ever recorded the start date under-counts by exactly the nights.
    """
    off = timedelta(minutes=int(row.get("off_m") or 0))
    first = (row["started_at"] + off).date()
    last = (row["ended_at"] + off).date()
    days, cur = set(), first
    while cur <= last and len(days) < 400:
        days.add(cur.isoformat())
        cur += timedelta(days=1)
    return days


def summarize(rows, by="country"):
    """Per city, country or place: times there, hours, distinct days, first and last seen."""
    totals = {}
    for stay in merge(rows, by=by):
        agg = totals.setdefault(stay["key"], {
            "name": stay["key"], "country": stay.get("country"),
            "country_code": stay.get("country_code"), "visits": 0, "seconds": 0.0,
            "stops": 0, "days": set(), "cities": set(), "places": set(),
            "first_seen": stay["start"], "last_seen": stay["end"]})
        agg["visits"] += 1
        agg["seconds"] += stay["seconds"]
        agg["stops"] += stay["stops"]
        agg["days"] |= stay["days"]
        agg["cities"] |= stay["cities"]
        agg["places"] |= stay["places"]
        agg["first_seen"] = min(agg["first_seen"], stay["start"])
        agg["last_seen"] = max(agg["last_seen"], stay["end"])
    return sorted(totals.values(), key=lambda a: a["seconds"], reverse=True)


def coverage():
    """What the archive actually holds, table by table.

    Read this before concluding someone was never somewhere. The sources cover different
    spans, and saying which is the point: imported visits stop at the last export, live pings
    start when the phone did, and altitude and speed exist only for the raw fix log.
    """
    return db().fetch_one(
        "select (select count(*) from location_visits) as visits, "
        "(select min(started_at) from location_visits) as first_visit, "
        "(select max(ended_at) from location_visits) as last_visit, "
        "(select count(*) from location_activities) as journeys, "
        "(select round((sum(distance_m) / 1000)::numeric) from location_activities) as km, "
        "(select count(*) from location_pings) as pings, "
        "(select count(place_id) from location_pings) as pings_resolved, "
        "(select min(coalesce(captured_at, created_at)) from location_pings) as first_ping, "
        "(select max(coalesce(captured_at, created_at)) from location_pings) as last_ping, "
        "(select count(*) from location_raw_positions) as raw_positions, "
        "(select count(*) from location_trips) as trips, "
        "(select count(*) from location_m_day_home) as days_with_data, "
        "(select count(*) from location_places) as places, "
        "(select count(distinct city) from location_places where city <> '') as cities, "
        "(select count(distinct country) from location_places where country <> '') "
        "as countries")


def gaps(min_days=14):
    """Stretches with no data at all, measured rather than remembered.

    An archive assembled from exports and a live feed has holes: the months between the day
    an export was taken and the day the phone started reporting, a period when the app was
    uninstalled, a phone that was replaced. An agent that does not know where the holes are
    will answer "you were never in Portugal" when the truth is "nothing was recorded that
    year", and those two sentences are not interchangeable.
    """
    rows = db().fetch_all(
        "with d as (select day from location_m_day_home order by day), "
        "n as (select day, lead(day) over (order by day) as next from d) "
        "select day as last_seen, next as resumed, (next - day) - 1 as missing_days "
        "from n where next is not null and (next - day) - 1 >= %s order by day",
        (int(min_days),))
    return [{"from": str(r["last_seen"]), "to": str(r["resumed"]),
             "missing_days": int(r["missing_days"])} for r in rows]


# --------------------------------------------------------------------------- rendering

def iso(value):
    """A value json.dumps will accept. Postgres hands back a Decimal for anything numeric,
    which json refuses outright."""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def hours(seconds):
    return round(seconds / 3600.0, 2)


def humanize(seconds):
    total = int(seconds)
    d, rem = divmod(total, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"
