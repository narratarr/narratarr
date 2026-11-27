"""Fetch indexer-hosted images through Narratarr so the browser never talks to the indexer.

Issue #24: cover art was linked straight at the indexer's image host
(i.omgwtfnzbs.org and friends), so a reader's own address was handed to a third party on every
Discover page. The indexer already knows Narratarr's address; it has no business knowing the
reader's.

This is a proxy with an allowlist, not a general fetcher. The allowlist is derived from the
indexer URLs an admin has already configured, so Narratarr can only be asked to fetch hosts it was
already talking to. A URL on any other host is returned unchanged, and the browser fetches it
exactly as it does today.

ponytail: in-memory cache, 128 entries, 24 hours. A cover is a few tens of KB and the same art is
requested on every page, so this removes the repeat traffic that matters. A disk cache is the next
step if restarts start showing cold-page flicker; nothing else about this design changes.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import TYPE_CHECKING
from urllib.parse import quote, urlparse

import httpx

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import Settings

MAX_BYTES = 5 * 1024 * 1024
MAX_ENTRIES = 128
TTL_SECONDS = 24 * 60 * 60
MAX_HOPS = 3
TIMEOUT = 10.0
IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif", "image/avif")

# Test seam: a handler here replaces the network without touching the code under test.
_TRANSPORT: httpx.AsyncBaseTransport | None = None

_CACHE: OrderedDict[str, tuple[bytes, str, float]] = OrderedDict()


class NotAllowed(Exception):
    """The host is not one this Narratarr is configured to use."""


class Failed(Exception):
    """The fetch failed, or what came back was not an image."""


def host_of(url: str) -> str:
    """Hostname, lowercased, without a port or credentials."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    return (parsed.hostname or "").lower()


# Sources whose results are indexers. Art from these is what issue #24 is about: the indexer
# hands back a poster URL on its own image host, which is a different host to the indexer API.
INDEXER_SOURCES = ("prowlarr", "torznab", "audiobookbay")

# Hosts an indexer has actually served art from, observed when we rewrite one of its URLs.
# Trust-on-first-use, bounded and expiring: an address can only be proxied if some indexer we
# are configured for has already pointed at it, which is what stops this being an open proxy.
_OBSERVED: OrderedDict[str, float] = OrderedDict()
OBSERVED_TTL = 7 * 24 * 60 * 60
MAX_OBSERVED = 64


def observe(url: str) -> None:
    host = host_of(url)
    if not host:
        return
    _OBSERVED[host] = time.time()
    _OBSERVED.move_to_end(host)
    while len(_OBSERVED) > MAX_OBSERVED:
        _OBSERVED.popitem(last=False)


def _observed_hosts() -> set[str]:
    now = time.time()
    for host, seen in list(_OBSERVED.items()):
        if now - seen > OBSERVED_TTL:
            _OBSERVED.pop(host, None)
    return set(_OBSERVED)


def allowed_hosts(settings: Settings | None = None) -> set[str]:
    """Hosts that may be proxied: configured indexers, plus hosts an indexer pointed us at."""
    if settings is None:
        from . import config

        settings = config.load()
    hosts = {
        host_of(getattr(settings, "prowlarr_url", "")),
        host_of(getattr(settings, "torznab_url", "")),
        host_of(getattr(settings, "abb_base_url", "")),
    }
    return {h for h in hosts if h} | _observed_hosts()


def proxied(
    url: str, settings: Settings | None = None, *, from_indexer: bool = False
) -> str:
    """The proxy path for an allowlisted image URL, otherwise the URL unchanged.

    `from_indexer` says the URL came back in an indexer's own response. That is what makes the
    indexer's separate image host (i.omgwtfnzbs.org and friends) proxied as well as the indexer
    API host, without letting a caller name an arbitrary address.
    """
    if not url or not url.startswith(("http://", "https://")):
        return url
    host = host_of(url)
    if not host:
        return url
    if host in allowed_hosts(settings):
        return f"/api/image?url={quote(url, safe='')}"
    if from_indexer:
        observe(url)
        return f"/api/image?url={quote(url, safe='')}"
    return url


def _cached(url: str) -> tuple[bytes, str] | None:
    hit = _CACHE.get(url)
    if not hit:
        return None
    body, content_type, stored_at = hit
    if time.time() - stored_at > TTL_SECONDS:
        _CACHE.pop(url, None)
        return None
    _CACHE.move_to_end(url)
    return body, content_type


def _store(url: str, body: bytes, content_type: str) -> None:
    _CACHE[url] = (body, content_type, time.time())
    _CACHE.move_to_end(url)
    while len(_CACHE) > MAX_ENTRIES:
        _CACHE.popitem(last=False)


def clear_cache() -> None:
    _CACHE.clear()


async def fetch(url: str, settings: Settings | None = None) -> tuple[bytes, str]:
    """Return (bytes, content_type) for an allowlisted image URL.

    Redirects are followed by hand, one hop at a time, so a redirect cannot smuggle the fetch onto
    a host that is not on the allowlist.
    """
    allowed = allowed_hosts(settings)
    if not url.startswith(("http://", "https://")):
        raise NotAllowed("Not an http(s) URL.")
    if host_of(url) not in allowed:
        raise NotAllowed("That host is not one Narratarr is configured to use.")

    cache_key = url
    hit = _cached(cache_key)
    if hit:
        return hit

    current = url
    async with httpx.AsyncClient(
        timeout=TIMEOUT, follow_redirects=False, transport=_TRANSPORT
    ) as client:
        for _ in range(MAX_HOPS + 1):
            try:
                async with client.stream("GET", current) as response:
                    if response.is_redirect:
                        target = str(response.headers.get("location", ""))
                        if host_of(target) not in allowed:
                            raise NotAllowed("That image redirected off the allowlist.")
                        if not target.startswith(("http://", "https://")):
                            from urllib.parse import urljoin

                            target = urljoin(current, target)
                        current = target
                        continue
                    if response.status_code != 200:
                        raise Failed(f"The image host answered {response.status_code}.")
                    content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
                    if content_type not in IMAGE_TYPES:
                        raise Failed("That URL did not return an image.")
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > MAX_BYTES:
                            raise Failed("That image is too large to proxy.")
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    _store(cache_key, body, content_type)
                    return body, content_type
            except httpx.HTTPError as e:
                raise Failed(str(e) or "The image host could not be reached.") from e

    raise Failed("Too many redirects.")
