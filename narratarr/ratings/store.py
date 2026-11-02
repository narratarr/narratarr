"""Per-instance sqlite cache for ratings.

Two things are cached, and the distinction matters:

* what a title resolved to (its ISBN and Goodreads id), so a book is never resolved twice;
* the rating that came back, so the page renders from disk and never from the network.

A miss is recorded too. Without that, an unmatched book is retried on every search and the
one-request-per-second budget is spent re-failing the same titles.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

# A miss is worth retrying eventually — a book can appear on Goodreads later — but not often.
RETRY_AFTER = 7 * 24 * 3600


def key_for(title: str, author: str) -> str:
    """The cache key: a title and author normalised just enough that punctuation and case don't
    produce a second row for the same book."""
    def norm(s: str) -> str:
        return " ".join((s or "").lower().replace("’", "'").split())

    return f"{norm(title)}|{norm(author)}"


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._local = threading.local()
        self._ready = False

    def _con(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(str(self.path), timeout=10)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA journal_mode=WAL")
            self._local.con = con
        return con

    def _init(self) -> None:
        if self._ready:
            return
        with self._lock:
            con = self._con()
            con.execute(
                """CREATE TABLE IF NOT EXISTS rating (
                       key          TEXT,
                       provider     TEXT NOT NULL,
                       state        TEXT NOT NULL,   -- 'rated' | 'unrated'
                       isbn         TEXT NOT NULL DEFAULT '',
                       remote_id    TEXT NOT NULL DEFAULT '',
                       score        REAL,
                       count        INTEGER,
                       url          TEXT NOT NULL DEFAULT '',
                       checked_at   REAL NOT NULL,
                       PRIMARY KEY (key, provider)
                   )"""
            )
            # The resolver's own table: a cache row can exist for the resolver before any rating.
            con.execute(
                """CREATE TABLE IF NOT EXISTS resolved (
                       key        TEXT PRIMARY KEY,
                       isbn       TEXT NOT NULL DEFAULT '',
                       remote_id  TEXT NOT NULL DEFAULT '',
                       checked_at REAL NOT NULL
                   )"""
            )
            con.commit()
        self._ready = True

    # ---- ratings ---------------------------------------------------------------------------

    def get(self, key: str, provider: str, ttl: float | None = None) -> dict | None:
        """The cached rating, or None. Never touches the network.

        `ttl` expires a rating that was found (a score does drift); a remembered miss always uses
        `RETRY_AFTER`, so a book that had no rating is looked at again sooner than one that did.
        """
        self._init()
        row = self._con().execute(
            "SELECT * FROM rating WHERE key = ? AND provider = ?", (key, provider)
        ).fetchone()
        if row is None:
            return None
        age = time.time() - row["checked_at"]
        if row["state"] == "unrated":
            return {"state": "unrated"} if age < RETRY_AFTER else None
        if ttl is not None and age > ttl:
            return None
        return dict(row)

    def put(self, key: str, provider: str, **fields) -> None:
        self._init()
        with self._lock:
            con = self._con()
            con.execute(
                """INSERT INTO rating (key, provider, state, isbn, remote_id, score, count, url, checked_at)
                   VALUES (:key, :provider, :state, :isbn, :remote_id, :score, :count, :url, :checked_at)
                   ON CONFLICT(key, provider) DO UPDATE SET
                       state=excluded.state, isbn=excluded.isbn, remote_id=excluded.remote_id,
                       score=excluded.score, count=excluded.count, url=excluded.url,
                       checked_at=excluded.checked_at""",
                {
                    "key": key,
                    "provider": provider,
                    "state": fields.get("state", "rated"),
                    "isbn": fields.get("isbn") or "",
                    "remote_id": fields.get("remote_id") or "",
                    "score": fields.get("score"),
                    "count": fields.get("count"),
                    "url": fields.get("url") or "",
                    "checked_at": time.time(),
                },
            )
            con.commit()

    def count(self, provider: str) -> dict:
        self._init()
        rows = self._con().execute(
            "SELECT state, COUNT(*) AS n FROM rating WHERE provider = ? GROUP BY state", (provider,)
        ).fetchall()
        return {r["state"]: r["n"] for r in rows}

    # ---- resolver --------------------------------------------------------------------------

    def get_resolved(self, key: str) -> dict | None:
        self._init()
        row = self._con().execute("SELECT * FROM resolved WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        if not (row["isbn"] or row["remote_id"]) and time.time() - row["checked_at"] < RETRY_AFTER:
            return {"isbn": "", "remote_id": ""}  # a recent dead end
        return dict(row)

    def put_resolved(self, key: str, isbn: str = "", remote_id: str = "") -> None:
        self._init()
        with self._lock:
            con = self._con()
            con.execute(
                """INSERT INTO resolved (key, isbn, remote_id, checked_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET isbn=excluded.isbn, remote_id=excluded.remote_id,
                                                  checked_at=excluded.checked_at""",
                (key, isbn or "", remote_id or "", time.time()),
            )
            con.commit()
