"""The schema, against a real Postgres.

A migration reviewed by reading is a guess. These build the whole thing from empty, feed it a
synthetic month, and check that the views answer correctly, including the cases that are wrong
in every naive implementation: the seam between two sources, home changing address, a run
broken by a gap, and a trip keeping a name someone typed.

Skipped unless LOCATION_TEST_DATABASE_URL points at a database this may drop schemas in.

    docker run -d --name location-test -e POSTGRES_PASSWORD=test -p 5433:5432 postgres:16
    LOCATION_TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:5433/postgres pytest
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import psycopg
import pytest

from tars_location import core
from tars_location.cli import MIGRATIONS
from tars_location.config import Settings

UTC = timezone.utc
URL = os.environ.get("LOCATION_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(not URL, reason="set LOCATION_TEST_DATABASE_URL")


def at(day, hour=12):
    return datetime(2025, 3, day, hour, tzinfo=UTC)


@pytest.fixture()
def archive():
    """A fresh schema and a month of synthetic history, torn down after."""
    core.use(Settings(database_url=URL, geocoder_contact="test@example.com",
                      fallback_tz="UTC", place_radius_m=40, geocode_sleep_s=0,
                      read_only_row_cap=2000))
    with core.db().connection() as conn:
        conn.execute("drop schema public cascade; create schema public;")
        conn.execute((MIGRATIONS / "0001_core.sql").read_text())

    # Two places: home in Paris, and somewhere in Lisbon.
    home = core.db().fetch_one(
        "insert into location_places (lat, lon, address, label, city, country, country_code, tz) "
        "values (48.8566, 2.3522, '1 rue Test, Paris', 'Home', 'Paris', 'France', 'fr', "
        "'Europe/Paris') returning *")
    away = core.db().fetch_one(
        "insert into location_places (lat, lon, address, label, city, country, country_code, tz) "
        "values (38.7223, -9.1393, '2 rua Test, Lisboa', 'Hotel', 'Lisbon', 'Portugal', 'pt', "
        "'Europe/Lisbon') returning *")

    # Google's own place identity is what carries the HOME label, and HOME is what every
    # distance-from-home number is measured against.
    core.execute(
        "insert into location_google_places (place_id, lat, lon, profile_label, place_id_ref, "
        "first_seen, last_seen, visit_count, total_seconds) "
        "values ('g-home', 48.8566, 2.3522, 'HOME', %s, %s, %s, 10, 864000)",
        (home["id"], at(1), at(28)))

    # Days 1 to 5 at home, 10 to 16 in Lisbon, 20 to 22 at home. Nothing on 6 to 9 or 17 to 19,
    # which is what a real archive looks like.
    rows = ([(at(d, 8), at(d, 20), home["id"]) for d in range(1, 6)]
            + [(at(d, 8), at(d, 20), away["id"]) for d in range(10, 17)]
            + [(at(d, 8), at(d, 20), home["id"]) for d in range(20, 23)])
    for start, end, place in rows:
        core.execute(
            "insert into location_visits (started_at, ended_at, start_offset_m, lat, lon, "
            "place_id, google_place_id, semantic_type) values (%s, %s, 0, 0.1, 0.1, %s, "
            "case when %s = %s then 'g-home' else null end, 'INFERRED_HOME')",
            (start, end, place, place, home["id"]))

    core.execute("select location_refresh_days()")
    yield {"home": home, "away": away}
    with core.db().connection() as conn:
        conn.execute("drop schema public cascade; create schema public;")


class TestMigration:
    def test_it_is_idempotent(self, archive):
        """Running it twice is the normal case after an upgrade, and a migration that only
        works on an empty database is one nobody can apply."""
        with core.db().connection() as conn:
            conn.execute((MIGRATIONS / "0001_core.sql").read_text())
        assert core.db().fetch_one("select count(*) as n from location_places")["n"] == 2

    def test_every_object_the_code_reads_exists(self, archive):
        for name in ("location_places", "location_pings", "location_visits",
                     "location_activities", "location_trips", "location_v_stays",
                     "location_v_days", "location_v_day_home", "location_m_day_home",
                     "location_v_home_periods", "location_v_records",
                     "location_v_trip_segments", "location_place_stats"):
            got = core.db().fetch_one("select to_regclass(%s) as v", (f"public.{name}",))
            assert got["v"], f"{name} is missing"


class TestStays:
    def test_the_spine_carries_every_visit(self, archive):
        rows = core.stay_rows(limit=100)
        assert len(rows) == 15

    def test_a_stay_knows_its_local_date(self, archive):
        row = core.stay_rows(limit=1)[0]
        assert str(row["day"]) == "2025-03-01"

    def test_a_window_overlaps_rather_than_contains(self, archive):
        """Asking about one Tuesday in the middle of a week away has to return that week's
        stay, not nothing."""
        rows = core.stay_rows(at(13, 0), at(14, 0))
        assert rows and all(r["city"] == "Lisbon" for r in rows)

    def test_the_live_tail_starts_after_the_last_import(self, archive):
        """Without the cut, a day present in both sources reads as two separate stays in the
        same place and every duration doubles."""
        core.execute(
            "insert into location_pings (captured_at, lat, lon, place_id, source) "
            "values (%s, 48.8566, 2.3522, %s, 'test')", (at(3, 12), archive["home"]["id"]))
        sources = {r["source"] for r in core.stay_rows(at(3, 0), at(4, 0))}
        assert sources == {"visit"}

    def test_a_ping_after_the_last_visit_becomes_a_stay(self, archive):
        for hour in (8, 9, 10):
            core.execute(
                "insert into location_pings (captured_at, lat, lon, place_id, source) "
                "values (%s, 38.7223, -9.1393, %s, 'test')",
                (datetime(2025, 4, 1, hour, tzinfo=UTC), archive["away"]["id"]))
        rows = core.stay_rows(datetime(2025, 4, 1, tzinfo=UTC),
                              datetime(2025, 4, 2, tzinfo=UTC))
        assert len(rows) == 1 and rows[0]["source"] == "ping"
        assert rows[0]["seconds"] == 2 * 3600


class TestHomeAndDistance:
    def test_home_is_read_from_the_profile_label(self, archive):
        homes = core.db().fetch_all("select * from location_v_home_periods")
        assert len(homes) == 1 and homes[0]["city"] == "Paris"

    def test_a_day_away_measures_the_real_distance(self, archive):
        row = core.db().fetch_one(
            "select km_from_home from location_m_day_home where day = '2025-03-12'")
        # Paris to Lisbon is about 1450 km.
        assert 1400 < row["km_from_home"] < 1500

    def test_a_day_at_home_is_zero_from_home(self, archive):
        row = core.db().fetch_one(
            "select km_from_home from location_m_day_home where day = '2025-03-02'")
        assert row["km_from_home"] < 1


class TestTrips:
    def test_the_week_away_is_detected_as_one_trip(self, archive):
        core.db().fetch_one("select * from location_detect_trips(1)")
        trips = core.db().fetch_all("select * from location_trips order by started_at")
        assert len(trips) == 1
        assert trips[0]["primary_country"] == "Portugal"
        assert trips[0]["nights"] == 6

    def test_the_days_at_home_are_not_a_trip(self, archive):
        core.db().fetch_one("select * from location_detect_trips(1)")
        assert not core.db().fetch_all(
            "select 1 from location_trips where primary_country = 'France'")

    def test_a_name_you_typed_survives_re_detection(self, archive):
        """Without name_is_auto, every re-run silently renames 'Honeymoon' back to
        'Portugal - March 2025'."""
        core.db().fetch_one("select * from location_detect_trips(1)")
        core.execute("update location_trips set name = 'Honeymoon', name_is_auto = false")
        core.db().fetch_one("select * from location_detect_trips(1)")
        assert core.db().fetch_one("select name from location_trips")["name"] == "Honeymoon"

    def test_a_stay_inside_a_trip_carries_its_name(self, archive):
        core.db().fetch_one("select * from location_detect_trips(1)")
        rows = core.stay_rows(at(12, 0), at(13, 0))
        assert rows[0]["trip_name"]


class TestCoverage:
    def test_it_counts_what_is_actually_there(self, archive):
        have = core.coverage()
        assert have["visits"] == 15
        assert have["places"] == 2
        assert have["countries"] == 2

    def test_the_gaps_are_measured_not_remembered(self, archive):
        """'You were never in Portugal' and 'nothing was recorded that month' are different
        answers, and only one of them is honest."""
        holes = core.gaps(min_days=3)
        spans = {(h["from"], h["to"]) for h in holes}
        assert ("2025-03-05", "2025-03-10") in spans
        assert ("2025-03-16", "2025-03-20") in spans

    def test_a_continuous_archive_reports_no_gap(self, archive):
        assert core.gaps(min_days=90) == []


class TestReadOnlyGuard:
    def test_a_select_works(self, archive):
        rows = core.db().fetch_read_only("select count(*) as n from location_places", 10)
        assert rows[0]["n"] == 2

    def test_postgres_itself_refuses_a_write(self, archive):
        """A single valid SELECT, no keyword an inspector would flag, and it writes: the
        function behind it refreshes a materialized view. Nothing but the read-only
        transaction stops this one, which is why the guard is the transaction and not a
        promise from the model to send only SELECT. An agent will eventually be handed a
        query by a web page it was asked to summarize."""
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            core.db().fetch_read_only("select location_refresh_days()", 10)

    def test_a_write_smuggled_in_a_cte_is_refused(self, archive):
        """Valid SQL and a plain SELECT at the top level, and it empties a table. Caught a
        layer earlier than the one above: the query is wrapped in a subquery to apply the row
        cap, and a data-modifying CTE cannot live there. Both layers are load-bearing."""
        before = core.db().fetch_one("select count(*) as n from location_places")["n"]
        with pytest.raises(psycopg.Error):
            core.db().fetch_read_only(
                "with x as (delete from location_places returning *) select * from x", 10)
        assert core.db().fetch_one("select count(*) as n from location_places")["n"] == before

    def test_a_second_statement_is_refused_before_it_reaches_the_database(self, archive):
        with pytest.raises(ValueError, match="one statement"):
            core.db().fetch_read_only("select 1; drop table location_places", 10)

    def test_the_row_cap_is_a_ceiling_not_a_suggestion(self, archive):
        core.use(Settings(database_url=URL, geocoder_contact="t@e.com", fallback_tz="UTC",
                          place_radius_m=40, geocode_sleep_s=0, read_only_row_cap=1))
        rows = core.db().fetch_read_only("select * from location_places", 500)
        assert len(rows) == 1


class TestPeopleBridge:
    def test_it_refuses_to_install_without_a_people_table(self, archive):
        """Half-installing would leave a view that raises at read time, and an agent told
        'relation does not exist' invents an explanation for it."""
        with pytest.raises(Exception, match="people"):
            with core.db().connection() as conn:
                conn.execute((MIGRATIONS / "0002_people_bridge.sql").read_text())

    def test_it_installs_when_one_exists(self, archive):
        with core.db().connection() as conn:
            # Quoted, because that is how people-memory declares it: `current_role` is reserved.
            conn.execute('create table people (id bigserial primary key, full_name text, '
                         'current_org text, "current_role" text)')
            conn.execute((MIGRATIONS / "0002_people_bridge.sql").read_text())
        assert core.db().fetch_one(
            "select to_regclass('public.location_v_trip_people') as v")["v"]

    def test_the_persons_job_never_comes_back_as_a_database_role(self, archive):
        """`select current_role from ...` is the SQL function, not the column, and Postgres
        prefers the function without a word of warning. Every person would have read as
        'postgres'. The view renames it so the trap cannot be walked into downstream."""
        with core.db().connection() as conn:
            conn.execute('create table people (id bigserial primary key, full_name text, '
                         'current_org text, "current_role" text)')
            conn.execute("insert into people (full_name, \"current_role\") "
                         "values ('A Friend', 'Photographer')")
            conn.execute((MIGRATIONS / "0002_people_bridge.sql").read_text())
        core.db().fetch_one("select * from location_detect_trips(1)")
        core.execute("insert into location_trip_people (trip_id, person_id) "
                     "select id, 1 from location_trips")
        from tars_location import server
        out = server.run_tool("who_was_there", {
            "trip": core.db().fetch_one("select slug from location_trips")["slug"]})
        assert out["people"][0]["job_title"] == "Photographer"

    def test_the_core_schema_never_mentions_people(self):
        """The whole point of the bridge being separate. A reference here would make the
        people graph undroppable and tie the two products' migrations together forever."""
        sql = (MIGRATIONS / "0001_core.sql").read_text().lower()
        assert " people " not in sql and "people(" not in sql


