"""Import a Google Timeline export into the archive, whole.

A live feed only knows where you have been since you installed it. Google has kept the same
log for years, and its export is the only way to backfill the part that matters: which
countries, which cities, how long, and how you got there.

**The export is not a list of coordinates.** For every segment it carries the transport mode
Google inferred from the phone's sensors, its own measurement of the distance travelled, the
route geometry, the raw fixes with altitude and speed, what the sensors thought you were
doing, the trips it detected by itself, and a profile of your habits including which place is
home. Flattening that into one coordinate per row throws away everything a reverse-geocoder
cannot reconstruct. Each shape therefore lands in its own table, and `location_pings` keeps
receiving two rows per visit so the time model, the MCP and any map read the whole history
without knowing where it came from.

**Four export shapes, all handled**, because which one you get depends on the phone and on
which door you exported through:

    {"semanticSegments": [...]}   the on-device export (Android and iOS, 2024+)
    [ ... ]                       the same segments as a bare list
    {"timelineObjects": [...]}    classic Takeout, Semantic Location History, one file a month
    {"locations": [...]}          classic Takeout, Records.json, the raw fix log

**Re-running is the normal case.** A phone re-exports its whole history every time, so every
export overlaps the last. Each table has a natural unique key and every insert is `on conflict
do nothing`, so a second pass adds only what is new. `location_imports` keeps one row per file
so the import history is itself readable.

Geocoding is deliberately not part of the insert: Nominatim allows one call a second and an
export brings a thousand new places. `--geocode N` drains N of them here, otherwise the
enricher does it in the background.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

from .. import core

E7 = 1e7
DEGREES = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*°?\s*,\s*(-?\d+(?:\.\d+)?)\s*°?\s*$")

SRC_VISIT = "gtl:visit"
SRC_ACTIVITY = "gtl:activity"
SRC_PATH = "gtl:path"
SRC_RAW = "gtl:raw"

NOTHING_FOUND = (
    "nothing to import: no position found in this export.\n"
    "Google keeps Timeline on the phone now, so a Takeout of it holds settings only. "
    "Export from the phone instead: Settings > Location > Location services > Timeline > "
    "Export Timeline data.")


# ----------------------------------------------------------------------------- parsing

def coords(value):
    """Latitude and longitude out of every shape Google has ever written them in."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if text.lower().startswith("geo:"):
            text = text[4:]
        m = DEGREES.match(text)
        if m:
            return float(m.group(1)), float(m.group(2))
        return None
    if isinstance(value, dict):
        for key in ("latLng", "LatLng", "latlng", "placeLocation", "location", "point"):
            if key in value:
                got = coords(value[key])
                if got:
                    return got
        for la, lo, scale in (("latitudeE7", "longitudeE7", E7), ("latE7", "lngE7", E7),
                              ("latitude", "longitude", 1.0), ("lat", "lng", 1.0),
                              ("lat", "lon", 1.0)):
            if value.get(la) is not None and value.get(lo) is not None:
                return float(value[la]) / scale, float(value[lo]) / scale
    return None


