# Repository instructions

- Never commit a real position, address, place name, token or connection string. Tests use synthetic coordinates; the two in the fixtures are city centres, not anyone's home.
- A migration reviewed by reading is a guess. Every schema change runs against a real Postgres before it lands: `docker run -d --name location-test -e POSTGRES_PASSWORD=test -p 5433:5432 postgres:16`, then `LOCATION_TEST_DATABASE_URL=... pytest`.
- Migrations are additive and idempotent. `create ... if not exists` and `create or replace`, because upgrading an archive someone already filled is the normal case.
- The core schema never references `people`. That join lives in `0002_people_bridge.sql`, which refuses to install without one.
- A duration is measured, never modelled. If a change makes the code invent a boundary between two fixes, it is the wrong change.
- "No record" is not "was not there". Anything that returns empty points at `location_coverage`, and the gaps it reports are computed from the data rather than written down.
- `location_sql` stays read-only enforced by Postgres, not by prompting. Do not add a path that runs caller SQL outside the read-only transaction.
- A chance of rain is counted over ensemble members, never lifted off a single deterministic run, and the weather panel holds one model per independent centre. Adding a regional model that falls back to someone else's global run makes the panel report agreement it has not got.
- psycopg is the only runtime dependency. The geocoder, the HTTP endpoint and the JSON-RPC server are standard library and stay that way.
- Run `ruff check .` and `pytest` after Python changes.
