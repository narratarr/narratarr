"""SQLite storage for the user's library."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from . import config

# Lifecycle of a book:
#   wanted -> queued -> resolving -> caching -> downloading -> organizing -> completed
#                                     any step can land in -> failed
STATUSES = ("wanted", "queued", "resolving", "caching", "downloading", "organizing", "completed", "failed")
ACTIVE = ("queued", "resolving", "caching", "downloading", "organizing")

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    source_url    TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    author        TEXT NOT NULL DEFAULT '',
    narrators     TEXT NOT NULL DEFAULT '[]',
    cover_url     TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    language      TEXT NOT NULL DEFAULT '',
    categories    TEXT NOT NULL DEFAULT '[]',
    format        TEXT NOT NULL DEFAULT '',
    bitrate       TEXT NOT NULL DEFAULT '',
    length        TEXT NOT NULL DEFAULT '',
    size          TEXT NOT NULL DEFAULT '',
    info_hash     TEXT NOT NULL DEFAULT '',
    trackers      TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL DEFAULT 'wanted',
    progress      REAL NOT NULL DEFAULT 0,
    speed         INTEGER NOT NULL DEFAULT 0,
    detail        TEXT NOT NULL DEFAULT '',
    error         TEXT NOT NULL DEFAULT '',
    rd_torrent_id TEXT NOT NULL DEFAULT '',
    path          TEXT NOT NULL DEFAULT '',
    added_at      REAL NOT NULL,
    updated_at    REAL NOT NULL,
    completed_at  REAL
);
"""

# Columns added after the first release; added to existing databases on startup.
MIGRATIONS = {
    "listen_position": "REAL NOT NULL DEFAULT 0",
    "listen_duration": "REAL NOT NULL DEFAULT 0",
    "listened_at": "REAL",
    "finished": "INTEGER NOT NULL DEFAULT 0",
    "files": "TEXT NOT NULL DEFAULT '[]'",
    "protocol": "TEXT NOT NULL DEFAULT ''",
    "download_url": "TEXT NOT NULL DEFAULT ''",
    "sab_nzo_id": "TEXT NOT NULL DEFAULT ''",
    "indexer": "TEXT NOT NULL DEFAULT ''",
    "release": "TEXT NOT NULL DEFAULT ''",
    "download_client": "TEXT NOT NULL DEFAULT ''",  # which client holds the job: sabnzbd, nzbget, qbittorrent…
    "client_job_id": "TEXT NOT NULL DEFAULT ''",  # that client's id for it (an nzo_id, NZBID or info hash)
    "release_url": "TEXT NOT NULL DEFAULT ''",  # the search result a Wanted book was grabbed from
    "import_mode": "TEXT NOT NULL DEFAULT ''",  # local imports: "copy" or "move"
    "asin": "TEXT NOT NULL DEFAULT ''",
    "series": "TEXT NOT NULL DEFAULT '[]'",  # [{asin, title, sequence}]
    "released": "TEXT NOT NULL DEFAULT ''",
    "last_searched": "REAL",
    "search_note": "TEXT NOT NULL DEFAULT ''",
    "requested_by": "INTEGER",  # the user who added it, when known
}

JSON_COLUMNS = ("narrators", "categories", "trackers", "files", "series")

FOLLOWS = """
CREATE TABLE IF NOT EXISTS follows (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,              -- "author" or "series"
    asin       TEXT NOT NULL,
    name       TEXT NOT NULL,
    image      TEXT NOT NULL DEFAULT '',
    known      TEXT NOT NULL DEFAULT '[]', -- book ASINs already seen, so only new releases get added
    added_at   REAL NOT NULL,
    checked_at REAL,
    UNIQUE (kind, asin)
);
"""

