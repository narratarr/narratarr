"""Notifications: tell ntfy, Gotify, Discord or any webhook when something happens to a book.

Every event is also logged, so `docker logs` (and scripts reading it) see it whether or not any
destination is set up. Destinations are kept in their own private file beside settings.json, since
their tokens, header values and webhook addresses are secrets.

Sending never holds up or breaks anything else: it runs in the background, retries once, and a
failure is only noted on the destination (shown in Settings) and in the log.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import threading
import time
from urllib.parse import urlparse, urlunparse

import httpx

from . import config, db

log = logging.getLogger("narratarr.notify")

KINDS = {"ntfy": "ntfy", "gotify": "Gotify", "discord": "Discord", "webhook": "Webhook"}
EVENTS = {
    "wanted": "Added to Wanted",
    "found": "Release found",
    "grabbed": "Download started",
    "completed": "In your library",
    "failed": "Download failed",
}
# What a webhook body can use. The book* names are the ones AudioBookRequest uses, so bodies written
# for it work here too.
VARIABLES = (
    "event", "eventLabel", "message", "title", "author", "narrator", "format", "cover", "username", "error", "release",
    "bookId", "bookTitle", "bookAuthors", "bookCover",
)
DEFAULT_BODY = """{
  "event": "{event}",
  "title": "{title}",
  "author": "{author}",
  "username": "{username}",
  "cover": "{cover}",
  "message": "{message}"
}"""
MAX_DESTINATIONS = 20
TIMEOUT = httpx.Timeout(10, connect=5)
RETRY_SECONDS = 5
COLOR = 0xEC6F55  # Narratarr's coral, for Discord's embed stripe

_lock = threading.Lock()
_tasks: set[asyncio.Task] = set()


class NotifyError(ValueError):
    pass


def _file():
    return config.DATA_DIR / "notifications.json"


def load() -> list[dict]:
    with _lock:
        try:
            items = json.loads(_file().read_text())
        except (OSError, ValueError):
            return []
    return [n for n in items if isinstance(n, dict) and n.get("kind") in KINDS] if isinstance(items, list) else []


def _save(items: list[dict]) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        config._write_private(_file(), json.dumps(items, indent=2))


def get(nid: int) -> dict | None:
    return next((n for n in load() if n["id"] == nid), None)


# ---- what an event says ---------------------------------------------------------


def _requester(book: dict) -> str:
    by = book.get("requested_by")
    if not by:
        return ""
    from . import users  # here: users imports db, which imports config; keep this module light

    u = users.get(by)
    return u["username"] if u else ""


def context(event: str, book: dict, username: str = "", error: str = "") -> dict:
    title = book.get("title") or "Untitled"
    author = book.get("author") or ""
    cover = book.get("cover_url") or ""
    ebook = book.get("kind") == "ebook"
    username = username or _requester(book)
    by = f" by {author}" if author else ""
    what = f"{title}{by}" + (" (ebook)" if ebook else "")
    message = {
        "wanted": f"{username or 'Someone'} wants {what}"
                  + (". It's waiting for your approval" if book.get("status") == "requested" else ""),
        "found": f"Found {book.get('release') or 'a release'} for {what}" + (f", wanted by {username}" if username else "")
                 + ". It's waiting for your approval",
        "grabbed": f"Downloading {what}" + (f", for {username}" if username else ""),
        "completed": f"{what} is in your library" + (f", for {username}" if username else ""),
        "failed": f"{what} couldn't download: {error or 'unknown error'}",
    }[event]
    ctx = {
        "event": event,
        "eventLabel": EVENTS[event],
        "message": message,
        "title": title,
        "author": author,
        "narrator": ", ".join(book.get("narrators") or []),
        "format": "ebook" if ebook else "audiobook",
        # Only a cover the internet can reach; a library cover lives behind Narratarr's sign-in.
        "cover": cover if cover.startswith(("http://", "https://")) else "",
        "username": username,
        "error": error,
        "release": book.get("release") or "",
        "bookId": str(book.get("id") or ""),
    }
    ctx.update(bookTitle=title, bookAuthors=author, bookCover=ctx["cover"])
    return ctx


SAMPLE = {"id": 0, "title": "Project Hail Mary", "author": "Andy Weir", "narrators": ["Ray Porter"],
          "cover_url": "https://m.media-amazon.com/images/I/91vS2L5YfEL.jpg", "kind": "audiobook"}


def render(template: str, ctx: dict) -> str:
    """Fill {placeholders}. Values are JSON-escaped, so a title with quotes can't break the body."""
    return re.sub(
        r"\{(\w+)\}", lambda m: json.dumps(str(ctx[m.group(1)]))[1:-1] if m.group(1) in ctx else m.group(0), template
    )


