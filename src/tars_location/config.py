"""Where the database is, and the handful of things you are allowed to tune.

Everything comes from the environment or from an env file, and nothing is baked into the
code. That is not a style preference: a location archive is the most personal database a
person is likely to own, and a connection string committed once is a connection string
forever.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_FILE_VAR = "LOCATION_ENV_FILE"
DEFAULT_ENV_FILE = "~/.config/tars-location/.env"


def load_env_file(path: str | None = None) -> None:
    """Read KEY=VALUE lines into the environment, without overwriting what is already set.

    A real environment variable always wins over the file, so a container, a systemd unit
    or a one-off `LOCATION_DATABASE_URL=... command` does not have to fight a stale file.
    """
    target = Path(path or os.environ.get(ENV_FILE_VAR) or DEFAULT_ENV_FILE).expanduser()
    if not target.is_file():
        return
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    database_url: str
    # Nominatim's usage policy asks for a real contact address in the User-Agent so they can
    # reach whoever is hammering them. Without one you are an anonymous scraper and they are
    # entitled to block you. This has no default on purpose.
    geocoder_contact: str
    # Where you are when the archive cannot say. It only decides what "yesterday" means on a
    # database with no fixes yet, and UTC is the honest answer until there is one.
    fallback_tz: str
    # A phone at rest wanders a few metres, so anything inside this radius is the same spot
    # and reuses the place already resolved for it. Much wider merges a house with its
    # neighbour, and the street-level address then belongs to the wrong one.
    place_radius_m: float
    # Nominatim allows one request per second, absolute. This is the pause after each call.
    geocode_sleep_s: float
    read_only_row_cap: int
    # Weather. Open-Meteo is free for non-commercial use and needs no key, so these three
    # only ever have to be touched by someone who wants Fahrenheit or has bought a plan.
    weather_units: str
    weather_timeout_s: float
    weather_api_key: str

    @classmethod
    def load(cls, env_file: str | None = None) -> Settings:
        load_env_file(env_file)
        url = os.environ.get("LOCATION_DATABASE_URL", "").strip()
        if not url:
            raise RuntimeError(
                "LOCATION_DATABASE_URL is not set. Put it in "
                f"{os.environ.get(ENV_FILE_VAR) or DEFAULT_ENV_FILE} or in the environment. "
                "See .env.example.")
        contact = os.environ.get("LOCATION_GEOCODER_CONTACT", "").strip()
        return cls(
            database_url=url,
            geocoder_contact=contact,
            fallback_tz=os.environ.get("LOCATION_FALLBACK_TZ", "UTC").strip() or "UTC",
            place_radius_m=float(os.environ.get("LOCATION_PLACE_RADIUS_M", "40")),
            geocode_sleep_s=float(os.environ.get("LOCATION_GEOCODE_SLEEP_S", "1.1")),
            read_only_row_cap=int(os.environ.get("LOCATION_READ_ONLY_ROW_CAP", "2000")),
            weather_units=(os.environ.get("LOCATION_WEATHER_UNITS", "metric").strip().lower()
                           or "metric"),
            weather_timeout_s=float(os.environ.get("LOCATION_WEATHER_TIMEOUT_S", "15")),
            weather_api_key=os.environ.get("LOCATION_WEATHER_API_KEY", "").strip(),
        )

    def user_agent(self) -> str:
        if not self.geocoder_contact:
            raise RuntimeError(
                "LOCATION_GEOCODER_CONTACT is not set, so this would call Nominatim "
                "anonymously. Their usage policy asks for an address they can reach you at. "
                "Set it to your email or the URL of your project.")
        return f"tars-location-mcp (+{self.geocoder_contact})"