ACCOUNTS = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL,                -- "admin" or "listener"
    created_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,                -- SHA-256 of the cookie; the token itself isn't stored
    user_id    INTEGER NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS app_grants (         -- an app someone signed in to with their account (API v1)
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    name         TEXT NOT NULL,               -- what the app called itself, e.g. "Fable Frog on iPhone"
    created_at   REAL NOT NULL,
    last_used_at REAL NOT NULL,
    last_ip      TEXT NOT NULL DEFAULT '',
    expires_at   REAL NOT NULL                -- when it stops working unless refreshed again
);
CREATE TABLE IF NOT EXISTS app_tokens (
    token_hash TEXT PRIMARY KEY,                -- SHA-256 of the token; the token itself isn't stored
    grant_id   INTEGER NOT NULL,
    kind       TEXT NOT NULL,                   -- "access" or "refresh"
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at    REAL,                            -- refresh: when it was traded in (each works once); access: first use
    parent     TEXT NOT NULL DEFAULT '',        -- the refresh token's hash this one was issued for
    superseded INTEGER NOT NULL DEFAULT 0       -- replaced by a retry: whoever presents it is signed out
);
CREATE INDEX IF NOT EXISTS app_tokens_grant ON app_tokens (grant_id);
CREATE INDEX IF NOT EXISTS app_tokens_parent ON app_tokens (parent);
CREATE INDEX IF NOT EXISTS app_tokens_expiry ON app_tokens (expires_at);
CREATE TABLE IF NOT EXISTS progress (           -- each person's place in each book
    user_id     INTEGER NOT NULL,
    book_id     INTEGER NOT NULL,
    position    REAL NOT NULL DEFAULT 0,
    duration    REAL NOT NULL DEFAULT 0,
    listened_at REAL,
    finished    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, book_id)
);
"""

# Whose listening progress book rows carry: set per request to the signed-in user.
viewer: ContextVar[int | None] = ContextVar("viewer", default=None)


def db_path() -> Path:
    return config.DATA_DIR / "library.db"


@contextmanager
def connect():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA + FOLLOWS + ACCOUNTS)
        if "superseded" not in {r["name"] for r in conn.execute("PRAGMA table_info(app_tokens)")}:  # pre-release 0.6.0
            conn.execute("ALTER TABLE app_tokens ADD COLUMN superseded INTEGER NOT NULL DEFAULT 0")
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(books)")}
        for col, ddl in MIGRATIONS.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE books ADD COLUMN {col} {ddl}")
        # 0.2.0 kept SABnzbd's job id in its own column.
        conn.execute(
            "UPDATE books SET download_client='sabnzbd', client_job_id=sab_nzo_id "
            "WHERE sab_nzo_id != '' AND client_job_id = ''"
        )
        # Anything mid-flight when the app last stopped goes back in the queue.
        conn.execute(
            f"UPDATE books SET status='queued', speed=0 WHERE status IN ({','.join('?' * len(ACTIVE))})",
            ACTIVE,
        )


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for col in JSON_COLUMNS:
        d[col] = json.loads(d[col] or "[]")
    d["finished"] = bool(d.get("finished"))
    return d


def _books(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    """Book rows, carrying the current viewer's own place in each one."""
    books = [_row(r) for r in conn.execute(sql, args).fetchall()]
    user_id = viewer.get()
    if user_id is None or not books:
        return books
    mine = {r["book_id"]: r for r in conn.execute("SELECT * FROM progress WHERE user_id=?", (user_id,))}
    for b in books:
        p = mine.get(b["id"])
        b["listen_position"] = p["position"] if p else 0.0
        b["listen_duration"] = p["duration"] if p else 0.0
        b["listened_at"] = p["listened_at"] if p else None
        b["finished"] = bool(p["finished"]) if p else False
    return books


def list_books() -> list[dict]:
    with connect() as conn:
        return _books(conn, "SELECT * FROM books ORDER BY added_at DESC")


def _one(sql: str, args: tuple) -> dict | None:
    with connect() as conn:
        found = _books(conn, sql, args)
    return found[0] if found else None


def get(book_id: int) -> dict | None:
    return _one("SELECT * FROM books WHERE id=?", (book_id,))


def get_by_url(url: str) -> dict | None:
    return _one("SELECT * FROM books WHERE source_url=?", (url,))


