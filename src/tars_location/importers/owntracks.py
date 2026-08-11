"""The live half: an HTTP endpoint an OwnTracks phone posts to.

The export gives you the past. This gives you today, which is the half people actually ask
about. OwnTracks in HTTP mode posts a small JSON object on a schedule or on movement, and
this turns it into a ping.

Deliberately the standard library and nothing else. A location endpoint is the most sensitive
thing in this project: it is the one door open to the internet, and every dependency behind it
is something else that can have a CVE. There is no framework here to have one.

Three rules it enforces, and none of them are optional:

* **A bearer token from `location_auth`, compared in constant time.** No token, no write. A
  location feed with an open endpoint is a stalking tool with your name on it.
* **HTTPS is your job, not this file's.** Put it behind a reverse proxy that terminates TLS.
  It refuses to start on a non-loopback address unless you say `--i-terminate-tls-elsewhere`,
  because a bearer token over plain HTTP is a token in every hop's logs.
* **It only ever inserts.** Nothing here reads the archive back out, so the credential a
  phone carries cannot be used to ask where you have been.

    python3 -m tars_location.cli serve --port 8080

OwnTracks: Preferences > Connection > Mode "HTTP", URL your endpoint, and under Authentication
put the token in the Password field with any username, or send it as an `Authorization: Bearer`
header from your proxy.
"""

from __future__ import annotations

import base64
import hmac
import json
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import core

MAX_BODY = 64 * 1024


def known_tokens() -> list[str]:
    return [r["token"] for r in core.db().fetch_all("select token from location_auth")]


def token_ok(presented: str) -> bool:
    """Constant-time against every stored token.

    `==` on a secret leaks its length and its matching prefix through timing. That is a real
    attack on a long-lived bearer token, and `compare_digest` costs nothing to use.
    """
    if not presented:
        return False
    return any(hmac.compare_digest(presented, known) for known in known_tokens())


def presented_token(headers) -> str:
    auth = headers.get("Authorization") or ""
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    if auth.startswith("Basic "):
        # OwnTracks sends its credentials as HTTP basic auth. The username is whatever the
        # phone is called; the password is the token.
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8", "replace")
        except Exception:
            return ""
        return decoded.partition(":")[2]
    return ""


def store(payload: dict, token: str) -> bool:
    """One OwnTracks report into `location_pings`. Returns whether anything was written."""
    if (payload.get("_type") or "location") != "location":
        return False           # transitions, waypoints, and other chatter
    lat, lon = payload.get("lat"), payload.get("lon")
    if lat is None or lon is None:
        return False
    when = payload.get("tst")
    captured = (datetime.fromtimestamp(float(when), timezone.utc) if when
                else datetime.now(timezone.utc))
    core.execute(
        "insert into location_pings (captured_at, lat, lon, accuracy_m, source, token) "
        "values (%s, %s, %s, %s, %s, %s)",
        (captured, float(lat), float(lon), payload.get("acc"), "owntracks", token))
    if payload.get("alt") is not None or payload.get("vel") is not None:
        core.execute(
            "insert into location_raw_positions "
            "(captured_at, lat, lon, accuracy_m, altitude_m, speed_ms, source) "
            "values (%s, %s, %s, %s, %s, %s, 'owntracks') on conflict do nothing",
            (captured, float(lat), float(lon), payload.get("acc"),
             payload.get("alt"),
             # OwnTracks reports velocity in km/h; everything downstream is metres per second.
             (float(payload["vel"]) / 3.6) if payload.get("vel") is not None else None))
    return True


class Handler(BaseHTTPRequestHandler):
    server_version = "tars-location"
    sys_version = ""

    def do_POST(self):  # noqa: N802
        token = presented_token(self.headers)
        if not token_ok(token):
            # 401 with nothing else. Saying which part was wrong helps only the caller who
            # should not be here.
            return self._json(401, {"error": "unauthorized"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(400, {"error": "bad length"})
        if length <= 0 or length > MAX_BODY:
            return self._json(413, {"error": "body too large"})
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return self._json(400, {"error": "bad json"})
        if not isinstance(payload, dict):
            return self._json(400, {"error": "expected an object"})
        try:
            store(payload, token)
        except Exception as err:
            print("tars-location ingest: %s" % err, file=sys.stderr)
            return self._json(500, {"error": "write failed"})
        # OwnTracks expects a JSON array back and treats anything else as a failure to
        # deliver, which makes it retry the same point forever.
        return self._json(200, [])

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            return self._json(200, {"ok": True})
        return self._json(404, {"error": "not found"})

    def _json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # The default logs the full request line to stdout. Coordinates never belong in a log
        # file, and stdout may be a protocol stream elsewhere in this package.
        print("%s - %s" % (self.address_string(), fmt % args), file=sys.stderr)


def serve(host: str = "127.0.0.1", port: int = 8080, insecure_ok: bool = False) -> None:
    if host not in ("127.0.0.1", "localhost", "::1") and not insecure_ok:
        raise SystemExit(
            f"refusing to listen on {host} without TLS in front of it. A bearer token over "
            "plain HTTP is a token in every hop's logs. Put it behind a reverse proxy and "
            "bind 127.0.0.1, or pass --i-terminate-tls-elsewhere if the proxy is already "
            "there.")
    if not known_tokens():
        raise SystemExit(
            "no token in location_auth, so nothing could ever authenticate. Add one:\n"
            "  python3 -m tars_location.cli token add --label pixel")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"listening on http://{host}:{port}", file=sys.stderr)
    server.serve_forever()
