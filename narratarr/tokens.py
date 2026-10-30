"""Tokens for apps that use Narratarr's API (v1) on someone's behalf.

Signing in from an app creates a *grant*: one app on one device, which the person can see
and revoke under their account. A grant holds two kinds of token:

- an **access token**, sent with every request, which works for an hour;
- a **refresh token**, traded for a fresh pair before the access token runs out. Each one
  works once, and the grant lapses after 90 days without a refresh.

Tokens are 256 random bits behind a recognisable prefix (so secret scanners can spot a
leaked one). Only their SHA-256 is stored, as with web sessions, so a copy of the database
can't be used to sign in.

Refresh tokens rotate. A refresh token that turns up again after it was traded in means
someone has a copy, so the whole grant is revoked: the thief and the app are both signed
out, and the person signs the app in again.

The one exception is a genuine retry: an app whose reply was lost to a dropped connection
sends the same refresh token again within a few seconds, before touching the pair it never
received. That gets a new pair, and the first pair is marked *superseded* rather than
deleted, so whoever holds it (the app, or someone who replayed the token first) trips the
alarm the moment they use either token of it. A retry is refused once a token from an earlier answer has
been used, because then the app clearly got its reply.

Changing a password or removing a user revokes all of their grants (users.py).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import sqlite3
import time

from . import db, users

log = logging.getLogger("narratarr.tokens")

ACCESS_PREFIX = "nrt_at_"
REFRESH_PREFIX = "nrt_rt_"
ACCESS_SECONDS = 60 * 60
REFRESH_SECONDS = 90 * 24 * 3600
RETRY_SECONDS = 120  # a refresh token traded in again this soon, with its new pair untouched, is a retry
MAX_GRANTS = 25  # per person; signing in one more app retires the longest-unused one
REFRESHES_PER_WINDOW = 30  # per grant; an app needs one an hour
REFRESH_WINDOW = 10 * 60
KEEP_SPENT = 20  # traded-in refresh tokens remembered per grant, to recognise a stolen copy
KEEP_ACCESS = 5  # recent access tokens kept after expiry per grant, so a late one says "expired", not "invalid"
RETRIES_PER_TOKEN = 3  # more retries of one refresh token than this isn't a flaky network
MAX_SUPERSEDED = 30  # per grant; an app that piles up more retried pairs than this is signed out
# Superseded tokens are never cleaned up while their grant lives: they're the tripwires. Their
# number is bounded by MAX_SUPERSEDED instead.
NAME_MAX = 64
_TOUCH_EVERY = 60  # how often last-used is written, so every request isn't a database write


class TokenError(Exception):
    """Why a token didn't work. `code` is part of the API: invalid_token, token_expired or rate_limited."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _looks_like(token: str, prefix: str) -> bool:
    return token.startswith(prefix) and 40 <= len(token) <= 100 and token.isascii()


def clean_name(name: str) -> str:
    """An app's name as it'll be shown: printable, one line, at most NAME_MAX characters."""
    name = " ".join("".join(c for c in name if c.isprintable()).split())
    return name[:NAME_MAX].strip()


def _new_pair(conn: sqlite3.Connection, grant_id: int, parent: str, now: float) -> dict:
    access = ACCESS_PREFIX + secrets.token_urlsafe(32)
    refresh = REFRESH_PREFIX + secrets.token_urlsafe(32)
    conn.executemany(
        "INSERT INTO app_tokens (token_hash, grant_id, kind, created_at, expires_at, parent) VALUES (?, ?, ?, ?, ?, ?)",
        [(_digest(access), grant_id, "access", now, now + ACCESS_SECONDS, parent),
         (_digest(refresh), grant_id, "refresh", now, now + REFRESH_SECONDS, parent)],
    )
    conn.execute("UPDATE app_grants SET expires_at=?, last_used_at=? WHERE id=?", (now + REFRESH_SECONDS, now, grant_id))
    return {
        "access_token": access, "refresh_token": refresh, "token_type": "Bearer",
        "expires_in": ACCESS_SECONDS, "refresh_expires_in": REFRESH_SECONDS,
    }


def _drop_grant(conn: sqlite3.Connection, grant_id: int) -> None:
    conn.execute("DELETE FROM app_tokens WHERE grant_id=?", (grant_id,))
    conn.execute("DELETE FROM app_grants WHERE id=?", (grant_id,))