def get_by_path(path: str) -> dict | None:
    return _one("SELECT * FROM books WHERE path=?", (path,))


def save_progress(user_id: int, book_id: int, position: float | None = None, duration: float | None = None,
                  finished: bool | None = None) -> None:
    """Record where someone is in a book. Finishing it puts them back at the start for next time."""
    if finished:
        position = 0.0
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO progress (user_id, book_id) VALUES (?, ?)", (user_id, book_id))
        sets, args = ["listened_at=?"], [time.time()]
        for col, v in (("position", position), ("duration", duration), ("finished", finished)):
            if v is not None:
                sets.append(f"{col}=?")
                args.append(int(v) if col == "finished" else v)
        conn.execute(f"UPDATE progress SET {', '.join(sets)} WHERE user_id=? AND book_id=?", (*args, user_id, book_id))


def insert(book: dict) -> dict:
    now = time.time()
    data = {**book, "added_at": now, "updated_at": now}
    for col in JSON_COLUMNS:
        if col in data:
            data[col] = json.dumps(data[col])
    cols = ", ".join(data)
    marks = ", ".join("?" * len(data))
    with connect() as conn:
        cur = conn.execute(f"INSERT INTO books ({cols}) VALUES ({marks})", tuple(data.values()))
        book_id = cur.lastrowid
    return get(book_id)


def update(book_id: int, **changes) -> None:
    changes["updated_at"] = time.time()
    for col in JSON_COLUMNS:
        if col in changes:
            changes[col] = json.dumps(changes[col])
    sets = ", ".join(f"{k}=?" for k in changes)
    with connect() as conn:
        conn.execute(f"UPDATE books SET {sets} WHERE id=?", (*changes.values(), book_id))


def delete(book_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM books WHERE id=?", (book_id,))
        conn.execute("DELETE FROM progress WHERE book_id=?", (book_id,))


def next_queued(exclude: set[int]) -> dict | None:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM books WHERE status='queued' ORDER BY updated_at ASC").fetchall()
    for r in rows:
        if r["id"] not in exclude:
            return _row(r)
    return None


def stats() -> dict:
    with connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM books GROUP BY status").fetchall()
    counts = {s: 0 for s in STATUSES}
    for r in rows:
        counts[r["status"]] = r["n"]
    return {
        "total": sum(counts.values()),
        "completed": counts["completed"],
        "active": sum(counts[s] for s in ACTIVE),
        "wanted": counts["wanted"],
        "failed": counts["failed"],
    }


# ---- follows ----------------------------------------------------------------


def _follow(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["known"] = json.loads(d["known"] or "[]")
    return d


def list_follows() -> list[dict]:
    with connect() as conn:
        return [_follow(r) for r in conn.execute("SELECT * FROM follows ORDER BY name COLLATE NOCASE").fetchall()]


def get_follow(follow_id: int) -> dict | None:
    with connect() as conn:
        return _follow(conn.execute("SELECT * FROM follows WHERE id=?", (follow_id,)).fetchone())


def add_follow(kind: str, asin: str, name: str, image: str, known: list[str]) -> dict:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO follows (kind, asin, name, image, known, added_at, checked_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(kind, asin) DO UPDATE SET name=excluded.name, image=excluded.image",
            (kind, asin, name, image, json.dumps(known), time.time(), time.time()),
        )
        follow_id = cur.lastrowid or conn.execute(
            "SELECT id FROM follows WHERE kind=? AND asin=?", (kind, asin)
        ).fetchone()["id"]
    return get_follow(follow_id)


def update_follow(follow_id: int, **changes) -> None:
    if "known" in changes:
        changes["known"] = json.dumps(changes["known"])
    sets = ", ".join(f"{k}=?" for k in changes)
    with connect() as conn:
        conn.execute(f"UPDATE follows SET {sets} WHERE id=?", (*changes.values(), follow_id))


def delete_follow(follow_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM follows WHERE id=?", (follow_id,))

