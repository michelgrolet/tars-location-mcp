"""The command line. You are not really meant to type it; your agent is.

    tars-location migrate                  create or update the schema
    tars-location status                   what the archive holds
    tars-location import <path>            a Google Timeline export
    tars-location enrich [--geocode N]     give unresolved points an address
    tars-location detect-trips             find trips in the days already imported
    tars-location serve [--port 8080]      the live ingest endpoint
    tars-location token add|list|revoke    credentials for that endpoint
    tars-location mcp                      the MCP server on stdin/stdout
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path

from . import core
from .config import Settings

# Inside the package, so an installed wheel carries them and `uvx` works with no checkout.
MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def cmd_migrate(args) -> int:
    """Run the SQL files in order. Every one is idempotent, so this is safe to repeat."""
    names = ["0001_core.sql"]
    if args.with_people_bridge:
        names.append("0002_people_bridge.sql")
    with core.db().connection() as conn:
        for name in names:
            path = MIGRATIONS / name
            if not path.is_file():
                raise SystemExit(f"missing migration: {path}")
            conn.execute(path.read_text(encoding="utf-8"))
            print(f"applied {name}")
    return 0


def cmd_status(args) -> int:
    have = core.coverage() or {}
    print(json.dumps({k: core.iso(v) for k, v in have.items()}, indent=1))
    holes = core.gaps()
    if holes:
        print("\ngaps of two weeks or more:")
        for hole in holes:
            print(f"  {hole['from']} -> {hole['to']}  ({hole['missing_days']} days)")
    return 0


def cmd_import(args) -> int:
    from .importers import google_takeout
    google_takeout.run(args.path, dry_run=args.dry_run, no_raw=args.no_raw,
                       geocode=args.geocode)
    return 0


def cmd_enrich(args) -> int:
    resolved, geocoded = core.enrich(budget=args.geocode)
    print(f"resolved {resolved} points with {geocoded} geocodes")
    left = core.db().fetch_one(
        "select count(*) as n from location_pings where place_id is null")["n"]
    if left:
        print(f"{left} still unresolved. At one geocode a second this takes a while; "
              f"run it on a timer rather than in one burst.")
    return 0


def cmd_detect_trips(args) -> int:
    row = core.db().fetch_one("select * from location_detect_trips(%s)", (args.min_nights,))
    print(f"{row['inserted']} new trips, {row['updated']} updated")
    return 0


def cmd_refresh(args) -> int:
    core.execute("select location_refresh_days()")
    print("day grid refreshed")
    return 0


def cmd_serve(args) -> int:
    from .importers import owntracks
    owntracks.serve(host=args.host, port=args.port,
                    insecure_ok=args.i_terminate_tls_elsewhere)
    return 0


def cmd_token(args) -> int:
    if args.action == "add":
        token = secrets.token_urlsafe(32)
        core.execute("insert into location_auth (token, label) values (%s, %s)",
                     (token, args.label))
        # Printed once, on stdout, and never retrievable in full again.
        print(token)
        return 0
    if args.action == "list":
        for row in core.db().fetch_all(
                "select label, created_at, left(token, 6) as head from location_auth "
                "order by created_at"):
            print(f"{row['head']}...  {row['label'] or '(no label)'}  {row['created_at']}")
        return 0
    if args.action == "revoke":
        if not args.label:
            raise SystemExit("which one? pass --label")
        core.execute("delete from location_auth where label = %s", (args.label,))
        print(f"revoked {args.label}")
        return 0
    raise SystemExit("unknown action")


def cmd_mcp(args) -> int:
    from . import server
    server.main()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tars-location", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", help="where LOCATION_DATABASE_URL lives")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("migrate", help="create or update the schema")
    p.add_argument("--with-people-bridge", action="store_true",
                   help="also link trips to a people graph in the same database")
    p.set_defaults(run=cmd_migrate)

    p = sub.add_parser("status", help="what the archive holds, and where the gaps are")
    p.set_defaults(run=cmd_status)

    p = sub.add_parser("import", help="a Google Timeline export, file or directory")
    p.add_argument("path")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-raw", action="store_true",
                   help="skip the raw fix log, which is most of the volume")
    p.add_argument("--geocode", type=int, default=0,
                   help="resolve up to N places now, at one per second")
    p.set_defaults(run=cmd_import)

    p = sub.add_parser("enrich", help="give unresolved points an address")
    p.add_argument("--geocode", type=int, default=25)
    p.set_defaults(run=cmd_enrich)

    p = sub.add_parser("detect-trips", help="find trips in the days already imported")
    p.add_argument("--min-nights", type=int, default=1)
    p.set_defaults(run=cmd_detect_trips)

    p = sub.add_parser("refresh", help="rebuild the day grid")
    p.set_defaults(run=cmd_refresh)

    p = sub.add_parser("serve", help="the live ingest endpoint")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--i-terminate-tls-elsewhere", action="store_true",
                   help="you have a reverse proxy doing TLS in front of this")
    p.set_defaults(run=cmd_serve)

    p = sub.add_parser("token", help="credentials for the ingest endpoint")
    p.add_argument("action", choices=["add", "list", "revoke"])
    p.add_argument("--label", help="which device this is")
    p.set_defaults(run=cmd_token)

    p = sub.add_parser("mcp", help="the MCP server, stdio")
    p.set_defaults(run=cmd_mcp)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    core.use(Settings.load(args.env_file))
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
