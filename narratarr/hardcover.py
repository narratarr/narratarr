"""Hardcover (hardcover.app): books on your Want to Read shelf come to Wanted.

HOW IT CONNECTS
    Hardcover's OAuth Device Authorization Grant, for a public client (no secret). Narratarr asks Hardcover
    for a short code, the admin types it in at hardcover.app (or opens the link with it filled in) and
    approves, and Narratarr gets an access token and a refresh token. Hardcover's docs ask apps never to
    have people paste a personal token in, and this is the flow they give for apps on a machine you
    don't control. Scopes: read:me:content (whose shelf) and read:library (the shelf). Nothing is ever
    written to Hardcover.

WHAT STAYS ON THE SERVER
    The device code, the access token and the refresh token. None of them is sent to the browser
    (config.SERVER_ONLY), the settings form can't change them, and Disconnect revokes them at Hardcover
    as well as forgetting them here.

ONE AT A TIME
    Refresh tokens rotate: each renewal hands back a new one and retires the old. Two renewals at once
    (the six-hourly check and Check now) would send the same old token twice, and the second would be
    refused. So renewals take a lock and look again once they have it, and the sign-in is only forgotten
    when the token Hardcover refused is still the one stored. A generation number makes Disconnect win
    over a renewal that was already under way. Checks of the shelf take a lock too.

LIMITS (docs.hardcover.app, "Getting Started with the API")
    The API is a beta that "may change or break at any time". Free accounts get 60 requests a minute,
    a burst of 10 and 5,000 a day; each top-level query costs one. A check costs one for `me` plus one
    per 100 shelf books read, stops reading once it has enough new books, waits a second between pages,
    and honours Retry-After once. Queries time out at 30 seconds, and a user-agent naming the tool is
    recommended.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

from . import config

log = logging.getLogger(__name__)

API = "https://api.hardcover.app"
GRAPHQL = f"{API}/v1/graphql"
DEVICE = f"{API}/oauth2/device"
TOKEN = f"{API}/oauth2/token"
REVOKE = f"{API}/oauth2/revoke"
SCOPE = "read:me:content read:library"
LINK_HOST = "https://hardcover.app/"  # where the sign-in page may point, and nowhere else

# Narratarr's public app id on Hardcover. A public client has no secret, so this is safe to publish (Hardcover:
# "anyone can decompile your app or read your script, so it has to be safe for this to be public").
BUILT_IN_CLIENT_ID = ""
WANT_TO_READ = 1  # user_books.status_id
AUDIO, BOTH = 2, 3  # editions.reading_format_id
PAGE = 100
MAX_BOOKS = 2000  # never read past this far down the shelf in one check
PAGE_PAUSE = 1.0  # seconds between shelf pages: the per-minute limit refills one a second
EXPIRED_TOKEN_CODES = {"invalid-jwt", "invalid_token", "invalid-token"}  # a 200 that means "renew me"


class HardcoverError(Exception):
    pass


class Busy(HardcoverError):
    """A check of the shelf is already running."""


# What the last check did, for Settings: {"at": time, "error": "", "added": n, "not_found": n, "more": bool}.
last_sync: dict = {}
_generation = 0  # goes up whenever the sign-in is forgotten, so a renewal already under way can't bring it back
_locks: dict = {}  # per event loop: {"token": Lock, "sync": Lock}


def _lock(name: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    mine = _locks.get(id(loop))
    if not mine or mine["loop"] is not loop:
        mine = _locks[id(loop)] = {"loop": loop, "token": asyncio.Lock(), "sync": asyncio.Lock()}
    return mine[name]


async def record(check):
    """Run a shelf check and note what it did in last_sync, unless the account was disconnected (or a new
    one connected) meanwhile, so an old check's result never shows against the new state."""
    generation = _generation
    try:
        result = await check
    except Busy:
        raise
    except Exception as e:
        if generation == _generation:
            from . import audible

            known = isinstance(e, (HardcoverError, audible.AudibleError, httpx.HTTPError))
            last_sync.clear()
            last_sync.update(at=time.time(), error=str(e) if known else "Couldn't finish checking the shelf. Try again later.")
        raise
    if generation == _generation:
        last_sync.clear()
        last_sync.update(at=time.time(), error="", **{k: result[k] for k in ("added", "not_found", "more")})
    return result