def _prune(conn: sqlite3.Connection, now: float) -> None:
    """Forget lapsed grants, and tokens long past any use. Recently expired access tokens stay,
    so an app using one hears "expired" (refresh) rather than "invalid" (sign in again)."""
    for (grant_id,) in conn.execute("SELECT id FROM app_grants WHERE expires_at < ?", (now,)).fetchall():
        _drop_grant(conn, grant_id)
    conn.execute("DELETE FROM app_tokens WHERE expires_at < ? AND superseded=0", (now - REFRESH_SECONDS,))


def _stolen(conn: sqlite3.Connection, grant_id: int, why: str) -> TokenError:
    _drop_grant(conn, grant_id)
    log.warning("Signed app %s out: %s", grant_id, why)
    return TokenError("invalid_token", "That refresh token was already used, so this app was signed out to be "
                                       "safe. Sign in again.")


def issue(user_id: int, name: str, ip: str = "") -> dict:
    """Sign an app in: a new grant and its first pair of tokens."""
    name = clean_name(name) or "An app"
    now = time.time()
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _prune(conn, now)
        extra = conn.execute(
            "SELECT id FROM app_grants WHERE user_id=? ORDER BY last_used_at DESC LIMIT -1 OFFSET ?",
            (user_id, MAX_GRANTS - 1),
        ).fetchall()
        for (grant_id,) in extra:
            _drop_grant(conn, grant_id)
        cur = conn.execute(
            "INSERT INTO app_grants (user_id, name, created_at, last_used_at, last_ip, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, name, now, now, ip, now + REFRESH_SECONDS),
        )
        return _new_pair(conn, cur.lastrowid, "", now)


def refresh(token: str, ip: str = "") -> tuple[dict, dict]:
    """Trade a refresh token for a new pair. Returns (tokens, user)."""
    if not _looks_like(token, REFRESH_PREFIX):
        raise TokenError("invalid_token", "That refresh token isn't valid. Sign in again.")
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")  # one trade at a time, so two can't both win
        # Revocations below must be committed even though the trade fails, so the
        # outcome is returned rather than raised inside the transaction.
        outcome = _trade(conn, _digest(token), ip, time.time())
    if isinstance(outcome, TokenError):
        raise outcome
    return outcome


def _trade(conn: sqlite3.Connection, digest: str, ip: str, now: float) -> tuple[dict, dict] | TokenError:
    _prune(conn, now)
    row = conn.execute(
        "SELECT app_tokens.*, app_grants.user_id FROM app_tokens "
        "JOIN app_grants ON app_grants.id = app_tokens.grant_id WHERE token_hash=? AND kind='refresh'",
        (digest,),
    ).fetchone()
    if row is None:
        return TokenError("invalid_token", "That refresh token isn't valid. Sign in again.")
    grant_id = row["grant_id"]
    if row["superseded"]:
        return _stolen(conn, grant_id, "a refresh token replaced by a retry was used")
    retry = row["used_at"] is not None
    if retry:
        if now - row["used_at"] > RETRY_SECONDS:
            return _stolen(conn, grant_id, "a refresh token was used twice")
        first_pairs = conn.execute("SELECT used_at, kind FROM app_tokens WHERE parent=?", (digest,)).fetchall()
        if any(t["used_at"] is not None for t in first_pairs):  # the app got its reply and used it
            return _stolen(conn, grant_id, "a refresh token was used again after its new pair was")
        if sum(t["kind"] == "refresh" for t in first_pairs) > RETRIES_PER_TOKEN:
            return _stolen(conn, grant_id, "a refresh token was retried too many times")
        retired = conn.execute("SELECT COUNT(*) FROM app_tokens WHERE grant_id=? AND superseded=1 AND kind='refresh'",
                               (grant_id,)).fetchone()[0]
        if retired >= MAX_SUPERSEDED:
            return _stolen(conn, grant_id, "far too many retried refreshes")
    recent = conn.execute("SELECT COUNT(*) FROM app_tokens WHERE grant_id=? AND kind='refresh' AND created_at > ?",
                          (grant_id, now - REFRESH_WINDOW)).fetchone()[0]
    if recent >= REFRESHES_PER_WINDOW:  # checked before anything changes, so the token still works later
        return TokenError("rate_limited", "This app is refreshing far too often. Try again in a few minutes.")
    if retry:  # the reply to the first trade was lost; that pair stays on record, superseded
        conn.execute("UPDATE app_tokens SET superseded=1 WHERE parent=?", (digest,))
    else:
        conn.execute("UPDATE app_tokens SET used_at=? WHERE token_hash=?", (now, digest))
    user = conn.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone()
    if user is None:
        _drop_grant(conn, grant_id)
        return TokenError("invalid_token", "That account no longer exists.")
    # Keep the table small: the most recent spent refresh tokens (enough to catch a copy being
    # used) and access tokens. Superseded tokens are left alone; they're tripwires.
    for kind, used, keep in (("refresh", "AND used_at IS NOT NULL", KEEP_SPENT), ("access", "", KEEP_ACCESS)):
        conn.execute(
            f"DELETE FROM app_tokens WHERE token_hash IN (SELECT token_hash FROM app_tokens WHERE grant_id=? "
            f"AND kind=? AND superseded=0 {used} ORDER BY created_at DESC LIMIT -1 OFFSET ?)",
            (grant_id, kind, keep),
        )
    conn.execute("UPDATE app_grants SET last_ip=? WHERE id=?", (ip, grant_id))
    return _new_pair(conn, grant_id, digest, now), users._public(user)


