"""Windows, days and runs. Every bug this file pins was a wrong answer, not a crash.

Time is where a location archive lies to you. A day is local, a stay crosses midnight, a run
of visits is one time somewhere, and a silence is not a stay.
"""

from datetime import datetime, timedelta, timezone

import pytest

from tars_location import core

UTC = timezone.utc


def stay(start, end, city=None, country=None, off_m=0, place_id=1, label=None):
    return {"started_at": start, "ended_at": end, "city": city, "country": country,
            "off_m": off_m, "place_id": place_id, "label": label,
            "seconds": (end - start).total_seconds(), "country_code": None,
            "address": None, "tz": None, "trip_name": None}


class TestWindows:
    def test_a_named_period_returns_a_utc_pair(self):
        start, end, label, tz = core.resolve_window("today", tz="UTC")
        assert start.tzinfo == UTC and end.tzinfo == UTC
        assert label == "today" and tz == "UTC"

    def test_boundaries_are_local_not_utc(self):
        """'Yesterday' has to mean the user's yesterday. From California that is an eight-hour
        difference on both edges, and answering in UTC returns the wrong day's stays."""
        paris, _, _, _ = core.resolve_window("today", tz="Europe/Paris")
        la, _, _, _ = core.resolve_window("today", tz="America/Los_Angeles")
        assert paris != la

    def test_all_time_has_no_edges(self):
        start, end, label, _ = core.resolve_window("all", tz="UTC")
        assert start is None and end is None and label == "all time"

    def test_last_n_days_is_parsed_rather_than_enumerated(self):
        start, end, label, _ = core.resolve_window("last_45_days", tz="UTC")
        assert label == "last 45 days"
        assert (end - start).days in (44, 45)

    def test_an_explicit_end_date_is_inclusive_of_that_whole_day(self):
        """A user asking for 'until the 5th' means the end of the 5th. Treating the date as an
        exclusive midnight silently drops that day's stays."""
        _, end, _, _ = core.resolve_window(since="2025-03-01", until="2025-03-05", tz="UTC")
        assert end == datetime(2025, 3, 6, tzinfo=UTC)

    def test_since_and_until_beat_a_period(self):
        start, _, label, _ = core.resolve_window(
            period="today", since="2020-01-01", until="2020-02-01", tz="UTC")
        assert start == datetime(2020, 1, 1, tzinfo=UTC)
        assert "2020-01-01" in label

    def test_a_period_nobody_defined_says_so(self):
        with pytest.raises(ValueError, match="unknown period"):
            core.resolve_window("last_fortnight", tz="UTC")

    def test_an_unreadable_date_says_what_it_wanted(self):
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            core.resolve_window(since="last tuesday", tz="UTC")


class TestDaysTouched:
    def test_an_overnight_stay_counts_for_two_days(self):
        """A reader that records only the start date under-counts by exactly the nights, which
        is the difference between 'twelve days in Rome' and 'six'."""
        days = core.days_touched(stay(datetime(2025, 3, 1, 22, tzinfo=UTC),
                                      datetime(2025, 3, 2, 9, tzinfo=UTC)))
        assert days == {"2025-03-01", "2025-03-02"}

    def test_the_local_offset_decides_the_date(self):
        """23:30 UTC in Bangkok is already the next morning there, and that is the day the
        user will name."""
        late = stay(datetime(2025, 3, 1, 23, 30, tzinfo=UTC),
                    datetime(2025, 3, 1, 23, 45, tzinfo=UTC), off_m=420)
        assert late["started_at"].date().isoformat() == "2025-03-01"
        assert core.days_touched(late) == {"2025-03-02"}

    def test_a_runaway_span_is_capped_rather_than_looping(self):
        long = stay(datetime(2020, 1, 1, tzinfo=UTC), datetime(2030, 1, 1, tzinfo=UTC))
        assert len(core.days_touched(long)) == 400


