"""The export parsers, which are where the bugs live.

Google has written coordinates five different ways across the life of Timeline, and a reader
that handles four of them loses a year of history in silence. Every shape below came out of a
real export.
"""

from datetime import datetime, timezone

from tars_location.importers import google_takeout as g


class TestCoords:
    def test_e7_integers(self):
        assert g.coords({"latitudeE7": 457640000, "longitudeE7": 48357000}) == (45.7640, 4.8357)

    def test_plain_floats_under_three_different_key_names(self):
        assert g.coords({"latitude": 45.76, "longitude": 4.84}) == (45.76, 4.84)
        assert g.coords({"lat": 45.76, "lng": 4.84}) == (45.76, 4.84)
        assert g.coords({"lat": 45.76, "lon": 4.84}) == (45.76, 4.84)

    def test_the_geo_string_the_on_device_export_uses(self):
        assert g.coords("geo:45.7640,4.8357") == (45.7640, 4.8357)

    def test_degrees_with_the_symbol_and_stray_spaces(self):
        assert g.coords(" 45.7640° , 4.8357° ") == (45.7640, 4.8357)

    def test_it_digs_through_the_wrappers(self):
        assert g.coords({"placeLocation": {"latLng": "geo:1.5,2.5"}}) == (1.5, 2.5)

    def test_nothing_readable_is_none_rather_than_a_guess(self):
        assert g.coords(None) is None
        assert g.coords("somewhere nice") is None
        assert g.coords({}) is None


class TestSane:
    def test_null_island_is_refused(self):
        """0,0 is what a missing field looks like after two casts. Accepting it puts a stay
        in the Gulf of Guinea in the middle of an otherwise correct week."""
        assert g.sane((0, 0)) is None

    def test_out_of_range_is_refused(self):
        assert g.sane((91.0, 0.5)) is None
        assert g.sane((10.0, 181.0)) is None

    def test_a_real_position_survives(self):
        assert g.sane((45.7640, 4.8357)) == (45.7640, 4.8357)


class TestStamp:
    def test_iso_with_a_zulu_suffix(self):
        assert g.stamp("2024-10-04T08:30:00Z") == datetime(2024, 10, 4, 8, 30, tzinfo=timezone.utc)

    def test_epoch_milliseconds_as_a_string(self):
        assert g.stamp("1728030600000") == datetime(2024, 10, 4, 8, 30, tzinfo=timezone.utc)

    def test_nanosecond_precision_does_not_crash_it(self):
        """Some exports carry nine fractional digits. fromisoformat before 3.11 refuses more
        than six, and an unhandled ValueError there drops a whole file."""
        assert g.stamp("2024-10-04T08:30:00.123456789Z") is not None

    def test_an_offset_is_normalised_to_utc(self):
        assert g.stamp("2024-10-04T10:30:00+02:00") == datetime(
            2024, 10, 4, 8, 30, tzinfo=timezone.utc)

    def test_garbage_is_none_rather_than_now(self):
        assert g.stamp("not a date") is None
        assert g.stamp(None) is None


class TestBag:
    def test_a_visit_writes_two_pings_one_at_each_end(self):
        """The time model reads 'same place twice' as presence. Two rows reproduce the whole
        stay without inventing points inside it."""
        bag = g.Bag()
        start = datetime(2024, 10, 4, 8, tzinfo=timezone.utc)
        end = datetime(2024, 10, 4, 18, tzinfo=timezone.utc)
        bag.visit(start, end, (45.76, 4.84), semantic_type="HOME")
        assert len(bag.visits) == 1
        assert len(bag.pings) == 2
        assert {p["captured_at"] for p in bag.pings} == {start, end}

    def test_the_same_visit_twice_lands_once(self):
        """Every export overlaps the last, so the file itself repeats. Deduplicating here
        keeps the insert honest about how much is really new."""
        bag = g.Bag()
        start = datetime(2024, 10, 4, 8, tzinfo=timezone.utc)
        end = datetime(2024, 10, 4, 18, tzinfo=timezone.utc)
        for _ in range(3):
            bag.visit(start, end, (45.76, 4.84))
        assert bag.counts()["visits"] == 1

    def test_a_visit_at_null_island_is_dropped_entirely(self):
        bag = g.Bag()
        bag.visit(datetime(2024, 10, 4, tzinfo=timezone.utc),
                  datetime(2024, 10, 4, 1, tzinfo=timezone.utc), (0, 0))
        assert bag.counts()["visits"] == 0
        assert bag.counts()["pings"] == 0


