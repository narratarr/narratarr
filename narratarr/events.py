"""Server-sent events: a version counter for the library, and the stream that carries it.

The library used to be polled from every open tab every two seconds — about 43,000 requests a day
against a shelf that changes a handful of times — which is what issue #23 is about. The server now
pushes instead, and every message carries a version so a client that lost the connection can tell
whether it missed anything.

The version is a plain integer bumped by db.py on every write to a book or to someone's place in
one. Writes happen on worker threads, so the counter is deliberately the dumbest thing that works:
reading and incrementing an int needs no lock, no queue and no event loop handle. Each open stream
ticks four times a second and sends a frame when the number has moved.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

# Seconds between heartbeat comments. Proxies (nginx, Nginx Proxy Manager) close a connection that
# has been silent for a while; a comment line keeps it alive without the client doing anything.
HEARTBEAT_SECONDS = 20

# How often one open stream looks at the counter. Sets the worst-case delay between a book being
# filed and the shelf showing it, and replaces the two-second poll with half a second.
# ponytail: one timer per open stream, not a subscriber registry. A handful of tabs is nothing;
# if hundreds ever connect, swap this for queues fed by bump() via call_soon_threadsafe.
TICK_SECONDS = 0.5

_state = {"version": 0}


def library_version() -> int:
    return _state["version"]


def bump() -> int:
    """A book or someone's place in one changed. Called from db.py writes, on any thread."""
    _state["version"] += 1
    return _state["version"]


def library_etag() -> str:
    return f'W/"{library_version()}"'


def frame(kind: str, **fields) -> str:
    """One SSE message. Compact JSON, because this text goes down the wire every time."""
    return f"data: {json.dumps({'type': kind, **fields}, separators=(',', ':'))}\n\n"


async def stream() -> AsyncIterator[str]:
    seen = library_version()
    # Straight away, before anything changes: what the server has now. A client that reconnected
    # compares this with what it rendered, which is what makes a dropped connection safe.
    yield frame("library_changed", version=seen)
    loop = asyncio.get_running_loop()
    last = loop.time()
    while True:
        await asyncio.sleep(TICK_SECONDS)
        now = loop.time()
        version = library_version()
        if version != seen:
            seen = version
            last = now
            yield frame("library_changed", version=version)
        elif now - last >= HEARTBEAT_SECONDS:
            last = now
            yield ": ping\n\n"