class TestServerDegradesWithoutTheBridge:
    def test_who_was_there_says_what_is_missing(self, archive):
        from tars_location import server
        out = server.run_tool("who_was_there", {"person": "anyone"})
        assert out["available"] is False
        assert "0002_people_bridge" in out["note"]

    def test_trips_still_work_without_it(self, archive):
        from tars_location import server
        core.db().fetch_one("select * from location_detect_trips(1)")
        out = server.run_tool("trips", {})
        assert len(out["trips"]) == 1

    def test_coverage_reports_its_gaps_through_the_tool(self, archive):
        """The tool uses the two-week threshold, so the fixture's few missing days are correctly
        invisible to it. A silence long enough to change an answer is not."""
        from tars_location import server
        core.execute(
            "insert into location_visits (started_at, ended_at, start_offset_m, lat, lon, "
            "place_id) values (%s, %s, 0, 0.1, 0.1, %s)",
            (datetime(2025, 6, 1, 8, tzinfo=UTC), datetime(2025, 6, 1, 20, tzinfo=UTC),
             archive["home"]["id"]))
        core.execute("select location_refresh_days()")
        out = server.run_tool("location_coverage", {})
        assert out["gaps"] and "gap" in out["note"]
        assert out["gaps"][0]["missing_days"] > 60