class TestMerge:
    def test_two_stops_in_one_city_an_hour_apart_are_one_time_there(self):
        rows = [
            stay(datetime(2025, 3, 1, 9, tzinfo=UTC), datetime(2025, 3, 1, 11, tzinfo=UTC),
                 city="Lisbon"),
            stay(datetime(2025, 3, 1, 12, tzinfo=UTC), datetime(2025, 3, 1, 18, tzinfo=UTC),
                 city="Lisbon")]
        merged = core.merge(rows, by="city")
        assert len(merged) == 1
        assert merged[0]["stops"] == 2

    def test_the_hour_in_between_belongs_to_neither(self):
        """That gap is travel. Folding it into the total inflates every 'how long was I there'
        by exactly the time spent going somewhere else."""
        rows = [
            stay(datetime(2025, 3, 1, 9, tzinfo=UTC), datetime(2025, 3, 1, 11, tzinfo=UTC),
                 city="Lisbon"),
            stay(datetime(2025, 3, 1, 12, tzinfo=UTC), datetime(2025, 3, 1, 18, tzinfo=UTC),
                 city="Lisbon")]
        merged = core.merge(rows, by="city")
        assert merged[0]["seconds"] == (2 + 6) * 3600

    def test_a_long_silence_breaks_the_run_whatever_the_city_says(self):
        """Without this, a hole in the archive glues the last stop before it to the first one
        after, and four separate trips read as one very long visit."""
        rows = [
            stay(datetime(2024, 1, 1, 9, tzinfo=UTC), datetime(2024, 1, 1, 18, tzinfo=UTC),
                 city="Lisbon"),
            stay(datetime(2025, 1, 1, 9, tzinfo=UTC), datetime(2025, 1, 1, 18, tzinfo=UTC),
                 city="Lisbon")]
        assert len(core.merge(rows, by="city")) == 2

    def test_a_row_with_no_key_is_skipped_not_bucketed_under_none(self):
        rows = [stay(datetime(2025, 3, 1, 9, tzinfo=UTC), datetime(2025, 3, 1, 11, tzinfo=UTC))]
        assert core.merge(rows, by="city") == []


class TestSummarize:
    def test_it_counts_separate_visits_not_stops(self):
        rows = [
            stay(datetime(2025, 1, 1, 9, tzinfo=UTC), datetime(2025, 1, 1, 18, tzinfo=UTC),
                 city="Porto"),
            stay(datetime(2025, 1, 1, 19, tzinfo=UTC), datetime(2025, 1, 1, 23, tzinfo=UTC),
                 city="Porto"),
            stay(datetime(2025, 6, 1, 9, tzinfo=UTC), datetime(2025, 6, 1, 18, tzinfo=UTC),
                 city="Porto"),
        ]
        got = core.summarize(rows, by="city")
        assert len(got) == 1
        assert got[0]["visits"] == 2
        assert got[0]["stops"] == 3

    def test_most_time_first(self):
        rows = [
            stay(datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 1, 1, 2, tzinfo=UTC),
                 city="Porto"),
            stay(datetime(2025, 2, 1, tzinfo=UTC), datetime(2025, 2, 5, tzinfo=UTC),
                 city="Lisbon"),
        ]
        assert [a["name"] for a in core.summarize(rows, by="city")] == ["Lisbon", "Porto"]


class TestRendering:
    def test_humanize_drops_to_the_unit_that_matters(self):
        assert core.humanize(90) == "1m"
        assert core.humanize(3 * 3600 + 5 * 60) == "3h 05m"
        assert core.humanize(timedelta(days=2, hours=3).total_seconds()) == "2d 3h"

    def test_iso_survives_the_decimal_postgres_returns(self):
        """json.dumps refuses a Decimal outright, and a sum of distances comes back as one."""
        from decimal import Decimal
        assert core.iso(Decimal("42")) == 42
        assert core.iso(Decimal("42.5")) == 42.5
        assert core.iso(datetime(2025, 1, 1, tzinfo=UTC)).startswith("2025-01-01")


class TestHaversine:
    def test_a_missing_side_is_infinitely_far_not_zero(self):
        """Returning 0 would make an unknown position match every radius, and every unresolved
        ping would attach itself to the first place in the table."""
        assert core.haversine_m(None, 2.0, 48.0, 2.0) == float("inf")

    def test_a_known_distance(self):
        # Paris to London, about 344 km.
        metres = core.haversine_m(48.8566, 2.3522, 51.5074, -0.1278)
        assert 340_000 < metres < 350_000
