# Changelog

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
