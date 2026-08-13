# Changelog

## Unreleased

- Weather, at the coordinates the archive already holds. Five tools, all of which default to
  where you are and take a `place` or a `lat`/`lon` pair instead: `will_it_rain`,
  `weather_now`, `weather_forecast`, `weather_models`, `weather_history`.
- A chance of rain is counted over about 120 ensemble members from ECMWF, DWD and NOAA rather
  than read off one forecast, with each centre weighted equally so the one running the most
  members does not decide the answer on its own. Their disagreement is reported instead of
  averaged away.
- `weather_models` puts seven independent national services side by side with their spread,
  and reports a model that does not cover the point as not covering it rather than folding
  its nulls into the mean as zeroes.
- `weather_history` reads ERA5 through the place you spent that day, so a past-weather
  question needs only a date. It says so plainly when the archive has no record of the day
  and the answer therefore describes where you are now.
- Open-Meteo throughout: free, no key, no new dependency. `LOCATION_WEATHER_UNITS`,
  `LOCATION_WEATHER_TIMEOUT_S` and `LOCATION_WEATHER_API_KEY` are the only new settings, and
  none of them has to be set.

## 0.1.0

First public release. Extracted from a private archive that had been running for months, with everything personal removed and everything that was hardcoded now measured.

- Postgres schema for stays, journeys, places, raw fixes, trips and home periods, applied by `tars-location migrate`. Additive and idempotent.
- `location_v_stays`, the single spine: imported stays followed by the live feed's tail after the last import, deduplicated at the seam.
- Home as a timeline rather than a setting, so a 2019 day is measured against the 2019 address.
- Trip detection from runs of days outside the home country or beyond 100 km, broken by gaps of more than a month. A name you typed yourself is never overwritten.
- Google Timeline importer covering four export shapes: on-device `semanticSegments`, the same segments as a bare list, classic Takeout `timelineObjects`, and classic `locations` records.
- OwnTracks ingest endpoint, standard library only, bearer token compared in constant time, loopback unless told there is TLS in front.
- MCP server with 14 tools, including `location_coverage`, which reports every gap of two weeks or more computed from the data rather than written down.
- `location_sql`, read-only enforced by Postgres: read-only transaction, statement timeout, single statement, row cap, rolled back.
- Optional people bridge in `0002_people_bridge.sql`, which refuses to install without a `people` table. The core schema never references one.
- 72 tests. The parsers, time maths and guards run with no database; the schema tests run against a real Postgres, because a migration reviewed by reading is a guess.