def stamp(value):
    """A timezone-aware UTC datetime out of an ISO string or epoch milliseconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / 1000, timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():                       # timestampMs, as a string
        return datetime.fromtimestamp(int(text) / 1000, timezone.utc)
    text = text.replace("Z", "+00:00")
    # fromisoformat before 3.11 chokes on more than 6 fractional digits
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def sane(point):
    """A position a phone can actually have measured. Null island is never one: it is what a
    missing field looks like after two casts, and it would put a stay off the coast of Ghana
    in the middle of an otherwise correct week."""
    if not point:
        return None
    lat, lon = point
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return None
    if lat == 0 and lon == 0:
        return None
    return lat, lon


class Bag:
    """Everything one export holds, one list per table, deduplicated on its own key.

    The file itself repeats: the same visit shows up in two segments, the same fix in two raw
    signals. Deduplicating here rather than leaning on the database keeps the insert honest
    about how much is really new.
    """

    def __init__(self):
        self.visits, self.activities, self.paths = [], [], []
        self.raw, self.records, self.wifi, self.memories = [], [], [], []
        self.places, self.pings = {}, []
        self.profile = None
        self._seen = set()

    def _new(self, *key):
        if key in self._seen:
            return False
        self._seen.add(key)
        return True

    def place(self, place_id, point, label=None):
        if not place_id or not point:
            return
        row = self.places.setdefault(place_id, {"place_id": place_id, "lat": point[0],
                                                "lon": point[1], "profile_label": None})
        if label:
            row["profile_label"] = label

    def ping(self, when, point, source, label=None, accuracy=None):
        """`location_pings` stays the spine that every reader shares."""
        point = sane(point)
        if not when or not point:
            return
        if self._new("ping", source, when, round(point[0], 7), round(point[1], 7)):
            self.pings.append({"captured_at": when, "lat": point[0], "lon": point[1],
                               "accuracy_m": accuracy, "label": label, "source": source})

    def visit(self, start, end, point, **kw):
        point = sane(point)
        if not start or not end or not point:
            return
        if self._new("visit", start, end, round(point[0], 7), round(point[1], 7)):
            self.visits.append(dict(started_at=start, ended_at=end, lat=point[0],
                                    lon=point[1], **kw))
        # Two pings, one at each end. The time model already reads "same place twice" as
        # presence, so two rows reproduce the whole stay without inventing points inside it.
        for when in (start, end):
            self.ping(when, point, SRC_VISIT, kw.get("semantic_type"))

    def activity(self, start, end, a, b, **kw):
        a, b = sane(a), sane(b)
        if not start or not end or not a or not b:
            return
        if self._new("act", start, end, round(a[0], 7), round(a[1], 7),
                     round(b[0], 7), round(b[1], 7)):
            self.activities.append(dict(started_at=start, ended_at=end, start_lat=a[0],
                                        start_lon=a[1], end_lat=b[0], end_lon=b[1], **kw))
        self.ping(start, a, SRC_ACTIVITY, kw.get("mode"))
        self.ping(end, b, SRC_ACTIVITY, kw.get("mode"))

    def path(self, when, point):
        point = sane(point)
        if not when or not point:
            return
        if self._new("path", when, round(point[0], 7), round(point[1], 7)):
            self.paths.append({"captured_at": when, "lat": point[0], "lon": point[1]})

    def position(self, when, point, **kw):
        point = sane(point)
        if not when or not point:
            return
        if self._new("raw", when, round(point[0], 7), round(point[1], 7)):
            self.raw.append(dict(captured_at=when, lat=point[0], lon=point[1], **kw))

    def record(self, when, candidates):
        if not when or not self._new("rec", when):
            return
        top = max(candidates, key=lambda c: c.get("confidence") or 0) if candidates else {}
        self.records.append({"captured_at": when, "top_type": top.get("type"),
                             "top_conf": number(top.get("confidence")),
                             "candidates": json.dumps(candidates)})

    def scan(self, when, devices):
        if not when or not self._new("wifi", when):
            return
        self.wifi.append({"captured_at": when, "device_count": len(devices),
                          "devices": json.dumps(devices)})

    def memory(self, start, end, trip, offsets):
        if not start or not end or not self._new("mem", start, end):
            return
        self.memories.append({"started_at": start, "ended_at": end,
                              "start_offset_m": offsets[0], "end_offset_m": offsets[1],
                              "distance_km": number(trip.get("distanceFromOriginKms")),
                              "destinations": json.dumps(trip.get("destinations") or [])})

    def counts(self):
        return {"visits": len(self.visits), "activities": len(self.activities),
                "path_points": len(self.paths), "raw_positions": len(self.raw),
                "activity_records": len(self.records), "wifi_scans": len(self.wifi),
                "memories": len(self.memories), "google_places": len(self.places),
                "pings": len(self.pings)}

    def span(self):
        times = [p["captured_at"] for p in self.pings]
        times += [r["captured_at"] for r in self.raw]
        return (min(times), max(times)) if times else (None, None)


# ----------------------------------------------------------------------------- readers

def read_segment(seg, bag):
    """One segment of the on-device export: a visit, an activity, a path, or a memory."""
    start, end = stamp(seg.get("startTime")), stamp(seg.get("endTime"))
    offsets = (seg.get("startTimeTimezoneUtcOffsetMinutes"),
               seg.get("endTimeTimezoneUtcOffsetMinutes"))

    visit = seg.get("visit")
    if isinstance(visit, dict):
        top = visit.get("topCandidate") or {}
        point = coords(top.get("placeLocation")) or coords(top)
        bag.place(top.get("placeId"), sane(point))
        bag.visit(start, end, point,
                  start_offset_m=offsets[0], end_offset_m=offsets[1],
                  google_place_id=top.get("placeId"),
                  semantic_type=top.get("semanticType"),
                  probability=number(top.get("probability")),
                  hierarchy_level=visit.get("hierarchyLevel"))

    activity = seg.get("activity")
    if isinstance(activity, dict):
        top = activity.get("topCandidate") or {}
        bag.activity(start, end, coords(activity.get("start")), coords(activity.get("end")),
                     start_offset_m=offsets[0], end_offset_m=offsets[1],
                     mode=top.get("type"),
                     probability=number(top.get("probability") or activity.get("probability")),
                     distance_m=number(activity.get("distanceMeters")))

    path = seg.get("timelinePath")
    if isinstance(path, list):
        for step in path:
            when = stamp(step.get("time"))
            if not when and start is not None:
                offset = step.get("durationMinutesOffsetFromStartTime")
                if offset is not None:
                    when = start + timedelta(minutes=float(offset))
            bag.path(when, coords(step.get("point")))

    memory = seg.get("timelineMemory")
    if isinstance(memory, dict) and isinstance(memory.get("trip"), dict):
        bag.memory(start, end, memory["trip"], offsets)


def read_timeline_object(obj, bag):
    """One entry of a classic Semantic Location History file."""
    visit = obj.get("placeVisit")
    if isinstance(visit, dict):
        loc = visit.get("location") or {}
        point = coords(loc)
        bag.place(loc.get("placeId"), sane(point))
        span = visit.get("duration") or {}
        bag.visit(stamp(span.get("startTimestamp") or span.get("startTimestampMs")),
                  stamp(span.get("endTimestamp") or span.get("endTimestampMs")), point,
                  google_place_id=loc.get("placeId"),
                  semantic_type=loc.get("name") or loc.get("address"),
                  probability=number(visit.get("visitConfidence")),
                  hierarchy_level=None, start_offset_m=None, end_offset_m=None)

    act = obj.get("activitySegment")
    if isinstance(act, dict):
        span = act.get("duration") or {}
        start = stamp(span.get("startTimestamp") or span.get("startTimestampMs"))
        end = stamp(span.get("endTimestamp") or span.get("endTimestampMs"))
        bag.activity(start, end, coords(act.get("startLocation")),
                     coords(act.get("endLocation")),
                     mode=act.get("activityType"),
                     probability=number(act.get("confidence")),
                     distance_m=number(act.get("distance") or act.get("distanceMeters")),
                     start_offset_m=None, end_offset_m=None)
        for point in ((act.get("simplifiedRawPath") or {}).get("points") or []):
            bag.path(stamp(point.get("timestampMs") or point.get("timestamp")), coords(point))
        # Waypoints carry no time of their own, so spread them evenly over the segment.
        marks = (act.get("waypointPath") or {}).get("waypoints") or []
        if marks and start and end and len(marks) > 1:
            step = (end - start) / (len(marks) - 1)
            for i, mark in enumerate(marks):
                bag.path(start + step * i, coords(mark))


def read_raw_signal(sig, bag):
    """One raw signal: a fix, a sensor guess, or a wifi scan."""
    pos = sig.get("position")
    if isinstance(pos, dict):
        when = stamp(pos.get("timestamp"))
        bag.position(when, coords(pos), accuracy_m=number(pos.get("accuracyMeters")),
                     altitude_m=number(pos.get("altitudeMeters")),
                     speed_ms=number(pos.get("speedMetersPerSecond")),
                     source=pos.get("source"))
        bag.ping(when, coords(pos), SRC_RAW, accuracy=number(pos.get("accuracyMeters")))

    rec = sig.get("activityRecord")
    if isinstance(rec, dict):
        bag.record(stamp(rec.get("timestamp")), rec.get("probableActivities") or [])

    scan = sig.get("wifiScan")
    if isinstance(scan, dict):
        bag.scan(stamp(scan.get("deliveryTime")), scan.get("devicesRecords") or [])


def read_record(rec, bag):
    """One fix of a classic Records.json."""
    when = stamp(rec.get("timestamp") or rec.get("timestampMs"))
    point = coords(rec)
    bag.position(when, point, accuracy_m=number(rec.get("accuracy")),
                 altitude_m=number(rec.get("altitude")),
                 speed_ms=number(rec.get("velocity")), source=rec.get("source"))
    bag.ping(when, point, SRC_RAW, accuracy=number(rec.get("accuracy")))


def read_profile(profile, bag):
    """Google's summary of your habits: which places are home and work, and your commutes.

    This is where `HOME` comes from, and home is what every kilometres-from-home number in
    the archive is measured against. Without it there is no such thing as being away.
    """
    if not isinstance(profile, dict):
        return
    for place in profile.get("frequentPlaces") or []:
        bag.place(place.get("placeId"), sane(coords(place.get("placeLocation"))),
                  label=place.get("label"))
    bag.profile = {"frequent_places": json.dumps(profile.get("frequentPlaces") or []),
                   "frequent_trips": json.dumps(profile.get("frequentTrips") or [])}


def read_file(path, bag, log=print):
    with open(path, encoding="utf-8") as fh:
        try:
            payload = json.load(fh)
        except ValueError as err:
            log("  skipped %s: not JSON (%s)" % (os.path.basename(path), err))
            return False

    before = sum(bag.counts().values())
    if isinstance(payload, list):
        for seg in payload:
            if isinstance(seg, dict):
                if "placeVisit" in seg or "activitySegment" in seg:
                    read_timeline_object(seg, bag)
                else:
                    read_segment(seg, bag)
    elif isinstance(payload, dict):
        for seg in payload.get("semanticSegments") or []:
            read_segment(seg, bag)
        for obj in payload.get("timelineObjects") or []:
            read_timeline_object(obj, bag)
        for sig in payload.get("rawSignals") or []:
            read_raw_signal(sig, bag)
        for rec in payload.get("locations") or []:
            read_record(rec, bag)
        read_profile(payload.get("userLocationProfile"), bag)

    added = sum(bag.counts().values()) - before
    if added:
        log("  %-44s %7d rows" % (os.path.basename(path)[:44], added))
    return bool(added)


def json_files(target):
    if os.path.isdir(target):
        found = []
        for root, _, names in os.walk(target):
            found += [os.path.join(root, n) for n in sorted(names)
                      if n.lower().endswith(".json")]
        return sorted(found)
    return [target]


def collect(target, log=print):
    bag = Bag()
    for path in json_files(target):
        read_file(path, bag, log=log)
    for rows in (bag.visits, bag.activities, bag.paths, bag.raw, bag.records,
                 bag.wifi, bag.memories, bag.pings):
        rows.sort(key=lambda r: r.get("started_at") or r.get("captured_at"))
    return bag


def digest(target):
    """One sha256 over the whole export, so a file already imported is recognised as such."""
    h, total = hashlib.sha256(), 0
    for path in json_files(target):
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
                total += len(chunk)
    return h.hexdigest(), total


# ----------------------------------------------------------------------------- writing

TABLES = [
    ("location_google_places", "places",
     ("place_id", "lat", "lon", "profile_label")),
    ("location_visits", "visits",
     ("started_at", "ended_at", "start_offset_m", "end_offset_m", "google_place_id",
      "lat", "lon", "semantic_type", "probability", "hierarchy_level")),
    ("location_activities", "activities",
     ("started_at", "ended_at", "start_offset_m", "end_offset_m", "mode", "probability",
      "distance_m", "start_lat", "start_lon", "end_lat", "end_lon")),
    ("location_path_points", "paths", ("captured_at", "lat", "lon")),
    ("location_raw_positions", "raw",
     ("captured_at", "lat", "lon", "accuracy_m", "altitude_m", "speed_ms", "source")),
    ("location_activity_records", "records",
     ("captured_at", "top_type", "top_conf", "candidates")),
    ("location_wifi_scans", "wifi", ("captured_at", "device_count", "devices")),
    ("location_memories", "memories",
     ("started_at", "ended_at", "start_offset_m", "end_offset_m", "distance_km",
      "destinations")),
    ("location_pings", "pings",
     ("captured_at", "lat", "lon", "accuracy_m", "label", "source")),
]


def write(bag, chunk=2000, log=print):
    """Everything in one transaction, so a failure halfway leaves nothing behind."""
    written = {}
    with core.db().connection() as conn:
        for table, attr, cols in TABLES:
            rows = getattr(bag, attr)
            if isinstance(rows, dict):
                rows = list(rows.values())
            if not rows:
                continue
            sql = "insert into %s (%s) values (%s) on conflict do nothing" % (
                table, ", ".join(cols), ", ".join(["%s"] * len(cols)))
            done = 0
            for i in range(0, len(rows), chunk):
                batch = [tuple(r.get(c) for c in cols) for r in rows[i:i + chunk]]
                with conn.cursor() as cur:
                    cur.executemany(sql, batch)
                    done += max(0, cur.rowcount or 0)
            written[table] = done
            log("  %-28s %7d new of %7d" % (table, done, len(rows)))

        # A place named in the profile keeps its HOME or WORK label even when the visits that
        # first mentioned it were imported earlier with nothing but coordinates.
        for row in bag.places.values():
            if row.get("profile_label"):
                with conn.cursor() as cur:
                    cur.execute("update location_google_places set profile_label = %s "
                                "where place_id = %s", (row["profile_label"], row["place_id"]))

        if bag.profile:
            with conn.cursor() as cur:
                cur.execute(
                    "insert into location_profile (id, frequent_places, frequent_trips) "
                    "values (1, %s, %s) on conflict (id) do update set "
                    "frequent_places = excluded.frequent_places, "
                    "frequent_trips = excluded.frequent_trips, updated_at = now()",
                    (bag.profile["frequent_places"], bag.profile["frequent_trips"]))

        # The per-place aggregates are derived, never carried: recompute them from the visits
        # that actually landed, so they stay true after a partial import.
        with conn.cursor() as cur:
            cur.execute("""
                update location_google_places g set
                  first_seen = s.first_seen, last_seen = s.last_seen,
                  visit_count = s.n, total_seconds = s.secs
                from (select google_place_id, min(started_at) first_seen,
                             max(ended_at) last_seen, count(*) n,
                             sum(extract(epoch from (ended_at - started_at)))::bigint secs
                      from location_visits where google_place_id is not null
                      group by 1) s
                where g.place_id = s.google_place_id""")
    return written


def record_import(target, bag, written):
    sha, size = digest(target)
    start, end = bag.span()
    core.execute(
        "insert into location_imports (file_sha256, file_bytes, span_start, span_end, counts) "
        "values (%s, %s, %s, %s, %s) on conflict (file_sha256) do update set "
        "imported_at = now(), counts = excluded.counts",
        (sha, size, start, end, json.dumps({"parsed": bag.counts(), "inserted": written})))
    return sha


def link_places(budget):
    """Resolve Google's places to the archive's own, at Nominatim's one call per second.

    Resolving the thousand-odd distinct places an export brings, rather than each of its
    thousands of visits, is what makes this finite: every visit reaches its address through
    the place id Google already attached to it.
    """
    rows = core.db().fetch_all(
        "select place_id, lat, lon from location_google_places where place_id_ref is null "
        "order by visit_count desc limit %s", (budget * 4,))
    resolved = called = 0
    for row in rows:
        if called >= budget:
            break
        place, did_call = core.resolve_place(row["lat"], row["lon"])
        if did_call:
            called += 1
        if not place:
            continue
        core.execute("update location_google_places set place_id_ref = %s where place_id = %s",
                     (place["id"], row["place_id"]))
        resolved += 1
        if did_call:
            time.sleep(core.settings().geocode_sleep_s)

    # Visits inherit the place their own coordinates already resolved to.
    core.execute("""
        update location_visits v set place_id = g.place_id_ref
        from location_google_places g
        where v.google_place_id = g.place_id and g.place_id_ref is not null
          and v.place_id is distinct from g.place_id_ref""")
    return resolved, called


def run(target, dry_run=False, no_raw=False, geocode=0, log=print):
    if not os.path.exists(target):
        raise SystemExit("no such file or directory: %s" % target)

    log("reading %s" % target)
    bag = collect(target, log=log)
    if no_raw:
        bag.raw, bag.records, bag.wifi = [], [], []
    counts = bag.counts()
    if not any(counts.values()):
        raise SystemExit(NOTHING_FOUND)
    start, end = bag.span()
    log(json.dumps(counts, indent=1))
    log("span %s -> %s" % (start.isoformat() if start else "?",
                           end.isoformat() if end else "?"))

    if dry_run:
        log("dry run, nothing written")
        return counts

    written = write(bag, log=log)
    record_import(target, bag, written)

    if geocode:
        resolved, called = link_places(geocode)
        log("resolved %d places with %d geocodes" % (resolved, called))

    left = core.db().fetch_one(
        "select count(*) as n from location_google_places where place_id_ref is null")["n"]
    if left:
        log("%d places still without an address. Run `tars-location enrich --geocode N`, "
            "or leave it to the background enricher." % left)
    return written
