"""One connection to one Postgres, and a read-only door for the SQL tool.

The read-only path is the part worth reading. An agent given a `location_sql` tool will
eventually be handed a query by something that is not its human: a web page it summarized, a
document it was asked about, a message it received. So the guard is not "the model promised
to only write SELECT". It is a transaction the database itself refuses to let write.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .config import Settings

# Semicolons inside string literals are legitimate, so this only refuses a semicolon that
# actually separates two statements. The real defence is the read-only transaction below;
# this one exists so a mistake gets a clear message instead of a silent single-statement run.
_STATEMENT_BREAK = re.compile(r";\s*\S")


class Database:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._conn: psycopg.Connection | None = None

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection]:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.settings.database_url, row_factory=dict_row)
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def fetch_all(self, sql: str, params: Sequence[Any] | None = None) -> list[dict]:
        with self.connection() as conn:
            return conn.execute(sql, tuple(params or ())).fetchall()

    def fetch_one(self, sql: str, params: Sequence[Any] | None = None) -> dict | None:
        rows = self.fetch_all(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> None:
        """A write with nothing to read back. `fetch_all` would raise here, since psycopg
        refuses to fetch from a statement that produced no rows."""
        with self.connection() as conn:
            conn.execute(sql, tuple(params or ()))

    def fetch_read_only(self, query: str, limit: int) -> list[dict]:
        """Run one caller-supplied SELECT with the database enforcing the read-only part.

        `set transaction read only` makes Postgres reject INSERT, UPDATE, DELETE, DDL and
        anything else that writes, whatever the text looks like. A statement timeout keeps a
        cartesian join from pinning a core, and the rollback at the end means even a
        successful write through some path nobody thought of does not survive.
        """
        text = query.strip().rstrip(";").strip()
        if not text:
            raise ValueError("empty query")
        if _STATEMENT_BREAK.search(text):
            raise ValueError("one statement at a time")
        head = text.lstrip("( \n\t").lower()
        if not (head.startswith("select") or head.startswith("with")):
            raise ValueError("only SELECT is allowed here")
        capped = min(int(limit), self.settings.read_only_row_cap)
        with self.connection() as conn:
            try:
                with conn.transaction():
                    conn.execute("set transaction read only")
                    conn.execute("set local statement_timeout = '15s'")
                    rows = conn.execute(
                        f"select * from ({text}) as guarded limit %s", (capped,)).fetchall()
                    # Rolling back is belt and braces on top of the read-only transaction:
                    # whatever the query turned out to be, nothing it did outlives this block.
                    raise _Done(rows)
            except _Done as done:
                return done.rows

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None


class _Done(Exception):
    """Carries the rows out of a transaction that is then rolled back."""

    def __init__(self, rows: list[dict]) -> None:
        super().__init__("done")
        self.rows = rows
