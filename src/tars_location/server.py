"""MCP server (stdio) over a location archive.

Answers the questions the raw tables cannot: where you are right now, which trip you are on,
which cities last February, who you were with, how far you have flown, how long you stayed.

JSON-RPC 2.0 on stdin/stdout. Diagnostics go to stderr; stdout belongs to the protocol, and
one stray print into it corrupts the stream for the rest of the session.

Two rules run through every tool here. **Durations are measured, never modelled**: they come
from `location_v_stays`, which reads the start and end an export recorded rather than
interpolating between fixes. **Every answer says what it covers**, because an archive
assembled from an export plus a live feed has holes, and "you were never there" and "nothing
was recorded then" are not the same sentence.

The weather tools obey the same two rules. They read the coordinates out of the archive
instead of asking, and every answer says which position it used and how that was decided,
because a forecast for the wrong town reads exactly like a wrong forecast. A chance of rain
is counted over ensemble members rather than lifted off one model's output.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from datetime import timezone as _tz
from zoneinfo import ZoneInfo

from . import core, weather

DEFAULT_PROTOCOL = "2025-06-18"
SERVER_INFO = {"name": "location", "version": "0.1.0"}
MAX_ROWS = 20000

PERIOD_ARG = {
    "type": "string",
    "description": ("Window to look at: today, yesterday, this_week, last_week, this_month, "
                    "last_month, this_year, last_N_days, or all. Boundaries are local to "
                    "where the user is, not UTC. Ignored when since/until are given."),
}
SINCE_ARG = {"type": "string", "description": "Start, YYYY-MM-DD or an ISO timestamp."}
UNTIL_ARG = {"type": "string", "description": "End, exclusive. YYYY-MM-DD or ISO timestamp."}
COUNTRY_ARG = {"type": "string", "description": "Filter to one country, name or ISO code."}
CITY_ARG = {"type": "string", "description": "Filter to one city, by name."}

# Every weather tool takes the same three, and all three are optional: with none of them the
# question is about where the user is right now, which is what it almost always is.
WHERE_ARGS = {
    "place": {"type": "string",
              "description": ("Somewhere other than where they are now. Matched against the "
                              "archive first, so a place they have been resolves to the spot "
                              "they stood on; anywhere else is geocoded by name.")},
    "lat": {"type": "number", "description": "Latitude, if you already have coordinates."},
    "lon": {"type": "number", "description": "Longitude, with lat."},
}


TOOLS = [
    {
        "name": "current_location",
        "description": ("Where the user is right now: address, coordinates, their local time, "
                        "how old the fix is, which trip they are on and day N of it, and how "
                        "far from home. Read this before saying anything about where they "
                        "are."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "stays",
        "description": ("The chronological list of stays over a period: every stop with "
                        "start, end and duration, the trip it belongs to, and the place. Use "
                        "it to reconstruct a day, a week or a trip in order."),
        "inputSchema": {"type": "object", "properties": {
            "by": {"type": "string", "enum": ["place", "city", "country"],
                   "description": "Granularity. Default place, which is every stop."},
            "min_minutes": {"type": "number",
                            "description": "Drop stays shorter than this. Default 0."},
            "country": COUNTRY_ARG, "city": CITY_ARG,
            "period": PERIOD_ARG, "since": SINCE_ARG, "until": UNTIL_ARG}},
    },
    {
        "name": "cities_visited",
        "description": ("Cities over a period, with time in each, how many separate times "
                        "they were there, and which days. Use for 'which cities did I go to "
                        "last spring'."),
        "inputSchema": {"type": "object", "properties": {
            "country": COUNTRY_ARG,
            "period": PERIOD_ARG, "since": SINCE_ARG, "until": UNTIL_ARG}},
    },
    {
        "name": "countries_visited",
        "description": ("Countries over a period: separate trips there, time spent, distinct "
                        "days, and the cities inside each. Pass `country` for one of them "
                        "('how many times have I been to Japan')."),
        "inputSchema": {"type": "object", "properties": {
            "country": COUNTRY_ARG,
            "period": PERIOD_ARG, "since": SINCE_ARG, "until": UNTIL_ARG}},
    },
    {
        "name": "top_places",
        "description": ("The places they spend time in, most time first: address, city, what "
                        "the source calls the place, how many stops and how many hours. Use "
                        "for 'where do I actually spend my time' or to find an address."),
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "number", "description": "How many places. Default 20."},
            "search": {"type": "string",
                       "description": "Match the label, address or city, case-insensitive."},
            "country": COUNTRY_ARG, "city": CITY_ARG,
            "period": PERIOD_ARG, "since": SINCE_ARG, "until": UNTIL_ARG}},
    },
    {
        "name": "trips",
        "description": ("Trips, newest first: name, dates, nights, countries, cities, how far "
                        "from home, and who was along. A trip is a run of nights outside the "
                        "country they live in or over 100 km from it. Names edited by hand "
                        "are kept."),
        "inputSchema": {"type": "object", "properties": {
            "country": COUNTRY_ARG,
            "person": {"type": "string",
                       "description": "Only trips this person is tagged on, by name. Needs "
                                      "the optional people bridge."},
            "min_nights": {"type": "number", "description": "Drop shorter trips."},
            "limit": {"type": "number", "description": "How many trips. Default 40."},
            "period": PERIOD_ARG, "since": SINCE_ARG, "until": UNTIL_ARG}},
    },
    {
        "name": "trip",
        "description": ("One trip in full: the day-by-day country and city, every stay, the "
                        "journeys with mode and distance, the people tagged on it, and the "
                        "note. Identify it by slug, by name, or by a date inside it."),
        "inputSchema": {"type": "object", "properties": {
            "slug": {"type": "string", "description": "The trip slug, e.g. japan-2025-02-09."},
            "name": {"type": "string", "description": "Part of the trip name."},
            "date": {"type": "string", "description": "Any date inside the trip, YYYY-MM-DD."}}},
    },
    {
        "name": "who_was_there",
        "description": ("The link between trips and a people graph, both ways. Pass `person` "
                        "for the trips shared with them, `trip` for everyone tagged on it, "
                        "neither for every tagged pairing. `via` says how the link was made: "
                        "'trip' for a tag on the trip, 'range' for a window that covers it. "
                        "Needs the optional people bridge."),
        "inputSchema": {"type": "object", "properties": {
            "person": {"type": "string", "description": "A person's name, partial is fine."},
            "trip": {"type": "string", "description": "A trip slug."}}},
    },
    {
        "name": "with_me",
        "description": ("Days spent with someone, and where those days landed. Reads the "
                        "windows recorded by `record_together` against the archive: cities, "
                        "countries, how long, and the trip each window falls in. Pass no "
                        "`person` for every window. Needs migrations/0003_companions.sql."),
        "inputSchema": {"type": "object", "properties": {
            "person": {"type": "string", "description": "A person's name, partial is fine."},
            "since": SINCE_ARG, "until": UNTIL_ARG}},
    },
    {
        "name": "record_together",
        "description": ("Record that someone was with them over a date range, times optional. "
                        "Use it whenever a conversation says who they were with and when — a "
                        "weekend, an evening, a week at a friend's — including for spans the "
                        "archive never detected as a trip. Never write where: the archive "
                        "answers that. Needs migrations/0003_companions.sql."),
        "inputSchema": {"type": "object", "properties": {
            "person": {"type": "string",
                       "description": "A person's name. It must resolve to exactly one."},
            "since": {"type": "string", "description": "First day, YYYY-MM-DD."},
            "until": {"type": "string",
                      "description": "Last day, YYYY-MM-DD, inclusive. Default the first day."},
            "from_time": {"type": "string",
                          "description": "HH:MM local, only for a window inside a day."},
            "to_time": {"type": "string",
                        "description": "HH:MM local. Give both times, or neither."},
            "note": {"type": "string", "description": "What it was, in a few words."}},
            "required": ["person", "since"]},
    },
    {
        "name": "day",
        "description": ("One date end to end: where they woke up, every stay in order, the "
                        "journeys between them with mode and distance, the trip it belongs "
                        "to, and how far from home. Use for 'what did I do on the 8th'."),
        "inputSchema": {"type": "object", "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD. Default their today."}}},
    },
    {
        "name": "travel_stats",
        "description": ("How much they moved over a period: kilometres by mode, number of "
                        "flights and the longest, days away from home against days at home, "
                        "countries and cities touched."),
        "inputSchema": {"type": "object", "properties": {
            "period": PERIOD_ARG, "since": SINCE_ARG, "until": UNTIL_ARG}},
    },
    {
        "name": "records",
        "description": ("The extremes: highest point, fastest, farthest from home, the four "
                        "compass records, longest trip, most cities in a day, longest flight. "
                        "Each carries the window it was measured over, which is not the same "
                        "for all of them."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "home",
        "description": ("Where they have lived and when. Home is a timeline, not a point, so "
                        "anything home-relative has to read this rather than assume one "
                        "address."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "location_coverage",
        "description": ("What the archive actually holds, table by table, with the span each "
                        "source covers and every gap of two weeks or more. Check this before "
                        "concluding someone was never somewhere: 'no record' and 'was not "
                        "there' are different answers."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "location_sql",
        "description": ("Read-only SELECT for questions the other tools do not shape. Tables: "
                        "location_visits(started_at, ended_at, start_offset_m, place_id, "
                        "semantic_type), location_activities(started_at, ended_at, mode, "
                        "distance_m, start_lat, start_lon, end_lat, end_lon), location_places"
                        "(id, lat, lon, address, label, city, admin, country, country_code, "
                        "tz), location_pings, location_raw_positions(altitude_m, speed_ms), "
                        "location_trips. Views: location_v_stays (the history spine), "
                        "location_m_day_home (one row per day with the anchor place and km "
                        "from home), location_v_records, location_v_home_periods. With the "
                        "people bridge installed: location_trip_people, "
                        "location_v_trip_people, people_v_trips, location_companions, "
                        "location_v_companions, location_v_companion_days, "
                        "people_v_together_places."),
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "A single SELECT statement."},
            "limit": {"type": "number", "description": "Row cap. Default 200."}},
            "required": ["query"]},
    },
    {
        "name": "will_it_rain",
        "description": ("The chance of rain where they are, counted over about 120 ensemble "
                        "members from ECMWF, DWD and NOAA rather than read off one forecast. "
                        "Returns the percentage hour by hour, how much rain to expect, the "
                        "longest dry stretch, and whether the three centres agree. Default "
                        "window is the rest of today. Use this for anything phrased as a "
                        "chance, a risk or 'should I take an umbrella'."),
        "inputSchema": {"type": "object", "properties": {
            **WHERE_ARGS,
            "date": {"type": "string",
                     "description": "A whole local day, YYYY-MM-DD, up to about 4 days out. "
                                    "Default is the rest of today."},
            "hours": {"type": "number",
                      "description": "The next N hours instead of a calendar day."},
            "threshold": {"type": "number",
                          "description": "What counts as rain, as a total over the window, in "
                                         "the same unit as the answer. Default 0.2 mm."}}},
    },
    {
        "name": "weather_now",
        "description": ("What it is doing outside right now where they are: sky, "
                        "temperature, what it feels like, wind and gusts, humidity, "
                        "pressure, sunrise and sunset, plus the next twelve hours. Read this "
                        "before saying anything about their weather."),
        "inputSchema": {"type": "object", "properties": dict(WHERE_ARGS)},
    },
    {
        "name": "weather_forecast",
        "description": ("The days ahead where they are: high and low, rain and how long it "
                        "lasts, chance of rain, wind, UV, sunrise and sunset, one row per "
                        "day. Use for 'what is the weekend looking like'."),
        "inputSchema": {"type": "object", "properties": {
            **WHERE_ARGS,
            "days": {"type": "number", "description": "How many days, 1 to 16. Default 7."}}},
    },
    {
        "name": "weather_models",
        "description": ("The same forecast from seven independent national weather services "
                        "side by side — ECMWF, NOAA, DWD, Météo-France, the Met Office, "
                        "Environment Canada and JMA — with their spread and which of them "
                        "actually cover this point. Use when the question is how reliable "
                        "the forecast is, or when two sources disagree."),
        "inputSchema": {"type": "object", "properties": {
            **WHERE_ARGS,
            "variable": {"type": "string",
                         "enum": ["precipitation", "temperature_2m", "wind_speed_10m",
                                  "cloud_cover", "relative_humidity_2m"],
                         "description": "What to compare. Default precipitation."},
            "hours": {"type": "number", "description": "Window from now. Default 24."}}},
    },
    {
        "name": "weather_history",
        "description": ("What the weather actually was on a past day, from the ERA5 "
                        "reanalysis, at the place they spent that day — the archive supplies "
                        "the coordinates, so 'was it raining in Lisbon that Tuesday' needs "
                        "only the date. Lags real time by about five days."),
        "inputSchema": {"type": "object", "properties": {
            **WHERE_ARGS,
            "date": {"type": "string", "description": "YYYY-MM-DD."}},
            "required": ["date"]},
    },
]


# ----------------------------------------------------------------------------- helpers

def _window(args):
    start, end, label, tz = core.resolve_window(
        args.get("period"), args.get("since"), args.get("until"))
    return start, end, {"period": label, "timezone": tz}


def _rows(sql, params=()):
    return [{k: core.iso(v) for k, v in row.items()} for row in core.db().fetch_all(sql, params)]


def _has_people_bridge() -> bool:
    """The bridge is optional, so every tool that touches it asks first and says so plainly
    when it is missing. An agent told "relation does not exist" invents an explanation."""
    row = core.db().fetch_one("select to_regclass('public.location_v_trip_people') as v")
    return bool(row and row["v"])


NO_BRIDGE = {
    "available": False,
    "note": ("no people bridge in this database. Run migrations/0002_people_bridge.sql "
             "against a database that also holds a people graph to link trips to people."),
}


def _has_companions() -> bool:
    row = core.db().fetch_one("select to_regclass('public.location_companions') as v")
    return bool(row and row["v"])


NO_COMPANIONS = {
    "available": False,
    "note": ("no companion windows in this database. Run migrations/0003_companions.sql, "
             "the second half of the people bridge, to record who was with them over a "
             "date range."),
}


def _stay_out(row):
    return {
        "place": row.get("label") or row.get("address"),
        "address": row.get("address"),
        "city": row.get("city"), "country": row.get("country"),
        "kind": row.get("semantic_type"),
        "start": core.iso(row["started_at"]), "end": core.iso(row["ended_at"]),
        "duration": core.humanize(row["seconds"] or 0),
        "hours": core.hours(row["seconds"] or 0),
        "day": core.iso(row.get("day")),
        "trip": row.get("trip_name"),
        "source": row.get("source"),
    }


def _merged_out(stay):
    return {
        "name": stay["key"], "city": stay.get("city"), "country": stay.get("country"),
        "start": core.iso(stay["start"]), "end": core.iso(stay["end"]),
        "duration": core.humanize(stay["seconds"]), "hours": core.hours(stay["seconds"]),
        "stops": stay["stops"], "days": len(stay["days"]), "trip": stay.get("trip"),
    }


def _summary_out(agg):
    return {
        "name": agg["name"], "country": agg.get("country"),
        "country_code": agg.get("country_code"),
        "visits": agg["visits"], "stops": agg["stops"],
        "hours": core.hours(agg["seconds"]), "duration": core.humanize(agg["seconds"]),
        "days": len(agg["days"]), "dates": sorted(agg["days"])[:90],
        "cities": sorted(agg["cities"]), "places": sorted(agg["places"])[:20],
        "first_seen": core.iso(agg["first_seen"]), "last_seen": core.iso(agg["last_seen"]),
    }


def _trip_out(row, people=None):
    out = {
        "slug": row["slug"], "name": row["name"],
        "start": core.iso(row["started_at"]), "end": core.iso(row["ended_at"]),
        "nights": row["nights"], "countries": row.get("countries") or [],
        "cities": row.get("cities") or [],
        "max_km_from_home": round(row["max_km_from_home"]) if row.get("max_km_from_home") else None,
        "renamed_by_hand": not row.get("name_is_auto", True),
    }
    if row.get("note"):
        out["note"] = row["note"]
    if people is not None:
        out["people"] = people
    return out


def _find_trip(args):
    if args.get("slug"):
        rows = core.db().fetch_all("select * from location_trips where slug = %s",
                                   (args["slug"],))
    elif args.get("name"):
        rows = core.db().fetch_all(
            "select * from location_trips where name ilike %s order by started_at desc",
            ("%%%s%%" % args["name"].replace("%", ""),))
    elif args.get("date"):
        rows = core.db().fetch_all(
            "select * from location_trips where started_at <= %s::timestamptz + interval '1 day' "
            "and ended_at > %s::timestamptz order by started_at desc",
            (args["date"], args["date"]))
    else:
        raise ValueError("pass a slug, a name or a date inside the trip")
    return rows[0] if rows else None


def _people_of(trip_id):
    if not _has_people_bridge():
        return None
    return _rows("select full_name, role, current_org, note from location_v_trip_people "
                 "where trip_id = %s order by full_name", (trip_id,))


def _journeys(start, end):
    return _rows(
        "select started_at, ended_at, mode, distance_m, start_lat, start_lon, end_lat, end_lon "
        "from location_activities where ended_at > %s and started_at < %s "
        "order by started_at", (start, end))


def _journey_out(row):
    return {"mode": (row.get("mode") or "unknown").lower().replace("_", " "),
            "km": round((row.get("distance_m") or 0) / 1000.0, 1),
            "start": row["started_at"], "end": row["ended_at"]}


# ------------------------------------------------------------------------------- tools

def run_tool(name, arguments):
    args = arguments or {}

    if name == "current_location":
        return _current_location()

    if name == "location_coverage":
        out = {k: core.iso(v) for k, v in (core.coverage() or {}).items()}
        # Measured from the data rather than written down. A gap list maintained by hand is
        # wrong the first time a source is re-imported, and wrong quietly.
        out["gaps"] = core.gaps()
        out["note"] = ("visits come from an imported export and stop at the last import; "
                       "pings are live. altitude and speed exist only for the raw fix log. "
                       "a date inside a gap means nothing was recorded, not that the user "
                       "was elsewhere.")
        return out

    if name == "stays":
        start, end, meta = _window(args)
        rows = core.stay_rows(start, end, country=args.get("country"), city=args.get("city"),
                              min_minutes=args.get("min_minutes") or 0, limit=MAX_ROWS)
        meta["stays_examined"] = len(rows)
        if not rows:
            return {**meta, "found": False,
                    "note": "nothing recorded in this window; check location_coverage"}
        by = args.get("by") or "place"
        if by == "place":
            return {**meta, "by": by, "stays": [_stay_out(r) for r in rows]}
        return {**meta, "by": by, "stays": [_merged_out(s) for s in core.merge(rows, by=by)]}

    if name in ("cities_visited", "countries_visited"):
        start, end, meta = _window(args)
        # The whole stream, then filter the result. Filtering first glues separate trips to
        # the same country into one run, because nothing is left in between to break them.
        rows = core.stay_rows(start, end, limit=MAX_ROWS)
        meta["stays_examined"] = len(rows)
        if not rows:
            return {**meta, "found": False,
                    "note": "nothing recorded in this window; check location_coverage"}
        by = "city" if name == "cities_visited" else "country"
        found = core.summarize(rows, by=by)
        wanted = (args.get("country") or "").strip().lower()
        if wanted:
            found = [a for a in found
                     if wanted in ((a["name"] or "").lower(), (a["country"] or "").lower(),
                                   (a["country_code"] or "").lower())]
            if not found:
                return {**meta, "found": False,
                        "note": "nothing in %r over this window" % args["country"]}
        return {**meta, ("cities" if by == "city" else "countries"):
                [_summary_out(a) for a in found]}

    if name == "top_places":
        return _top_places(args)

    if name == "trips":
        return _trips(args)

    if name == "trip":
        trip = _find_trip(args)
        if not trip:
            return {"found": False, "note": "no trip matches"}
        return _trip_detail(trip)

    if name == "who_was_there":
        return _who_was_there(args)

    if name == "with_me":
        return _with_me(args)

    if name == "record_together":
        return _record_together(args)

    if name == "day":
        return _day(args)

    if name == "travel_stats":
        return _travel_stats(args)

    if name == "records":
        return {"records": [{
            "kind": r["kind"], "label": r["label"],
            "value": round(r["value"], 1) if r.get("value") is not None else None,
            "unit": r["unit"], "at": core.iso(r["at"]),
            "lat": r.get("lat"), "lon": r.get("lon"),
            "measured_over": "%s to %s" % (core.iso(r["covers_from"])[:10],
                                           core.iso(r["covers_to"])[:10]),
        } for r in core.db().fetch_all("select * from location_v_records")]}

    if name == "home":
        homes = _rows(
            "select label, city, country, first_seen, last_seen, "
            "round((seconds / 86400)::numeric, 1) as days_of_presence "
            "from location_v_home_periods order by first_seen")
        return {"homes": homes, "count": len(homes),
                "note": ("home is a timeline, not a point. anything measured from home has "
                         "to pick the one that was current on the day in question.")}

    if name == "location_sql":
        limit = int(args.get("limit") or 200)
        rows = core.db().fetch_read_only(args.get("query") or "", limit)
        return {"rows": [{k: core.iso(v) for k, v in row.items()} for row in rows]}

    if name in ("will_it_rain", "weather_now", "weather_forecast", "weather_models",
                "weather_history"):
        return _weather(name, args)

    raise RuntimeError("unknown tool: %s" % name)


def _place_in_archive(search):
    """The place in the archive that best matches a name, or None.

    Most time spent wins, not most recent and not shortest string: "Lisbon" should land on
    the flat they slept in for three weeks rather than on the airport they passed through
    once, and both carry the city in their address.
    """
    pattern = "%%%s%%" % str(search).replace("%", "")
    row = core.db().fetch_one(
        "select label, address, city, country, avg(lat) as lat, avg(lon) as lon, "
        "sum(seconds) as seconds from location_v_stays "
        "where lat is not null and (label ilike %s or address ilike %s or city ilike %s) "
        "group by label, address, city, country order by sum(seconds) desc limit 1",
        (pattern, pattern, pattern))
    return row if row and row.get("lat") is not None else None


def _where(args, date=None):
    """Which coordinates a weather question is about, and how that was decided.

    Four sources, in order of how much they know: coordinates the caller passed, a place name
    resolved against the archive and then against a geocoder, the place they spent a given
    past day at, and finally the last fix. The answer always carries which one it used —
    a forecast for the wrong town is indistinguishable from a wrong forecast.
    """
    if args.get("lat") is not None and args.get("lon") is not None:
        return {"lat": float(args["lat"]), "lon": float(args["lon"]), "source": "given"}

    if args.get("place"):
        hit = _place_in_archive(args["place"])
        if hit:
            return {"lat": float(hit["lat"]), "lon": float(hit["lon"]),
                    "place": hit.get("label") or hit.get("address"), "city": hit.get("city"),
                    "country": hit.get("country"), "source": "matched in the archive"}
        geo = weather.geocode(args["place"], core.settings())
        if not geo:
            raise ValueError("cannot find %r, in the archive or by name. Pass lat and lon."
                             % args["place"])
        return {"lat": geo["lat"], "lon": geo["lon"], "place": geo["place"],
                "source": "geocoded by name"}

    if date:
        # Where the day was actually spent, which is the whole point of asking a location
        # archive about past weather: the longest stay that day, not the first fix of it.
        lo = datetime.fromisoformat(str(date)[:10]).replace(tzinfo=_tz.utc) - timedelta(hours=14)
        rows = [r for r in core.stay_rows(lo, lo + timedelta(hours=38), limit=MAX_ROWS)
                if str(date)[:10] in core.days_touched(r) and r.get("lat") is not None]
        if rows:
            stay = max(rows, key=lambda r: float(r.get("seconds") or 0))
            return {"lat": float(stay["lat"]), "lon": float(stay["lon"]),
                    "place": stay.get("label") or stay.get("address"), "city": stay.get("city"),
                    "country": stay.get("country"),
                    "source": "where they spent %s" % str(date)[:10]}

    ping = core.latest_ping()
    if not ping:
        raise ValueError("no position in the archive, so there is nowhere to forecast. "
                         "Pass a place, or lat and lon.")
    stamp = ping.get("captured_at") or ping.get("created_at")
    age = (datetime.now(_tz.utc) - stamp).total_seconds()
    out = {"lat": round(ping["lat"], 5), "lon": round(ping["lon"], 5),
           "place": ping.get("address"), "city": ping.get("city"),
           "country": ping.get("country"), "source": "their last fix, %s old" % core.humanize(age)}
    if age > 86400:
        # A day-old fix can be a country away, and a forecast for where they were yesterday
        # reads exactly like a forecast for where they are.
        out["stale"] = True
    return out


def _weather(name, args):
    settings = core.settings()
    spot = _where(args, date=args.get("date") if name == "weather_history" else None)
    coords = (spot["lat"], spot["lon"])
    try:
        if name == "will_it_rain":
            body = weather.rain_outlook(*coords, date=args.get("date"),
                                        hours=args.get("hours"),
                                        threshold=args.get("threshold"), settings=settings)
        elif name == "weather_now":
            body = weather.conditions(*coords, settings=settings)
        elif name == "weather_forecast":
            body = weather.daily(*coords, days=int(args.get("days") or 7), settings=settings)
        elif name == "weather_models":
            body = weather.model_panel(*coords, variable=args.get("variable") or "precipitation",
                                       hours=int(args.get("hours") or 24), settings=settings)
        else:
            body = weather.history(*coords, date=args["date"], settings=settings)
    except weather.WeatherError as err:
        return {"where": spot, "found": False, "error": str(err)}
    if name == "weather_history" and spot["source"].startswith("their last fix"):
        # The whole point of asking the archive about past weather is that it knows where the
        # day was spent. When it does not, saying so beats reporting the weather of wherever
        # they happen to be standing today as if it were that day's.
        spot["note"] = ("nothing recorded on that date, so this is the weather at their "
                        "current position, not at wherever they actually were. Check "
                        "location_coverage.")
    return {"where": spot, **body}


def _current_location():
    ping = core.latest_ping()
    if not ping:
        return {"known": False, "reason": "no ping in the archive"}
    stamp = ping.get("captured_at") or ping.get("created_at")
    age = (datetime.now(_tz.utc) - stamp).total_seconds()
    local = ""
    if ping.get("tz"):
        try:
            local = datetime.now(ZoneInfo(ping["tz"])).strftime("%Y-%m-%d %H:%M")
        except Exception:
            local = ""
    out = {"known": True, "address": ping.get("address"), "city": ping.get("city"),
           "country": ping.get("country"), "lat": round(ping["lat"], 5),
           "lon": round(ping["lon"], 5), "accuracy_m": ping.get("accuracy_m"),
           "timezone": ping.get("tz"), "local_time": local,
           "captured_at": core.iso(stamp), "age": core.humanize(age),
           # A day-old fix is a guess about the present, and an agent that reads the address
           # without reading this will state it as fact.
           "stale": age > 86400}

    # A fix is a point. What is actually wanted is "day 4 of Portugal, 280 km out".
    live = core.db().fetch_all(
        "select * from location_trips where started_at <= now() and ended_at > now() "
        "order by started_at desc limit 1")
    if live:
        t = live[0]
        day_n = (datetime.now(_tz.utc) - t["started_at"]).days + 1
        out["trip"] = {"name": t["name"], "slug": t["slug"],
                       "day": "day %d of %d" % (day_n, t["nights"] + 1),
                       "nights": t["nights"], "started": core.iso(t["started_at"])[:10]}
    today = core.db().fetch_all(
        "select day, km_from_home, home_city, anchor_city, anchor_country "
        "from location_m_day_home order by day desc limit 1")
    if today:
        d = today[0]
        out["from_home"] = {"as_of": core.iso(d["day"]),
                            "km": round(d["km_from_home"]) if d["km_from_home"] else 0,
                            "home_city": d["home_city"]}
    return out


def _top_places(args):
    start, end, meta = _window(args)
    where, params = ["1=1"], []
    if start:
        where.append("ended_at > %s")
        params.append(start)
    if end:
        where.append("started_at < %s")
        params.append(end)
    if args.get("country"):
        where.append("(lower(country) = lower(%s) or lower(country_code) = lower(%s))")
        params += [args["country"], args["country"]]
    if args.get("city"):
        where.append("lower(city) = lower(%s)")
        params.append(args["city"])
    if args.get("search"):
        where.append("(label ilike %s or address ilike %s or city ilike %s)")
        pat = "%%%s%%" % args["search"].replace("%", "")
        params += [pat, pat, pat]
    params.append(int(args.get("limit") or 20))
    rows = core.db().fetch_all(
        "select place_id, label, address, city, country, count(*) as stops, "
        "sum(seconds) as seconds, min(started_at) as first_seen, max(ended_at) as last_seen "
        "from location_v_stays where " + " and ".join(where) +
        " group by place_id, label, address, city, country "
        "order by sum(seconds) desc limit %s", params)
    return {**meta, "places": [{
        "place": r["label"] or r["address"], "address": r["address"], "city": r["city"],
        "country": r["country"], "stops": r["stops"],
        "hours": core.hours(float(r["seconds"] or 0)),
        "duration": core.humanize(float(r["seconds"] or 0)),
        "first_seen": core.iso(r["first_seen"]), "last_seen": core.iso(r["last_seen"]),
    } for r in rows]}


def _trips(args):
    start, end, meta = _window(args)
    bridge = _has_people_bridge()
    where, params = ["1=1"], []
    if start:
        where.append("t.ended_at > %s")
        params.append(start)
    if end:
        where.append("t.started_at < %s")
        params.append(end)
    if args.get("country"):
        where.append("(lower(t.primary_country) = lower(%s) "
                     "or lower(%s) = any(select lower(x) from unnest(t.country_codes) x))")
        params += [args["country"], args["country"]]
    if args.get("min_nights"):
        where.append("t.nights >= %s")
        params.append(int(args["min_nights"]))
    if args.get("person"):
        if not bridge:
            return {**meta, **NO_BRIDGE, "trips": []}
        where.append("exists (select 1 from location_v_trip_people p "
                     "where p.trip_id = t.id and p.full_name ilike %s)")
        params.append("%%%s%%" % args["person"].replace("%", ""))
    params.append(int(args.get("limit") or 40))
    rows = core.db().fetch_all(
        "select t.* from location_trips t where " + " and ".join(where) +
        " order by t.started_at desc limit %s", params)
    tagged = {}
    if bridge:
        for r in core.db().fetch_all(
                "select trip_id, full_name, role from location_v_trip_people"):
            tagged.setdefault(r["trip_id"], []).append(
                r["full_name"] if r["role"] == "with"
                else "%s (%s)" % (r["full_name"], r["role"]))
    return {**meta, "trips": [_trip_out(r, tagged.get(r["id"], []) if bridge else None)
                              for r in rows]}


def _trip_detail(trip):
    stays = core.stay_rows(trip["started_at"], trip["ended_at"], limit=MAX_ROWS)
    legs = _journeys(trip["started_at"], trip["ended_at"])
    km = sum((leg.get("distance_m") or 0) for leg in legs) / 1000.0
    days = _rows(
        "select day, anchor_city, anchor_country, round(km_from_home::numeric) as km_from_home "
        "from location_m_day_home where day >= %s::date and day < %s::date order by day",
        (core.iso(trip["started_at"])[:10], core.iso(trip["ended_at"])[:10]))
    return {"trip": _trip_out(trip, _people_of(trip["id"])),
            "km_travelled": round(km),
            "days": days,
            "stays": [_stay_out(s) for s in stays],
            # Below three kilometres a "journey" is a walk across a car park, and a trip
            # listing forty of them buries the flight that matters.
            "journeys": [_journey_out(leg) for leg in legs if (leg.get("distance_m") or 0) > 3000]}


def _like(name):
    return "%%%s%%" % name.replace("%", "")


def _resolve_person(name):
    """One person or nothing. Two matches is a question for the user, never a coin toss."""
    rows = core.db().fetch_all(
        "select id, full_name, current_org from people where full_name ilike %s "
        "order by full_name limit 6", (_like(name),))
    if len(rows) == 1:
        return rows[0], None
    if not rows:
        return None, {"found": False, "note": "nobody in the graph matches that name"}
    return None, {"found": False, "candidates": [dict(r) for r in rows],
                  "note": "several people match; say which one"}


def _with_me(args):
    if not _has_companions():
        return dict(NO_COMPANIONS)
    where, params = [], []
    if args.get("person"):
        where.append("full_name ilike %s")
        params.append(_like(args["person"]))
    if args.get("since"):
        where.append("ended_at > %s")
        params.append(args["since"])
    if args.get("until"):
        where.append("started_at < %s")
        params.append(args["until"])
    sql = ("select id, full_name, started_on, ended_on, days, has_time, tz, note, "
           "countries, cities, places, days_with_evidence, trips "
           "from location_v_companions")
    if where:
        sql += " where " + " and ".join(where)
    windows = _rows(sql + " order by started_at desc", tuple(params))

    place_sql = ("select full_name, country, city, days, seconds, first_day, last_day "
                 "from people_v_together_places")
    place_params = ()
    if args.get("person"):
        place_sql += " where full_name ilike %s"
        place_params = (_like(args["person"]),)
    places = [dict(r, duration=core.humanize(r["seconds"] or 0))
              for r in _rows(place_sql + " order by seconds desc limit 60", place_params)]
    return {"windows": windows, "places": places,
            "note": ("where is read from the archive, never typed: a window over a gap in it "
                     "has days but no city")}


def _record_together(args):
    if not _has_companions():
        return dict(NO_COMPANIONS)
    person, problem = _resolve_person(args["person"])
    if problem:
        return problem
    d0 = args["since"]
    d1 = args.get("until") or d0
    t0, t1 = args.get("from_time"), args.get("to_time")
    if bool(t0) != bool(t1):
        return {"written": False, "note": "give both times, or neither"}
    if d1 < d0:
        return {"written": False, "note": "the end is before the start"}
    # Wall clock with no tz, which is the contract the table's trigger reads: it re-anchors both
    # ends in the timezone they were standing in that day, so nothing here has to know where
    # that was. A whole-day window is half-open, ending at midnight after the last day.
    if t0:
        start, end = "%sT%s:00Z" % (d0, t0), "%sT%s:00Z" % (d1, t1)
    else:
        start = "%sT00:00:00Z" % d0
        end = "%sT00:00:00Z" % (datetime.fromisoformat(d1).date() + timedelta(days=1))
    try:
        core.execute(
            "insert into location_companions (person_id, started_at, ended_at, has_time, "
            "note, source) values (%s, %s, %s, %s, %s, 'agent')",
            (person["id"], start, end, bool(t0), args.get("note") or ""))
    except Exception as exc:                                  # noqa: BLE001
        if "location_companions_no_overlap" in str(exc):
            return {"written": False,
                    "note": "%s already has a window over those hours" % person["full_name"]}
        raise
    back = _rows("select started_on, ended_on, days, cities, countries, trips, tz "
                 "from location_v_companions where person_id = %s "
                 "order by created_at desc limit 1", (person["id"],))
    return {"written": True, "person": person["full_name"],
            "window": back[0] if back else None}


def _who_was_there(args):
    if not _has_people_bridge():
        return dict(NO_BRIDGE)
    # `via` only exists once 0003 has run, and a bridge stuck on 0002 is a supported install.
    via = ", via" if _has_companions() else ""
    if args.get("person"):
        rows = _rows(
            "select full_name, trip_name, slug, started_at, ended_at, nights, primary_country, "
            "role, note" + via + " from people_v_trips where full_name ilike %s "
            "order by started_at desc", (_like(args["person"]),))
        return {"person": args["person"], "trips": rows,
                "note": "linked trips only; a trip nobody is linked to says nothing either way"}
    if args.get("trip"):
        rows = _rows("select full_name, role, note, current_org, job_title" + via +
                     " from location_v_trip_people where slug = %s order by full_name",
                     (args["trip"],))
        return {"trip": args["trip"], "people": rows}
    return {"pairings": _rows(
        "select trip_name, slug, started_at, full_name, role" + via +
        " from location_v_trip_people order by started_at desc")}


def _day(args):
    date = args.get("date")
    if not date:
        date = datetime.now(ZoneInfo(core.current_tz())).date().isoformat()
    # A local day is not a UTC day, and the offset swings by up to a full day either side of
    # the date line. Take a wide window and let each stay's own local date decide.
    lo = datetime.fromisoformat(date).replace(tzinfo=_tz.utc) - timedelta(hours=14)
    hi = lo + timedelta(hours=38)
    rows = [r for r in core.stay_rows(lo, hi, limit=MAX_ROWS)
            if date in core.days_touched(r)]
    legs = [leg for leg in _journeys(lo, hi)
            if core.iso(leg["started_at"])[:10] == date]
    grid = core.db().fetch_all(
        "select anchor_city, anchor_country, cities, places, "
        "round(km_from_home::numeric) as km_from_home, home_city "
        "from location_m_day_home where day = %s", (date,))
    trip = core.db().fetch_all(
        "select name, slug from location_trips where started_at <= %s::timestamptz "
        "and ended_at > %s::timestamptz limit 1", (date, date))
    out = {"date": date,
           "stays": [_stay_out(r) for r in rows],
           "journeys": [_journey_out(leg) for leg in legs
                        if (leg.get("distance_m") or 0) > 500],
           "km": round(sum((leg.get("distance_m") or 0) for leg in legs) / 1000.0, 1)}
    if grid:
        g = grid[0]
        out["where"] = {"city": g["anchor_city"], "country": g["anchor_country"],
                        "cities": g["cities"], "km_from_home": g["km_from_home"],
                        "home_city": g["home_city"]}
    if trip:
        out["trip"] = trip[0]["name"]
    if not rows and not legs and not grid:
        out["found"] = False
        out["note"] = "nothing recorded that day; check location_coverage"
    return out


def _travel_stats(args):
    start, end, meta = _window(args)
    lo = start or datetime(2000, 1, 1, tzinfo=_tz.utc)
    hi = end or datetime.now(_tz.utc)
    modes = _rows(
        "select mode, count(*) as journeys, round((sum(distance_m) / 1000)::numeric) as km "
        "from location_activities where ended_at > %s and started_at < %s "
        "group by mode order by sum(distance_m) desc nulls last", (lo, hi))
    flights = core.db().fetch_all(
        "select count(*) as n, round((max(distance_m) / 1000)::numeric) as longest_km, "
        "round((sum(distance_m) / 1000)::numeric) as km from location_activities "
        "where mode = 'FLYING' and ended_at > %s and started_at < %s", (lo, hi))
    grid = core.db().fetch_all(
        "select count(*) as days, "
        "count(*) filter (where anchor_country is distinct from home_country "
        "                 or coalesce(km_from_home, 0) > 100) as days_away, "
        "count(distinct anchor_country) as countries, "
        "round(max(km_from_home)::numeric) as farthest_km "
        "from location_m_day_home where day >= %s::date and day < %s::date",
        (core.iso(lo)[:10], core.iso(hi)[:10]))
    out = {**meta,
           "km_by_mode": [{"mode": (m["mode"] or "unknown").lower().replace("_", " "),
                           "journeys": m["journeys"], "km": float(m["km"] or 0)}
                          for m in modes],
           "km_total": round(sum(float(m["km"] or 0) for m in modes))}
    if flights and flights[0]["n"]:
        out["flights"] = {"count": flights[0]["n"], "km": float(flights[0]["km"] or 0),
                          "longest_km": float(flights[0]["longest_km"] or 0)}
    if grid and grid[0]["days"]:
        g = grid[0]
        out["days"] = {"with_data": g["days"], "away": g["days_away"],
                       "at_home": g["days"] - g["days_away"],
                       "countries": g["countries"],
                       "farthest_from_home_km": float(g["farthest_km"] or 0)}
    return out


# ---------------------------------------------------------------------------- protocol

def handle(message):
    method = message.get("method")
    request_id = message.get("id")

    if method == "initialize":
        requested = (message.get("params") or {}).get("protocolVersion")
        return reply(request_id, {"protocolVersion": requested or DEFAULT_PROTOCOL,
                                  "capabilities": {"tools": {}},
                                  "serverInfo": SERVER_INFO})

    if request_id is None:
        return None

    if method == "ping":
        return reply(request_id, {})

    if method == "tools/list":
        return reply(request_id, {"tools": TOOLS})

    if method == "tools/call":
        params = message.get("params") or {}
        try:
            payload = run_tool(params.get("name"), params.get("arguments"))
            text = json.dumps(payload, ensure_ascii=False, default=str, indent=1)
            is_error = False
        except Exception as err:
            text = "error: %s" % err
            is_error = True
        return reply(request_id, {"content": [{"type": "text", "text": text}],
                                  "isError": is_error})

    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": -32601, "message": "unknown method: %s" % method}}


def reply(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        try:
            response = handle(message)
        except Exception as err:  # never let one bad request kill the server
            print("tars-location-mcp: %s" % err, file=sys.stderr)
            response = None if message.get("id") is None else {
                "jsonrpc": "2.0", "id": message.get("id"),
                "error": {"code": -32603, "message": str(err)}}
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