def client_id() -> str:
    """The app id: Narratarr's own, or one set with NARRATARR_HARDCOVER_CLIENT_ID (for a fork)."""
    return os.environ.get("NARRATARR_HARDCOVER_CLIENT_ID", "").strip() or BUILT_IN_CLIENT_ID


def available() -> bool:
    return bool(client_id())


def _user_agent() -> str:
    from .updates import current_version

    return f"Narratarr/{current_version()} (self-hosted audiobook manager; github.com/narratarr/narratarr)"


def _client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=30, transport=transport, headers={"User-Agent": _user_agent()},
                             follow_redirects=False)


def _json(resp: httpx.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        body = None
    return body if isinstance(body, dict) else {}


def _int(value, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(number, high))


# ---- connecting: the device sign-in ------------------------------------------------------------

# The sign-in under way. The device code is what proves it's ours when Narratarr asks for the tokens,
# so it never leaves the server.
_pending: dict = {}


async def start(transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """Ask Hardcover for a sign-in code. Returns what the admin needs: the code and where to enter it."""
    if not available():
        raise HardcoverError("This copy of Narratarr hasn't been registered with Hardcover yet.")
    async with _client(transport) as c:
        try:
            resp = await c.post(DEVICE, data={"client_id": client_id(), "scope": SCOPE})
        except httpx.HTTPError as e:
            raise HardcoverError("Couldn't reach Hardcover. Check this machine's internet connection.") from e
    body = _json(resp)
    if resp.status_code >= 400 or not body.get("device_code") or not body.get("user_code"):
        raise HardcoverError(f"Hardcover didn't start the sign-in ({body.get('error') or resp.status_code}).")
    link = str(body.get("verification_uri") or "")
    full = str(body.get("verification_uri_complete") or "")
    if not link.startswith(LINK_HOST):
        raise HardcoverError("Hardcover's answer pointed somewhere other than hardcover.app, so it was ignored.")
    expires_in = _int(body.get("expires_in"), 600, 60, 1800)
    interval = _int(body.get("interval"), 5, 2, 60)
    _pending.clear()
    _pending.update(device_code=str(body["device_code"]), interval=interval, next_poll=0.0,
                    expires_at=time.time() + expires_in)
    return {"user_code": str(body["user_code"])[:32], "verification_uri": link,
            "verification_uri_complete": full if full.startswith(LINK_HOST) else link,
            "expires_in": expires_in, "interval": interval}


async def poll(transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """Has the admin approved yet? {"state": "pending" | "connected" | "expired" | "denied" | "idle"}."""
    if not _pending:
        return {"state": "idle"}
    now = time.time()
    if now > _pending["expires_at"]:
        _pending.clear()
        return {"state": "expired"}
    if now < _pending["next_poll"]:
        return {"state": "pending"}  # Hardcover asks for no faster than `interval`; the page can ask as often as it likes
    _pending["next_poll"] = now + _pending["interval"]
    code = _pending["device_code"]
    async with _client(transport) as c:
        try:
            resp = await c.post(TOKEN, data={"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                                             "device_code": code, "client_id": client_id()})
        except httpx.HTTPError:
            return {"state": "pending"}  # a blip; the next poll tries again
    body = _json(resp)
    if _pending.get("device_code") != code:
        # Cancelled, replaced by a new sign-in, or Disconnect was pressed while this answer was on its way.
        if body.get("access_token"):
            await _revoke(str(body.get("refresh_token") or body["access_token"]), transport)
        return {"state": "pending" if _pending else "idle"}
    if body.get("access_token"):
        _pending.clear()
        global _generation
        _generation += 1  # a fresh sign-in: anything from before it is stale
        _save_tokens(body)
        try:
            me = await _me(transport)
            config.update({"hardcover_user": me["username"]})
        except HardcoverError as e:
            log.info("Connected to Hardcover, but couldn't read the account name: %s", e)
        return {"state": "connected", "user": config.load().hardcover_user}
    error = str(body.get("error") or "")
    if error == "authorization_pending":
        return {"state": "pending"}
    if error == "slow_down" or not error or resp.status_code >= 500 or resp.status_code == 429:
        # Slow down, or Hardcover having a bad moment: keep the code and wait longer before asking again.
        _pending["interval"] = min(_pending["interval"] + 5, 60)
        _pending["next_poll"] = time.time() + _pending["interval"]
        return {"state": "pending"}
    _pending.clear()
    return {"state": "denied" if error == "access_denied" else "expired"}


def _save_tokens(body: dict) -> None:
    access, refresh = str(body.get("access_token") or ""), str(body.get("refresh_token") or "")
    expires_in = _int(body.get("expires_in"), 3600, 60, 366 * 86400)
    config.update({"hardcover_access_token": access,
                   "hardcover_refresh_token": refresh or config.load().hardcover_refresh_token,
                   "hardcover_token_expires": time.time() + expires_in})


def _forget_tokens() -> None:
    global _generation
    _generation += 1
    config.update({"hardcover_access_token": "", "hardcover_refresh_token": "", "hardcover_token_expires": 0.0,
                   "hardcover_user": ""})


async def disconnect(transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Revoke the tokens at Hardcover (so it stops listing Narratarr as signed in), then forget them."""
    s = config.load()
    token = s.hardcover_refresh_token or s.hardcover_access_token
    hint = "refresh_token" if s.hardcover_refresh_token else "access_token"
    _pending.clear()
    _forget_tokens()  # first, so a renewal already under way can't store new ones (see _generation)
    if token:
        await _revoke(token, transport, hint)


async def _revoke(token: str, transport=None, hint: str = "refresh_token", client: httpx.AsyncClient | None = None) -> None:
    """Best effort: tell Hardcover a token is no longer wanted (revoking either half revokes the pair)."""
    if not (token and available()):
        return
    data = {"token": token, "client_id": client_id(), "token_type_hint": hint}
    try:
        if client is not None:
            await client.post(REVOKE, data=data)
        else:
            async with _client(transport) as c:
                await c.post(REVOKE, data=data)
    except httpx.HTTPError:
        log.info("Couldn't reach Hardcover to revoke a sign-in; it's forgotten here anyway")


# ---- asking -------------------------------------------------------------------------------------


def _usable(s) -> bool:
    return bool(s.hardcover_access_token) and time.time() < s.hardcover_token_expires - 60


async def _access_token(c: httpx.AsyncClient, stale: str = "") -> str:
    """A working access token, renewed when it has run out, or when `stale` (one Hardcover just refused)
    is still the one stored."""
    s = config.load()
    if _usable(s) and s.hardcover_access_token != stale:
        return s.hardcover_access_token
    async with _lock("token"):
        s = config.load()  # another renewal may have finished while this one waited
        if _usable(s) and s.hardcover_access_token != stale:
            return s.hardcover_access_token
        sent, generation = s.hardcover_refresh_token, _generation
        if not sent:
            if s.hardcover_access_token and s.hardcover_access_token != stale:
                return s.hardcover_access_token
            raise HardcoverError("Hardcover isn't connected. Connect it under Settings → Search & sources.")
        try:
            resp = await c.post(TOKEN, data={"grant_type": "refresh_token", "refresh_token": sent,
                                             "client_id": client_id()})
        except httpx.HTTPError as e:
            raise HardcoverError("Couldn't reach Hardcover.") from e
        body = _json(resp)
        if generation != _generation:
            if body.get("access_token"):  # Disconnect won: store nothing, and don't leave this new pair signed in
                await _revoke(str(body.get("refresh_token") or body["access_token"]), client=c)
            raise HardcoverError("Hardcover was disconnected.")
        if not body.get("access_token"):
            refused = body.get("error") in ("invalid_grant", "invalid_token", "unauthorized_client")
            if refused and config.load().hardcover_refresh_token == sent:
                _forget_tokens()  # revoked or expired at Hardcover: say so, rather than retry forever
                raise HardcoverError("Hardcover signed Narratarr out. Connect it again under Settings → Search & sources.")
            raise HardcoverError(f"Hardcover didn't renew Narratarr's sign-in ({body.get('error') or resp.status_code}).")
        _save_tokens(body)
        return str(body["access_token"])


def _expired_token_error(body: dict) -> bool:
    for e in body.get("errors") or []:
        code = ((e.get("extensions") or {}).get("code") if isinstance(e, dict) else "") or ""
        if str(code).lower() in EXPIRED_TOKEN_CODES:
            return True
    return False


async def query(text: str, variables: dict | None = None, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """One GraphQL request (always with variables, never with values pasted into the query)."""
    async with _client(transport) as c:
        stale, waited = "", False
        for _attempt in range(3):
            token = await _access_token(c, stale)
            try:
                resp = await c.post(GRAPHQL, json={"query": text, "variables": variables or {}},
                                    headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as e:
                raise HardcoverError("Couldn't reach Hardcover.") from e
            body = _json(resp)
            if (resp.status_code == 401 or (resp.status_code == 200 and _expired_token_error(body))) and not stale:
                stale = token  # it ran out early: renew it once
                continue
            wait = resp.headers.get("retry-after", "")
            if resp.status_code == 429 and not waited and wait.isdigit() and int(wait) <= 60:
                waited = True
                await asyncio.sleep(int(wait))  # the per-minute bucket refills; try once more
                continue
            break
    if resp.status_code == 429:
        raise HardcoverError(f"Hardcover is busy (rate limit). Try again{f' in {wait} seconds' if wait.isdigit() else ' later'}.")
    if resp.status_code == 401 or _expired_token_error(body):
        raise HardcoverError("Hardcover didn't accept Narratarr's sign-in. Connect it again.")
    if resp.status_code == 403:
        raise HardcoverError(f"Hardcover refused that ({body.get('error') or 'forbidden'}). Connect it again to grant access.")
    if resp.status_code >= 400:
        raise HardcoverError(f"Hardcover answered with an error ({resp.status_code}).")
    if body.get("errors"):
        first = body["errors"][0]
        message = first.get("message") if isinstance(first, dict) else str(first)
        raise HardcoverError(f"Hardcover: {str(message)[:200]}")
    data = body.get("data")
    if not isinstance(data, dict):
        raise HardcoverError("Hardcover's answer wasn't what Narratarr expected.")
    return data


async def _me(transport=None) -> dict:
    data = await query("query Me { me { id username } }", transport=transport)
    me = data.get("me")
    me = me[0] if isinstance(me, list) and me else me  # `me` is a function returning users, so it may be a list
    if not isinstance(me, dict) or not isinstance(me.get("id"), int):
        raise HardcoverError("Hardcover didn't say which account this is.")
    return {"id": me["id"], "username": str(me.get("username") or "")[:80]}


SHELF = """
query WantToRead($user: Int!, $status: Int!, $limit: Int!, $offset: Int!) {
  user_books(where: {user_id: {_eq: $user}, status_id: {_eq: $status}},
             order_by: {date_added: desc}, limit: $limit, offset: $offset) {
    id
    book { title contributions { contribution author { name } } }
    edition { asin reading_format_id }
  }
}
"""


def _clean(value, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


def _row(row) -> dict | None:
    if not isinstance(row, dict) or not isinstance(row.get("id"), int):
        return None
    book = row.get("book") if isinstance(row.get("book"), dict) else {}
    people = [c for c in book.get("contributions") or [] if isinstance(c, dict) and isinstance(c.get("author"), dict)]
    # The writers, not the translator, illustrator or narrator (`contribution` is the role; empty means author).
    writers = [c for c in people if str(c.get("contribution") or "author").strip().lower() == "author"] or people
    authors = [_clean(c["author"].get("name"), 120) for c in writers]
    edition = row.get("edition") if isinstance(row.get("edition"), dict) else {}
    asin = _clean(edition.get("asin"), 20).upper()
    audio = edition.get("reading_format_id") in (AUDIO, BOTH)
    return {"id": row["id"], "title": _clean(book.get("title")), "author": ", ".join(a for a in authors if a)[:300],
            "asin": asin if audio and len(asin) == 10 and asin.isalnum() else ""}


async def want_to_read(transport: httpx.AsyncBaseTransport | None = None, skip: set | frozenset = frozenset(),
                       enough: int | None = None) -> tuple[list[dict], bool]:
    """Books on the connected account's Want to Read shelf, newest first, leaving out the ids in `skip`:
    ([{"id": user_book id, "title", "author", "asin": an audio edition's ASIN or ""}], whether there are more).
    With `enough`, reading stops once that many are found, so a big shelf costs a page or two, not twenty."""
    me = await _me(transport)
    if me["username"] and me["username"] != config.load().hardcover_user:
        config.update({"hardcover_user": me["username"]})
    found: list[dict] = []
    offset = 0
    while offset < MAX_BOOKS:
        if offset:
            await asyncio.sleep(PAGE_PAUSE)
        data = await query(SHELF, {"user": me["id"], "status": WANT_TO_READ, "limit": PAGE, "offset": offset}, transport)
        rows = data.get("user_books") or []
        found += [r for r in (_row(x) for x in rows) if r and r["id"] not in skip]
        if enough is not None and len(found) > enough:
            return found[:enough], True
        if len(rows) < PAGE:
            return found, False
        offset += PAGE
    return found, True