class TestReaders:
    def test_the_on_device_shape(self):
        bag = g.Bag()
        g.read_segment({
            "startTime": "2024-10-04T08:00:00Z",
            "endTime": "2024-10-04T18:00:00Z",
            "startTimeTimezoneUtcOffsetMinutes": 120,
            "visit": {"topCandidate": {"placeId": "ChIJ-test",
                                       "placeLocation": {"latLng": "geo:45.76,4.84"},
                                       "semanticType": "HOME", "probability": 0.9}},
        }, bag)
        assert bag.counts()["visits"] == 1
        assert bag.visits[0]["semantic_type"] == "HOME"
        assert bag.visits[0]["start_offset_m"] == 120
        assert "ChIJ-test" in bag.places

    def test_the_classic_takeout_shape(self):
        bag = g.Bag()
        g.read_timeline_object({"placeVisit": {
            "location": {"latitudeE7": 457600000, "longitudeE7": 48400000,
                         "placeId": "ChIJ-old", "name": "A cafe"},
            "duration": {"startTimestamp": "2019-03-01T09:00:00Z",
                         "endTimestamp": "2019-03-01T10:00:00Z"},
            "visitConfidence": 88}}, bag)
        assert bag.counts()["visits"] == 1
        assert bag.visits[0]["google_place_id"] == "ChIJ-old"

    def test_an_activity_keeps_googles_own_distance(self):
        """Recomputing it from the two endpoints would give the straight line, which for a
        drive is not the distance travelled and for a flight is not the route."""
        bag = g.Bag()
        g.read_segment({
            "startTime": "2024-10-04T08:00:00Z", "endTime": "2024-10-04T09:00:00Z",
            "activity": {"start": "geo:45.76,4.84", "end": "geo:45.50,4.72",
                         "distanceMeters": 42000,
                         "topCandidate": {"type": "IN_PASSENGER_VEHICLE"}},
        }, bag)
        assert bag.activities[0]["distance_m"] == 42000
        assert bag.activities[0]["mode"] == "IN_PASSENGER_VEHICLE"

    def test_the_profile_is_where_home_comes_from(self):
        """Every kilometres-from-home number in the archive traces back to this label."""
        bag = g.Bag()
        g.read_profile({"frequentPlaces": [
            {"placeId": "ChIJ-home", "placeLocation": "geo:45.76,4.84", "label": "HOME"}]}, bag)
        assert bag.places["ChIJ-home"]["profile_label"] == "HOME"

    def test_waypoints_with_no_timestamps_are_spread_over_the_segment(self):
        bag = g.Bag()
        g.read_timeline_object({"activitySegment": {
            "duration": {"startTimestamp": "2024-10-04T08:00:00Z",
                         "endTimestamp": "2024-10-04T09:00:00Z"},
            "startLocation": {"latitudeE7": 457600000, "longitudeE7": 48400000},
            "endLocation": {"latitudeE7": 455000000, "longitudeE7": 47200000},
            "waypointPath": {"waypoints": [
                {"latE7": 457600000, "lngE7": 48400000},
                {"latE7": 456800000, "lngE7": 47800000},
                {"latE7": 455000000, "lngE7": 47200000}]}}}, bag)
        assert bag.counts()["path_points"] == 3
        times = sorted(p["captured_at"] for p in bag.paths)
        assert times[0] == datetime(2024, 10, 4, 8, tzinfo=timezone.utc)
        assert times[-1] == datetime(2024, 10, 4, 9, tzinfo=timezone.utc)