# ---- what gets sent where ---------------------------------------------------------


def _ntfy_parts(url: str) -> tuple[str, str]:
    """'https://ntfy.sh/books' -> ('https://ntfy.sh', 'books'): ntfy takes JSON at its root."""
    u = urlparse(url)
    segments = [s for s in u.path.split("/") if s]
    if not segments:
        raise NotifyError("Add the topic to the ntfy address, like https://ntfy.sh/narratarr.")
    base = urlunparse(u._replace(path="/" + "/".join(segments[:-1]), query="", fragment="")).rstrip("/")
    return base, segments[-1]


def build(n: dict, ctx: dict) -> tuple[str, dict, bytes]:
    """(url, headers, body) for one destination."""
    headers = {"Content-Type": "application/json", "User-Agent": "Narratarr"}
    kind, url, token = n["kind"], n["url"], n.get("token") or ""
    if kind == "ntfy":
        base, topic = _ntfy_parts(url)
        body = {"topic": topic, "title": ctx["eventLabel"], "message": ctx["message"], "tags": ["books"]}
        if ctx["cover"]:
            body["attach"] = ctx["cover"]
        if token:
            if ":" in token:  # user:password
                headers["Authorization"] = "Basic " + base64.b64encode(token.encode()).decode()
            else:
                headers["Authorization"] = f"Bearer {token}"
        return base + "/", headers, json.dumps(body).encode()
    if kind == "gotify":
        headers["X-Gotify-Key"] = token
        body = {"title": ctx["eventLabel"], "message": ctx["message"], "priority": 5}
        if ctx["cover"]:
            body["extras"] = {"client::notification": {"bigImageUrl": ctx["cover"]}}
        return url.rstrip("/") + "/message", headers, json.dumps(body).encode()
    if kind == "discord":
        embed = {"title": ctx["eventLabel"], "description": ctx["message"], "color": COLOR}
        if ctx["cover"]:
            embed["thumbnail"] = {"url": ctx["cover"]}
        # No pings, whatever a book title says ("@everyone" is a real book).
        body = {"username": "Narratarr", "allowed_mentions": {"parse": []}, "embeds": [embed]}
        return url, headers, json.dumps(body).encode()
    for h in n.get("headers") or []:
        headers[h["name"]] = h["value"]
    return url, headers, render(n.get("body") or DEFAULT_BODY, ctx).encode()


async def deliver(n: dict, ctx: dict, transport: httpx.AsyncBaseTransport | None = None) -> str:
    """Send one notification; "" when it arrived, otherwise what went wrong."""
    try:
        url, headers, body = build(n, ctx)
    except NotifyError as e:
        return str(e)
    error = ""
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False, transport=transport) as client:
                resp = await client.post(url, headers=headers, content=body)
            if resp.status_code < 300:
                return ""
            error = {
                401: "It refused the token (401).",
                403: "It refused the token (403).",
                404: "Nothing at that address (404). Check it, and the topic or webhook.",
            }.get(resp.status_code, f"It answered with an error ({resp.status_code}).")
            if resp.status_code < 500 and resp.status_code != 429:
                return error  # trying again won't change the answer
        except httpx.TimeoutException:
            error = "It took too long to answer."
        except httpx.HTTPError:
            error = "Couldn't reach it. Check the address."
        if attempt == 1:
            await asyncio.sleep(RETRY_SECONDS)
    return error


def _record(nid: int, error: str) -> None:
    items = load()
    for n in items:
        if n["id"] == nid:
            n["last_at"], n["last_error"] = time.time(), error
    _save(items)


async def _send_one(n: dict, ctx: dict, event_id: int | None) -> None:
    started = time.monotonic()
    try:
        error = await deliver(n, ctx)
    except Exception as e:  # noqa: BLE001 - whatever it was, it's noted, never raised
        error = f"Unexpected error: {e}"
    if error:
        log.warning("Notification to %s (%s) failed: %s", n["name"], KINDS[n["kind"]], error)
    _record(n["id"], error)
    if event_id:
        _note_delivery(event_id, n["id"], state="failed" if error else "sent", error=error,
                       ms=int((time.monotonic() - started) * 1000))


