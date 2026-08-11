# Security

This is the most personal database most people will ever own. A copy of it tells a stranger where you sleep, when the house is empty, and which building you walk into every weekday morning. Treat it accordingly.

## What is defended

**The ingest endpoint only ever inserts.** A phone carries a bearer token; that token can write positions and cannot read history back. Tokens are compared with `hmac.compare_digest` against every stored value, because `==` on a secret leaks its length and its matching prefix through timing.

**It refuses to bind a public interface without TLS in front.** `serve` binds loopback and will not listen on anything else until you pass `--insecure-ok` to say a reverse proxy is terminating TLS. A bearer token over plain HTTP is a token in every hop's logs.

**`location_sql` is read-only enforced by Postgres.** The query runs inside `set transaction read only` with a statement timeout, wrapped in a single statement, capped at a row ceiling, and rolled back at the end. Multiple statements are rejected before they reach the database. This matters because an agent will eventually be handed a query by a web page it was asked to summarize, and a guard that consists of asking a model to send only SELECT is not a guard.

**No credential is in the repo.** The connection string lives in an env file outside it, and `.gitignore` covers `.env`, keys, and Google export files.

**Third-party positions.** The people bridge stores who was on a trip. That is other people's location data. It never leaves the database, and no tool exposes it to anyone but the database's owner.

## What is not defended

- **Database access is total access.** Anyone who can reach your Postgres with the connection string reads everything. There is no per-row encryption and no second factor. Do not put this on a database you share.
- **The MCP server trusts its caller.** It speaks stdio to one agent on one machine. It has no authentication of its own, because there is no second party to authenticate. Do not expose it over a network socket.
- **A revoked token is revoked at the next request.** There is no push to the phone.
- **Nominatim sees the coordinates you geocode.** Enrichment sends a lat/lon to a third party to get an address back. If that is unacceptable for a place, do not geocode it; nothing else in the archive leaves the machine.
- **No auditing.** Nothing records who read what.

## Reporting

Open an issue for anything that is not itself sensitive. For a vulnerability that would expose data, use GitHub's private vulnerability reporting on this repository rather than a public issue.
