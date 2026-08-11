---
name: setup-location-memory
description: Install or repair the location archive. Use when the location tools are missing, when they error on the database, when the user wants to import a Google Timeline export, when they want to start a live feed from OwnTracks, or when they ask to set up location memory.
---

# Set up location memory

Four things have to be true: a Postgres to write to, the schema in it, an env file the server can find, and the MCP server registered with the agent. Check them in that order, because each one's error looks like the one before it.

## 1. Where the config lives

```bash
cat ~/.config/tars-location/.env
```

`LOCATION_DATABASE_URL` is required. `LOCATION_GEOCODER_CONTACT` has no default and nothing geocodes without it: Nominatim's usage policy asks for an address they can reach you at, and an anonymous scraper gets blocked. The rest have defaults, listed in `.env.example`.

If there is no database yet, a container on the same machine is enough:

```bash
docker run -d --name location-db -e POSTGRES_PASSWORD=local -p 5432:5432 postgres:16
```

## 2. The schema

```bash
tars-location migrate
tars-location status
```

`migrate` is idempotent, so running it again after an upgrade is the normal case. `status` prints what the archive holds per source and every gap it knows about; run it after every step below and read the counts rather than assuming.

## 3. Fill it

**The past.** Google keeps Timeline on the phone now, so a Takeout gives you settings only. The export lives at **Settings > Location > Location services > Timeline > Export Timeline data**, and produces one JSON file.

```bash
tars-location import ~/Downloads/location-history.json --geocode 200
tars-location detect-trips
```

Four export shapes are handled and every insert is `on conflict do nothing`, so re-importing an overlapping export is safe and adds only what is new. Geocoding is deliberately separate from the insert, because Nominatim allows one call a second and an export brings a thousand new places. Drain the rest on a timer:

```bash
tars-location enrich --geocode 25
```

**The present.** OwnTracks, in HTTP mode, with the token as the password under Authentication:

```bash
tars-location token add --label pixel
tars-location serve --port 8080
```

The endpoint refuses to bind anything but loopback unless you tell it there is TLS in front, because a bearer token over plain HTTP is a token in every hop's logs. Put a reverse proxy there before exposing it.

## 4. Register the server

```bash
claude mcp add -s user location \
  -e LOCATION_ENV_FILE="$HOME/.config/tars-location/.env" \
  -- uvx --from git+https://github.com/michelgrolet/tars-location-mcp tars-location-mcp
```

Start a new session afterwards; a running one will not see it.

## When it errors

- **"LOCATION_DATABASE_URL is not set"** — the server cannot find the env file. It is looked up at `LOCATION_ENV_FILE`, then `~/.config/tars-location/.env`. A real environment variable beats both.
- **"relation location_v_stays does not exist"** — the schema is not applied. Run `migrate` against the database the URL actually points at, which is often not the one you meant.
- **`who_was_there` says no people bridge** — that is the designed answer, not a fault. `tars-location migrate --with-people-bridge` links trips to a people graph, and refuses to run unless a `people` table already exists.
- **Places with no city or country** — nothing has geocoded them yet. `tars-location enrich --geocode 50`, and check `LOCATION_GEOCODER_CONTACT` is set.

Never print a token or a connection string back to the user in full.