async def _send_all(targets: list[dict], ctx: dict, event_id: int | None = None) -> None:
    await asyncio.gather(*(_send_one(n, ctx, event_id) for n in targets))


def _log_line(at: float, ctx: dict) -> str:
    """The line as the log shows it (Narratarr's log format), for the event log in Settings."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at)) + f",{int(at * 1000) % 1000:03d}"
    return f"{stamp} INFO {log.name}: [{ctx['event']}] {ctx['message']} (book {ctx['bookId'] or '?'})"


def _keep(at: float, ctx: dict, targets: list[dict]) -> int | None:
    """Add the event to the event log. Never lets a database problem get in the way."""
    try:
        deliveries = [{"id": n["id"], "name": n["name"], "kind": n["kind"], "state": "sending", "error": "", "ms": 0}
                      for n in targets]
        book_id = int(ctx["bookId"]) if str(ctx["bookId"]).isdigit() else None
        return db.add_event(at, ctx["event"], book_id, ctx["message"], _log_line(at, ctx), deliveries)
    except Exception:  # noqa: BLE001
        log.debug("Couldn't add the event to the event log", exc_info=True)
        return None


def _note_delivery(event_id: int, dest_id: int, **changes) -> None:
    try:
        db.update_delivery(event_id, dest_id, **changes)
    except Exception:  # noqa: BLE001
        log.debug("Couldn't note a delivery in the event log", exc_info=True)


def emit(event: str, book: dict, username: str = "", error: str = "") -> None:
    """Something happened to a book: log it, keep it for the event log, and notify every destination that
    wants this event."""
    try:
        ctx = context(event, book, username, error)
        log.info("[%s] %s (book %s)", event, ctx["message"], ctx["bookId"] or "?")
        targets = [n for n in load() if n.get("enabled", True) and event in (n.get("events") or [])]
    except Exception:  # a notification must never break what triggered it
        log.exception("Couldn't prepare the %s notification", event)
        return
    event_id = _keep(time.time(), ctx, targets)
    if not targets:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # no event loop (a script, or a test without one): logging was enough
        if event_id:
            for n in targets:
                _note_delivery(event_id, n["id"], state="not sent", error="Narratarr wasn't running its sender.")
        return
    task = loop.create_task(_send_all(targets, ctx, event_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def events(event: str = "", query: str = "", before: int | None = None, limit: int = 50) -> dict:
    """The event log, newest first, for Settings → Notifications."""
    rows = db.list_events(event, query.strip()[:100], before, min(max(limit, 1), 200) + 1)
    more = len(rows) > limit
    items = [{**r, "label": EVENTS.get(r["event"], r["event"])} for r in rows[:limit]]
    return {"items": items, "more": more, "counts": db.event_counts(), "kept": db.EVENTS_KEPT}


def events_log() -> str:
    """Every event kept, oldest first, as log lines; failed sends follow the line they belong to."""
    out = []
    for r in reversed(db.list_events(limit=db.EVENTS_KEPT)):
        out.append(r["line"])
        for d in r["deliveries"]:
            if d.get("state") == "failed":
                out.append(f"    {d.get('name')} ({KINDS.get(d.get('kind'), d.get('kind'))}): {d.get('error')}")
    return "\n".join(out) + ("\n" if out else "")


# ---- the destinations themselves (Settings → Notifications) ------------------------------


def _mask(secret: str) -> str:
    return "•" * 8 + secret[-4:] if secret else ""


def _masked_url(n: dict) -> str:
    """Discord and webhook addresses often hold their secret in the path; show only where they go."""
    if n["kind"] not in ("discord", "webhook"):
        return n["url"]
    u = urlparse(n["url"])
    if not (u.path.strip("/") or u.query):
        return n["url"]
    return f"{u.scheme}://{u.netloc}/{_mask(n['url'])}"


def public(n: dict) -> dict:
    """A destination as Settings shows it: secrets masked, like the rest of Settings."""
    return {
        **{k: n.get(k) for k in ("id", "name", "kind", "events", "enabled", "last_at", "last_error")},
        "url": _masked_url(n),
        "token": _mask(n.get("token") or ""),
        "headers": [{"name": h["name"], "value": _mask(h["value"])} for h in n.get("headers") or []],
        "body": n.get("body") or "",
    }


_HEADER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
_RESERVED = {"host", "content-length", "transfer-encoding", "connection", "content-type"}


def _line(value: str, what: str, limit: int) -> str:
    value = str(value or "").strip()
    if "\r" in value or "\n" in value:
        raise NotifyError(f"The {what} can't span several lines.")
    if len(value) > limit:
        raise NotifyError(f"The {what} is too long.")
    return value


def clean(data: dict, old: dict | None = None) -> dict:
    """A destination as it's stored, from what Settings sent. Masked values keep what was saved."""
    old = old or {}
    kind = data.get("kind") or old.get("kind")
    if kind not in KINDS:
        raise NotifyError("Choose ntfy, Gotify, Discord or Webhook.")
    masked = lambda v: isinstance(v, str) and "•" in v  # noqa: E731

    url = data.get("url", old.get("url", ""))
    if masked(url):  # the hidden address shown in Settings stands for the saved one
        if not old:
            raise NotifyError("Enter the address.")
        url = old["url"]
    url = _line(url, "address", 2000)
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise NotifyError("The address must start with http:// or https://.")
    if kind == "ntfy":
        _ntfy_parts(url)
    if kind == "discord" and "/api/webhooks/" not in u.path:
        raise NotifyError("Use the webhook address from Discord: Server settings → Integrations → Webhooks → Copy URL.")

    token = data.get("token", old.get("token", ""))
    if masked(token):
        token = old.get("token", "")
    token = _line(token, "token", 500)
    if kind == "gotify" and not token:
        raise NotifyError("Gotify needs an application token: Gotify → Apps → Create application.")

    headers = []
    if kind == "webhook":
        saved = {h["name"].lower(): h["value"] for h in old.get("headers") or []}
        for h in (data.get("headers") if "headers" in data else old.get("headers")) or []:
            name = _line(h.get("name"), "header name", 64)
            if not name and not h.get("value"):
                continue
            if not _HEADER_NAME.match(name):
                raise NotifyError(f"“{name}” isn't a header name: letters, numbers and dashes only.")
            if name.lower() in _RESERVED:
                raise NotifyError(f"Narratarr sets {name} itself.")
            value = h.get("value", "")
            value = saved.get(name.lower(), "") if masked(value) else _line(value, "header value", 2000)
            headers.append({"name": name, "value": value})
        if len(headers) > 10:
            raise NotifyError("Ten headers at most.")

    body = ""
    if kind == "webhook":
        body = str(data.get("body", old.get("body", "")) or "").strip()
        if len(body) > 8000:
            raise NotifyError("The body is too long.")
        try:
            json.loads(render(body or DEFAULT_BODY, context("failed", SAMPLE, "someone", 'an "example" error')))
        except ValueError as e:
            raise NotifyError(f"The body isn't valid JSON once it's filled in ({e.msg}, line {e.lineno}).")

    events = [e for e in (data.get("events") if "events" in data else old.get("events")) or [] if e in EVENTS]
    if not events:
        raise NotifyError("Choose at least one event to be told about.")
    name = " ".join(str(data.get("name", old.get("name", "")) or "").split())[:60] or KINDS[kind]
    return {
        "name": name, "kind": kind, "url": url, "token": token, "headers": headers, "body": body,
        "events": list(dict.fromkeys(events)), "enabled": bool(data.get("enabled", old.get("enabled", True))),
    }