def authenticate(token: str, ip: str = "") -> tuple[dict, int]:
    """The person and grant an access token belongs to. Raises TokenError."""
    if not _looks_like(token, ACCESS_PREFIX):
        raise TokenError("invalid_token", "That access token isn't valid.")
    with db.connect() as conn:
        # As in refresh(): a revocation must be committed, so failures are returned, then raised.
        outcome = _check_access(conn, _digest(token), ip, time.time())
    if isinstance(outcome, TokenError):
        raise outcome
    return outcome


def _check_access(conn: sqlite3.Connection, digest: str, ip: str, now: float) -> tuple[dict, int] | TokenError:
    row = conn.execute(
        "SELECT app_tokens.expires_at, app_tokens.grant_id, app_tokens.used_at AS first_used, app_tokens.superseded, "
        "app_grants.last_used_at, app_grants.last_ip, users.* "
        "FROM app_tokens JOIN app_grants ON app_grants.id = app_tokens.grant_id "
        "JOIN users ON users.id = app_grants.user_id WHERE token_hash=? AND kind='access'",
        (digest,),
    ).fetchone()
    if row is None:
        return TokenError("invalid_token", "That access token isn't valid.")
    if row["superseded"]:  # someone else traded the refresh token this came with: sign everyone out
        _drop_grant(conn, row["grant_id"])
        log.warning("Signed app %s out: an access token replaced by a retry was used", row["grant_id"])
        return TokenError("invalid_token", "This app was signed out to be safe, because its refresh token was "
                                           "used somewhere else. Sign in again.")
    if row["expires_at"] < now:
        return TokenError("token_expired", "That access token has expired. Use the refresh token to get a new one.")
    if row["first_used"] is None:  # a used pair can no longer be replaced by a retry
        conn.execute("UPDATE app_tokens SET used_at=? WHERE token_hash=?", (now, digest))
    if now - row["last_used_at"] > _TOUCH_EVERY or row["last_ip"] != ip:
        conn.execute("UPDATE app_grants SET last_used_at=?, last_ip=? WHERE id=?", (now, ip, row["grant_id"]))
    return users._public(row), row["grant_id"]


def revoke(grant_id: int, user_id: int | None = None) -> bool:
    """Sign one app out. With user_id, only if it's that person's. Whether anything was revoked."""
    with db.connect() as conn:
        found = conn.execute("SELECT user_id FROM app_grants WHERE id=?", (grant_id,)).fetchone()
        if found is None or (user_id is not None and found["user_id"] != user_id):
            return False
        _drop_grant(conn, grant_id)
    return True


def grants(user_id: int) -> list[dict]:
    """The apps signed in to this account, most recently used first."""
    now = time.time()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, name, created_at, last_used_at, last_ip FROM app_grants WHERE user_id=? AND expires_at >= ? "
            "ORDER BY last_used_at DESC", (user_id, now),
        ).fetchall()
    return [dict(r) for r in rows]
