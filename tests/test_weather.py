"""The weather maths, with no network and no database.

A forecast is somebody else's number, so the only things worth pinning here are the ones this
package decides for itself: how a probability is counted out of ensemble members, which model
gets to be in the count, what window "today" means at four in the afternoon, and what a unit
is when every key in the payload carries a model name after it. Every one of these was a
wrong answer at some point on the way in, not a crash.
"""

from datetime import datetime, timedelta

import pytest

from tars_location import weather as w


def hours(n, start="2026-08-13T00:00"):
    base = datetime.fromisoformat(start)
    return [(base + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(n)]


def ensemble_payload(members, times=None, offset=7200):
    """An ensemble response shaped exactly like the real one: control run unsuffixed, members
    numbered, model name last. `members` maps a tag to its hourly series."""
    times = times or hours(24)
    hourly = {"time": times}
    hourly.update({"precipitation_%s" % tag: values for tag, values in members.items()})
    return {"utc_offset_seconds": offset, "timezone": "Europe/Paris",
            "hourly_units": {"precipitation": "mm"}, "hourly": hourly}


class TestSeries:
    def test_it_reads_the_forecast_shape(self):
        payload = {"hourly": {"time": hours(2), "precipitation_icon_seamless": [0, 1],
                              "precipitation_ecmwf_ifs025": [2, 3]}}
        assert w.series(payload, "precipitation") == {"icon_seamless": [0, 1],
                                                      "ecmwf_ifs025": [2, 3]}

    def test_it_reads_the_ensemble_shape_including_the_control_run(self):
        payload = {"hourly": {"time": hours(1), "precipitation": [9],
                              "precipitation_member01_icon_seamless_eps": [1]}}
        found = w.series(payload, "precipitation")
        assert found["control"] == [9]
        assert found["member01_icon_seamless_eps"] == [1]

    def test_one_variable_never_swallows_another(self):
        """`temperature_2m` and `temperature_2m_max` share a prefix, and a reader that matches
        on the prefix alone averages a daily maximum into an hourly mean."""
        payload = {"hourly": {"time": hours(1), "temperature_2m_icon_seamless": [20],
                              "precipitation_icon_seamless": [1]}}
        assert list(w.series(payload, "precipitation")) == ["icon_seamless"]

    def test_member_tags_split_into_model_and_number(self):
        assert w.split_member("member07_icon_seamless_eps") == ("icon_seamless_eps", 7)
        assert w.split_member("ecmwf_ifs025_ensemble") == ("ecmwf_ifs025_ensemble", 0)


class TestEnsembleNames:
    @pytest.mark.parametrize("tag,expected", [
        ("ecmwf_ifs025_ensemble", "ECMWF ENS"),
        ("icon_seamless_eps", "DWD ICON-EPS"),
        # The one that broke it: you ask for gfs_seamless and the series comes back tagged
        # ncep_gefs_seamless, which no prefix match on the id you sent will ever find.
        ("ncep_gefs_seamless", "NOAA GEFS"),
    ])
    def test_the_tag_a_series_carries_maps_back_to_its_centre(self, tag, expected):
        assert w.ensemble_name(tag) == expected

    def test_an_unknown_tag_is_reported_as_itself(self):
        assert w.ensemble_name("bom_access_global_ensemble") == "bom_access_global_ensemble"


class TestCoverage:
    def test_a_model_outside_its_domain_answers_nulls_and_is_dropped(self):
        """Out of domain is not a forecast of zero. Averaging the nulls in as zeroes is how a
        regional model turns a wet afternoon into half a wet afternoon."""
        assert w.covered([None, None, None], [0, 1, 2]) is False
        assert w.covered([None, 0.4, None], [0, 1, 2]) is True

    def test_totals_treat_a_missing_hour_as_no_rain_not_as_a_crash(self):
        assert w.total([0.5, None, 0.5], [0, 1, 2]) == 1.0


class TestWindows:
    def test_a_date_is_that_whole_local_day(self):
        payload = {"utc_offset_seconds": 7200}
        start, end, label = w.day_window(payload, date="2026-08-15")
        assert start == datetime(2026, 8, 15) and end == datetime(2026, 8, 16)
        assert label == "2026-08-15"

    def test_hours_wins_over_a_date_and_runs_from_the_current_hour(self):
        start, end, label = w.day_window({"utc_offset_seconds": 0}, date="2026-08-15", hours=6)
        assert end - start == timedelta(hours=6)
        assert start.minute == 0 and label == "next 6 hours"

    def test_today_means_the_rest_of_today(self):
        """Asked at four in the afternoon, 'will it rain today' is not a question about the
        morning. The window ends at the next local midnight and starts now."""
        start, end, label = w.day_window({"utc_offset_seconds": 3600})
        assert label == "rest of today"
        assert end.hour == 0 and end.date() == start.date() + timedelta(days=1)
        assert start <= w.local_now({"utc_offset_seconds": 3600})

    def test_the_index_is_the_steps_inside_the_window_only(self):
        times = hours(24)
        index = w.window_index(times, datetime(2026, 8, 13, 9), datetime(2026, 8, 13, 12))
        assert index == [9, 10, 11]

    def test_a_window_with_nothing_forecast_in_it_is_empty_rather_than_wrong(self):
        assert w.window_index(hours(24), datetime(2020, 1, 1), datetime(2020, 1, 2)) == []


class TestMaths:
    def test_percentiles_interpolate(self):
        assert w.percentile([0, 10], 0.5) == 5
        assert w.percentile([0, 1, 2, 3, 4], 0.9) == pytest.approx(3.6)
        assert w.percentile([], 0.5) is None
        assert w.percentile([7], 0.9) == 7

    @pytest.mark.parametrize("pct,word", [(0, "no"), (9, "no"), (10, "unlikely"),
                                          (29, "unlikely"), (30, "maybe"), (59, "maybe"),
                                          (60, "likely"), (84, "likely"), (85, "yes"),
                                          (100, "yes")])
    def test_the_word_matches_the_number(self, pct, word):
        assert w.verdict(pct) == word

    def test_the_rain_threshold_follows_the_unit_system(self):
        assert w.rain_threshold("metric") == 0.2
        assert w.rain_threshold("imperial") == 0.008
        assert w.rain_threshold("metric", given=1.5) == 1.5

    def test_a_unit_is_found_whatever_suffix_the_key_carries(self):
        """Asking for one model renames every key, and a plain lookup then falls back to a
        default that is silently wrong the moment someone sets imperial units."""
        payload = {"hourly_units": {"precipitation_ncep_gefs_seamless": "inch"}}
        assert w.unit_of(payload, "precipitation") == "inch"
        assert w.unit_of({"hourly_units": {}}, "precipitation", "mm") == "mm"

    def test_wmo_codes_become_words(self):
        assert w.describe(0) == "clear sky"
        assert w.describe(95) == "thunderstorm"
        assert w.describe(None) is None
        assert "7" in w.describe(7)  # unknown code, reported rather than swallowed


class TestDryWindow:
    def test_it_finds_the_longest_run_below_the_bar(self):
        hourly = [{"time": "%02d:00" % h, "chance_pct": pct} for h, pct in
                  enumerate([80, 10, 10, 90, 5, 5, 5, 5, 70])]
        assert w.dry_window(hourly) == {"from": "04:00", "to": "07:00", "hours": 4,
                                        "at_most_pct": 25}

    def test_a_single_dry_hour_is_not_a_window(self):
        hourly = [{"time": "01:00", "chance_pct": 90}, {"time": "02:00", "chance_pct": 5},
                  {"time": "03:00", "chance_pct": 90}]
        assert w.dry_window(hourly) is None

    def test_a_run_that_reaches_the_end_still_counts(self):
        hourly = [{"time": "01:00", "chance_pct": 90}, {"time": "02:00", "chance_pct": 5},
                  {"time": "03:00", "chance_pct": 5}]
        assert w.dry_window(hourly)["hours"] == 2


class TestRainOutlook:
    """The whole tool against a payload with known members, so the arithmetic is checkable by
    hand. `_fetch` is the only thing stubbed; everything after it is the real path."""

    def stub(self, monkeypatch, payload):
        monkeypatch.setattr(w, "_fetch", lambda *a, **k: payload)

    def test_the_chance_is_the_share_of_members_that_reach_the_threshold(self, monkeypatch):
        # Four ECMWF members over one hour: three wet, one dry.
        wet, dry = [1.0], [0.0]
        self.stub(monkeypatch, ensemble_payload({
            "member01_ecmwf_ifs025_ensemble": wet, "member02_ecmwf_ifs025_ensemble": wet,
            "member03_ecmwf_ifs025_ensemble": wet, "member04_ecmwf_ifs025_ensemble": dry,
        }, times=hours(1)))
        out = w.rain_outlook(48.0, 2.0, date="2026-08-13")
        assert out["chance_of_rain_pct"] == 75
        assert out["verdict"] == "likely"
        assert out["members"] == 4

    def test_each_centre_is_weighted_equally_however_many_members_it_runs(self, monkeypatch):
        """ECMWF ships 51 members and GEFS 31. Pooling them into one count quietly hands the
        answer to whoever runs the most members, so the centres are averaged instead."""
        members = {"member%02d_ecmwf_ifs025_ensemble" % i: [1.0] for i in range(1, 10)}
        members["member01_ncep_gefs_seamless"] = [0.0]
        self.stub(monkeypatch, ensemble_payload(members, times=hours(1)))
        out = w.rain_outlook(48.0, 2.0, date="2026-08-13")
        # Pooled it would be 90 %. Per centre it is the mean of 100 % and 0 %.
        assert out["chance_of_rain_pct"] == 50
        assert out["by_system"]["ECMWF ENS"]["members"] == 9
        assert out["by_system"]["NOAA GEFS"]["chance_pct"] == 0

    def test_a_centre_that_does_not_cover_the_point_is_left_out(self, monkeypatch):
        self.stub(monkeypatch, ensemble_payload({
            "member01_ecmwf_ifs025_ensemble": [1.0],
            "member01_icon_seamless_eps": [None],
        }, times=hours(1)))
        out = w.rain_outlook(48.0, 2.0, date="2026-08-13")
        assert list(out["by_system"]) == ["ECMWF ENS"]
        assert out["members"] == 1

    def test_disagreement_between_the_centres_is_said_out_loud(self, monkeypatch):
        self.stub(monkeypatch, ensemble_payload({
            "member01_ecmwf_ifs025_ensemble": [1.0],
            "member01_ncep_gefs_seamless": [0.0],
        }, times=hours(1)))
        out = w.rain_outlook(48.0, 2.0, date="2026-08-13")
        assert "disagree" in out["agreement"]

    def test_drizzle_spread_over_a_day_does_not_count_as_rain(self, monkeypatch):
        """A member with a hundredth of a millimetre every hour is numerical noise, and
        counting it turns every overcast day into a certainty of rain."""
        self.stub(monkeypatch, ensemble_payload(
            {"member01_ecmwf_ifs025_ensemble": [0.01] * 24}, times=hours(24)))
        out = w.rain_outlook(48.0, 2.0, date="2026-08-13")
        assert out["chance_of_rain_pct"] == 0
        assert out["expected"] == 0.24

    def test_a_window_the_forecast_does_not_reach_says_so(self, monkeypatch):
        self.stub(monkeypatch, ensemble_payload(
            {"member01_ecmwf_ifs025_ensemble": [1.0]}, times=hours(1)))
        out = w.rain_outlook(48.0, 2.0, date="2020-01-01")
        assert out["found"] is False
        assert "weather_history" in out["note"]