def create(data: dict) -> dict:
    items = load()
    if len(items) >= MAX_DESTINATIONS:
        raise NotifyError(f"{MAX_DESTINATIONS} destinations at most.")
    n = {"id": max((i["id"] for i in items), default=0) + 1, **clean(data), "created_at": time.time(),
         "last_at": None, "last_error": ""}
    _save([*items, n])
    return n


def update(nid: int, data: dict) -> dict | None:
    items = load()
    for i, n in enumerate(items):
        if n["id"] == nid:
            changed = clean(data, n)
            # A new address or kind means a new place: last time's result no longer applies.
            fresh = changed["url"] != n["url"] or changed["kind"] != n["kind"]
            items[i] = {**n, **changed, **({"last_at": None, "last_error": ""} if fresh else {})}
            _save(items)
            return items[i]
    return None


def delete(nid: int) -> bool:
    items = load()
    kept = [n for n in items if n["id"] != nid]
    _save(kept)
    return len(kept) != len(items)


async def test(nid: int, transport: httpx.AsyncBaseTransport | None = None) -> str:
    """Send a sample "added to Wanted" to one destination now. Returns "" or what went wrong."""
    n = get(nid)
    if n is None:
        raise NotifyError("That destination is gone.")
    error = await deliver(n, context("wanted", SAMPLE, "Narratarr (a test)"), transport)
    _record(nid, error)
    return error
