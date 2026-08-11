# Location Memory

**Where you were, when, for how long, and who with.** A private location archive with an MCP server on top, so your agent can answer questions about your own past instead of asking you.

Your data stays in your Postgres. No hosted service, no telemetry, no account.

```
you: which cities did I go to last spring, and how long in each?

    cities_visited(period: "last_90_days")

    Lisbon        11 days   3 separate times   142h
    Porto          4 days   1 time              61h
    Paris         38 days                      —  home
```

[![ci](https://github.com/michelgrolet/tars-location-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/michelgrolet/tars-location-mcp/actions/workflows/ci.yml)

## Why this is not a table of coordinates

A position log answers "where was I at 14:03". Nobody asks that. People ask *how long were we in Lisbon*, *how many times have I been to Japan*, *what did I do on the 8th*, *how far did I fly last year*. Those are questions about **stays** and **journeys**, and you cannot recover either one from a stream of points without guessing.

So the archive keeps what the phone already knew and a geocoder can never reconstruct:

| What | Where it comes from | Why it cannot be recomputed |
|---|---|---|
| A stay, with a real start and end | the export | interpolating between fixes invents the boundaries |
| Transport mode | the phone's sensors | a straight line between two points does not say "flying" |
| Distance travelled | the export | the route is not the great circle |
| Which place is home | the export's own profile | every "far from home" number depends on it |
| Altitude and speed | the raw fix log | not derivable from lat/lon at all |

`location_v_stays` is the single spine everything reads: imported stays, followed by the live feed's tail after the last import, deduplicated at the seam.

## The two things it refuses to get wrong

**A duration is measured, never modelled.** Start and end come from the source. The one place a duration is inferred, a run of live pings in the same place, is capped at 72 hours, because a phone that goes quiet for a week did not stand still for a week.

**"No record" is not "was not there".** An archive built from an export plus a live feed has holes: the months between the day you exported and the day you installed the tracker, a phone you replaced, an app you uninstalled. `location_coverage` reports every gap of two weeks or more, **measured from the data** rather than written down, and every tool that comes back empty points at it. An agent that does not know where the holes are will tell you confidently that you have never been to Portugal.

## Install

```bash
git clone https://github.com/michelgrolet/tars-location-mcp.git
cd tars-location-mcp
pip install -e .
```

You need a Postgres. A container on the same laptop is fine, a free hosted project is fine:

```bash
docker run -d --name location-db -e POSTGRES_PASSWORD=local -p 5432:5432 postgres:16
```

```bash
mkdir -p ~/.config/tars-location
cat > ~/.config/tars-location/.env <<'EOF'
LOCATION_DATABASE_URL=postgresql://postgres:local@127.0.0.1:5432/postgres
LOCATION_GEOCODER_CONTACT=you@example.com
LOCATION_FALLBACK_TZ=Europe/Paris
EOF

tars-location migrate
```

`LOCATION_GEOCODER_CONTACT` has no default on purpose. Nominatim's usage policy asks for an address they can reach you at; without one you are an anonymous scraper and they are within their rights to block you.

## Fill it

### The past: a Google Timeline export

Google keeps Timeline on the phone now, so a Takeout of it holds settings only. Export from the device: **Settings > Location > Location services > Timeline > Export Timeline data**. That gives you a JSON file.

```bash
tars-location import ~/Downloads/location-history.json --geocode 200
tars-location detect-trips
tars-location status
```

Four export shapes are handled, because which one you get depends on the phone and on which door you exported through: the on-device `semanticSegments`, the same segments as a bare list, classic Takeout `timelineObjects`, and classic `locations` records. Re-running is the normal case: every export overlaps the last, every insert is `on conflict do nothing`, and a second pass adds only what is new.

Geocoding is deliberately not part of the insert. Nominatim allows one call a second and an export brings a thousand new places, so `--geocode N` drains N of them and the rest happen in the background:

```bash
tars-location enrich --geocode 25     # on a timer, every few minutes
```

### The present: OwnTracks

```bash
tars-location token add --label pixel     # prints the token once
tars-location serve --port 8080
```

Point OwnTracks at it in HTTP mode, with the token as the password under Authentication. Put a reverse proxy doing TLS in front: the endpoint refuses to bind anything but loopback until you tell it one is there, because a bearer token over plain HTTP is a token in every hop's logs.

## Connect an agent

```bash
# Codex
codex mcp add location \
  --env LOCATION_ENV_FILE="$HOME/.config/tars-location/.env" \
  -- uvx --from git+https://github.com/michelgrolet/tars-location-mcp tars-location-mcp

# Claude Code
claude mcp add -s user location \
  -e LOCATION_ENV_FILE="$HOME/.config/tars-location/.env" \
  -- uvx --from git+https://github.com/michelgrolet/tars-location-mcp tars-location-mcp
```

Anything that speaks MCP over stdio works. Start a new session after adding the server.

### With TARS, optionally

Location Memory is standalone and stays standalone: nothing above needs a particular harness. [TARS](https://github.com/michelgrolet/tars) is a harness for a personal agent that lists this in its extension registry, so if you happen to run it, one command does the clone and the wiring:

```bash
claude plugin install location-memory@tars
```

What that adds over the plain MCP server is *when* the tools fire: TARS puts the trigger in the one file it loads every session, so the agent checks where you were before answering rather than waiting to be told to.

## The tools

| Tool | What it answers |
|---|---|
| `current_location` | where you are now, which trip, day N of it, how far from home, and whether the fix is stale |
| `stays` | every stop over a window, in order, with duration and trip |
| `cities_visited` | cities over a window: time in each, how many separate times, which days |
| `countries_visited` | the same by country, with the cities inside each |
| `top_places` | where you actually spend time, most first. Also how you find an address you half remember |
| `trips` | trips newest first, with who was along |
| `trip` | one trip in full: day by day, every stay, every journey with mode and distance |
| `day` | one date end to end |
| `travel_stats` | kilometres by mode, flights, days away against days at home |
| `records` | highest, fastest, farthest, the four compass extremes, longest flight, most cities in a day |
| `home` | where you have lived and when |
| `location_coverage` | what the archive holds, per source, and every gap |
| `who_was_there` | trips shared with a person, both directions. Needs the people bridge |
| `location_sql` | read-only SELECT for anything the rest does not shape |

Every windowed answer carries the period and timezone it used, and the number of stays it looked at. An answer that does not say what it covers is not an answer.

## Trips are detected, not entered

A trip is a run of days spent outside the country you live in, or more than 100 km from home. That definition needs to know where home was *at the time*, which is why home is a timeline rather than a setting: anyone who has moved has several, and measuring a 2019 day against a 2025 address gets every distance wrong.

```bash
tars-location detect-trips
```

Runs are broken by a gap of more than a month, so a hole in the archive does not weld two visits into one four-year trip. **A name you typed yourself is never overwritten**: `name_is_auto` goes false the moment you rename a trip, and re-detection leaves it alone. Without that flag every re-run silently renames "Honeymoon" back to "Italy - June 2025".

## People, optionally

"Who was I with in Lisbon" is the question a location archive cannot answer alone, and the one people actually ask. It needs a table of people, which is a different product with a different lifecycle, so it is a bridge and not a dependency:

```bash
tars-location migrate --with-people-bridge
```

It refuses to run unless a `people` table exists, and the core schema never references one. Built against [people-memory](https://github.com/michelgrolet/people-memory-mcp); any table with `id`, `full_name`, `current_org` and `current_role` works. Without the bridge, `who_was_there` says so plainly rather than returning a database error for an agent to misread.

## Security

This is the most personal database most people will ever own. Someone with a copy knows where you sleep.

- **The ingest endpoint needs a bearer token**, compared in constant time against `location_auth`, and it only ever inserts. The credential a phone carries cannot be used to read your history back out.
- **It refuses to bind a public interface without TLS in front**, because a bearer token over plain HTTP is a token in every hop's logs.
- **`location_sql` is read-only enforced by Postgres**, not by asking a model nicely: a `set transaction read only` block with a statement timeout, rolled back at the end. An agent will eventually be handed a query by a web page it was summarizing, and the guard has to hold when it is.
- **No credential is ever in the repo.** The connection string lives in an env file outside it.

Full threat model, including what is *not* defended: [SECURITY.md](SECURITY.md).

## Tests

```bash
pytest
```

The parsers, the time maths and the guards run with no database. The schema tests need a real Postgres, because a migration reviewed by reading is a guess:

```bash
docker run -d --name location-test -e POSTGRES_PASSWORD=test -p 5433:5432 postgres:16
LOCATION_TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:5433/postgres pytest
```

CI runs both against Postgres 14 and 16, on Python 3.10 and 3.13.

## Requirements

Python 3.10+, Postgres 13+, and `psycopg`. Nothing else: the geocoder, the HTTP endpoint and the JSON-RPC server are all standard library. A location archive is the wrong place to carry a dependency tree.

## Not here yet

Polarsteps import, and a browser map over `location_v_stays`. Both exist in a private tree and are not extracted.

---

MIT.
