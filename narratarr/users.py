"""Accounts and sign-in sessions.

Two roles: admins run the place (settings, users, imports, deleting books); listeners
browse, request and listen. Everyone shares one library, but each person has their
own place in every book.

Passwords are hashed with scrypt from the standard library. A session is a random
token in an HttpOnly cookie; only its SHA-256 is stored, so a copy of the database
can't be used to sign in.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time

from . import db

ROLES = ("admin", "listener")
SESSION_SECONDS = 30 * 24 * 3600
MIN_PASSWORD = 8
_USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,31}")
# scrypt at n=2^15 takes ~50 ms and 32 MB: slow for anyone guessing, fine for a sign-in.
_N, _R, _P = 2**15, 8, 1
# At most two at once, so a flood of sign-ins queues up instead of using a gigabyte of memory.
_HASHING = threading.BoundedSemaphore(2)


class UserError(ValueError):
    pass


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    with _HASHING:
        key = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=64 << 20)
    b64 = base64.b64encode
    return f"scrypt${_N}${_R}${_P}${b64(salt).decode()}${b64(key).decode()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, key = stored.split("$")
        with _HASHING:
            got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                 maxmem=64 << 20)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, base64.b64decode(key))


_DUMMY = hash_password(secrets.token_hex(8))  # checked against when a username doesn't exist, so timing gives nothing away


def _public(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {"id": row["id"], "username": row["username"], "role": row["role"], "created_at": row["created_at"]}


def _valid_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise UserError(f"Use a password of at least {MIN_PASSWORD} characters.")


# ---- accounts -------------------------------------------------------------------------


def count(role: str | None = None) -> int:
    with db.connect() as conn:
        if role:
            return conn.execute("SELECT COUNT(*) FROM users WHERE role=?", (role,)).fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def all_users() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY role, username COLLATE NOCASE").fetchall()
    return [_public(r) for r in rows]


def get(user_id: int) -> dict | None:
    with db.connect() as conn:
        return _public(conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())


def by_name(username: str) -> dict | None:
    with db.connect() as conn:
        return _public(conn.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone())


def owner() -> dict | None:
    """The first admin: who the home network acts as when sign-in is skipped there."""
    with db.connect() as conn:
        return _public(conn.execute("SELECT * FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone())


def create(username: str, password: str, role: str = "listener") -> dict:
    username = username.strip()
    if not _USERNAME.fullmatch(username):
        raise UserError("Usernames are 1–32 letters, numbers, dots, dashes, underscores or @, starting with a letter or number.")
    if role not in ROLES:
        raise UserError("A user is either an admin or a listener.")
    _valid_password(password)
    first = count() == 0
    try:
        with db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                (username, hash_password(password), role, time.time()),
            )
            user_id = cur.lastrowid
            if first:  # the listening progress from before there were accounts belongs to whoever sets it up
                conn.execute(
                    "INSERT OR IGNORE INTO progress (user_id, book_id, position, duration, listened_at, finished) "
                    "SELECT ?, id, listen_position, listen_duration, listened_at, finished FROM books "
                    "WHERE listened_at IS NOT NULL OR finished = 1",
                    (user_id,),
                )
    except sqlite3.IntegrityError:
        raise UserError(f"There's already a user called {username}.")
    return get(user_id)


def authenticate(username: str, password: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username.strip(),)).fetchone()
    if row is None:
        check_password(password, _DUMMY)
        return None
    return _public(row) if check_password(password, row["password_hash"]) else None


def set_password(user_id: int, password: str) -> None:
    """Change a password and sign that person out everywhere, apps included."""
    _valid_password(password)
    with db.connect() as conn:
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(password), user_id))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        _revoke_apps(conn, user_id)


def set_role(user_id: int, role: str) -> None:
    if role not in ROLES:
        raise UserError("A user is either an admin or a listener.")
    user = get(user_id)
    if user and user["role"] == "admin" and role != "admin" and count("admin") == 1:
        raise UserError("That's the only admin. Make someone else an admin first.")
    with db.connect() as conn:
        conn.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))


def delete(user_id: int) -> None:
    user = get(user_id)
    if user and user["role"] == "admin" and count("admin") == 1:
        raise UserError("That's the only admin, so it can't be removed.")
    with db.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        _revoke_apps(conn, user_id)
        conn.execute("DELETE FROM progress WHERE user_id=?", (user_id,))
        conn.execute("UPDATE books SET requested_by=NULL WHERE requested_by=?", (user_id,))
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))


def _revoke_apps(conn: sqlite3.Connection, user_id: int) -> None:
    """Sign every app out of this account (see tokens.py)."""
    conn.execute("DELETE FROM app_tokens WHERE grant_id IN (SELECT id FROM app_grants WHERE user_id=?)", (user_id,))
    conn.execute("DELETE FROM app_grants WHERE user_id=?", (user_id,))


# ---- sessions --------------------------------------------------------------------------


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with db.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        conn.execute("INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                     (_digest(token), user_id, now, now + SESSION_SECONDS))
    return token


def session_user(token: str) -> dict | None:
    if not token:
        return None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id "
            "WHERE sessions.token_hash=? AND sessions.expires_at > ?",
            (_digest(token), time.time()),
        ).fetchone()
    return _public(row)


def end_session(token: str) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (_digest(token),))
