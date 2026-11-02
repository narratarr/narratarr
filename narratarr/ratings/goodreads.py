"""Goodreads ratings, read from the book page's own structured data.

Only two URLs are used, both allowed by their robots.txt:

    /book/isbn/<isbn>     redirects to the canonical page for that ISBN
    /book/show/<id>       the canonical page

`/search` is disallowed and answers a redirect loop to anyone unauthenticated, so this never calls
it. Finding the book is the resolver's job (`resolver.py`), not a scrape.

Each page carries a `<script type="application/ld+json">` block with `@type: Book` holding
`ratingValue` and `ratingCount`, which is what we read — not the HTML around it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time

import httpx

from . import Rating, compact_count, make_rating
from .match import strict_match, title_matches
from .resolver import Resolver

log = logging.getLogger("narratarr.ratings.goodreads")

BASE = "https://www.goodreads.com"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
REQUEST_GAP = 1.0     # one request a second, as asked
MAX_BACKOFF = 60.0
TIMEOUT = httpx.Timeout(30, connect=10)

class Blocked(Exception):
    """Goodreads answered, but with a bot challenge rather than a page.

    A `202` with an empty body is what their mitigation serves once it has decided this client is a
    robot. It looks like an empty page, so without this it would be cached as "this book has no
    rating" and the book would stay unrated for a week.
    """


_LD = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)
_SHOW_ID = re.compile(r"/book/show/(\d+)")

_last_request = 0.0
_lock: asyncio.Lock | None = None


def parse_book(html: str) -> dict | None:
    """The `@type: Book` block from a page, or None.

    Goodreads ships several ld+json blocks (breadcrumbs, the book, sometimes reviews), so this
    looks for the one whose @type is Book rather than taking the first.
    """
    for block in _LD.findall(html or ""):
        try:
            data = json.loads(block.strip())
        except (ValueError, TypeError):
            continue
        for candidate in (data if isinstance(data, list) else [data]):
            if not isinstance(candidate, dict):
                continue
            if str(candidate.get("@type", "")).lower() != "book":
                continue
            aggregate = candidate.get("aggregateRating") or {}
            try:
                score = float(aggregate.get("ratingValue"))
            except (TypeError, ValueError):
                continue
            if not score:
                continue
            try:
                count = int(float(aggregate.get("ratingCount") or 0))
            except (TypeError, ValueError):
                count = 0
            author = candidate.get("author")
            if isinstance(author, dict):
                author = [author.get("name") or ""]
            elif isinstance(author, list):
                author = [(a.get("name") if isinstance(a, dict) else str(a)) for a in author]
            else:
                author = [str(author)] if author else []
            return {
                "score": score,
                "count": count,
                "name": (candidate.get("name") or "").strip(),
                "authors": [a for a in author if a],
                "isbn": (candidate.get("isbn") or "").strip(),
            }
    return None


def book_id(url: str) -> str:
    m = _SHOW_ID.search(url or "")
    return m.group(1) if m else ""


def _rating(score: float, count: int, url: str, parsed: dict, title: str, author: str) -> Rating | None:
    """Only a page whose own title (and author, when it states one) is this book may score it."""
    if not title_matches(title, parsed.get("name", "")):
        return None
    if parsed.get("authors") and not strict_match(title, author, parsed.get("name", ""), parsed["authors"]):
        return None
    return make_rating(score, count, url, "goodreads")


class GoodreadsProvider:
    """Unofficial. Never the default.

    Goodreads' Terms of Use forbid collecting their listings with extraction tools, and robots.txt
    disallows their search outright. This provider therefore exists for someone who has read that
    and decided for themselves; it is not the resolver of record, it is not enabled by default, and
    its pages are only ever reached through an ISBN we already resolved.
    """

    name = "goodreads"
    label = "Goodreads (unofficial)"

    def __init__(self, store, client: httpx.AsyncClient | None = None, gap: float = REQUEST_GAP):
        self.store = store
        self._client = client
        self.gap = gap
        self._resolver = Resolver(store, client=client, gap=gap)

    @property
    def enabled(self) -> bool:
        return True

    # ---- page path: cache only -------------------------------------------------------------

    def state(self, title: str, author: str, asin: str = "") -> str:
        return self.store.state(self._key(title, author), self.name)

    def cached(self, title: str, author: str, asin: str = "") -> Rating | None:
        row = self.store.get(self._key(title, author), self.name)
        if not row or row.get("state") != "rated":
            return None
        return make_rating(row["score"], row["count"] or 0, row["url"], "goodreads")

    def _key(self, title: str, author: str) -> str:
        from .store import key_for

        return key_for(title, author)

    # ---- background: the only place that fetches -------------------------------------------

    async def fetch_and_store(self, title: str, author: str, asin: str = "") -> Rating | None:
        key = self._key(title, author)
        hit = await self._resolver.resolve(title, author)
        if not hit:
            self.store.put(key, self.name, state="unrated")
            return None

        try:
            html, final_url = await self._get(hit["url"])
        except Blocked as e:
            log.warning("Goodreads is not answering with pages any more (%s); leaving %r unrated "
                        "rather than recording it as having no rating", e, title)
            return None  # deliberately not cached: this says nothing about the book
        if html is None:
            self.store.put(key, self.name, state="unrated", isbn=hit.get("isbn", ""))
            return None

        parsed = parse_book(html)
        if not parsed:
            self.store.put(key, self.name, state="unrated", isbn=hit.get("isbn", ""))
            return None

        rating = _rating(parsed["score"], parsed["count"], final_url or hit["url"], parsed, title, author)
        if rating is None:  # real, but too few votes: remember it so we stop asking
            self.store.put(
                key, self.name, state="unrated", isbn=hit.get("isbn", ""),
                remote_id=book_id(final_url or hit["url"]), score=parsed["score"], count=parsed["count"],
            )
            return None

        self.store.put(
            key, self.name, state="rated", isbn=hit.get("isbn", ""),
            remote_id=book_id(rating.url), score=rating.score, count=rating.count, url=rating.url,
        )
        return rating

    async def lookup(self, title: str, author: str, isbn: str | None = None) -> Rating | None:
        """A single book, now. Used by the tests and by anything that wants an answer inline."""
        hit = await self._resolver.resolve(title, author, isbn=isbn)
        if not hit:
            return None
        html, final_url = await self._get(hit["url"])
        parsed = parse_book(html or "")
        if not parsed:
            return None
        return _rating(parsed["score"], parsed["count"], final_url or hit["url"], parsed, title, author)

    async def _get(self, url: str) -> tuple[str | None, str]:
        global _last_request, _lock
        if _lock is None:
            _lock = asyncio.Lock()
        backoff = 2.0
        for attempt in range(4):
            async with _lock:  # one request at a time, at the configured pace
                wait = self.gap - (time.monotonic() - _last_request)
                if wait > 0:
                    await asyncio.sleep(wait)
                _last_request = time.monotonic()
            try:
                if self._client is not None:
                    resp = await self._client.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT,
                                                  follow_redirects=True)
                else:
                    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as c:
                        resp = await c.get(url, headers={"User-Agent": UA})
            except httpx.HTTPError:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue
            if resp.status_code in (403, 429):  # asked to slow down; wait longer each time
                await asyncio.sleep(backoff + random.random())
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue
            if resp.status_code == 202 and not resp.content:
                # The bot challenge: a page-shaped no. Retrying within this call never helps.
                raise Blocked("202 with an empty body")
            if resp.status_code != 200:
                raise Blocked("no answer after retries")
            return resp.text, str(resp.url)
        raise Blocked("no answer after retries")
