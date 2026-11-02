"""Audnex ratings: Audible's catalogue through the community mirror at api.audnex.us.

Keyed by ASIN, which is the one identifier we can trust: a book's Audible page is the book, so no
title matching is involved once the ASIN is known.

Two things the API does that shape this file:

* **Region is not optional in practice.** The same ASIN that answers with `?region=uk` returns 404
  for `?region=us` and 404 with no region at all, so the region is a setting and defaults to `uk`.
* **`rating` is a string, and `"0.0"` means "no ratings yet"** — not zero stars. It is the absence of
  a rating, and it must stay absent rather than becoming the worst score on the page.

There is no ratings count in the payload, which is why `Rating.count` stays None here.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

import httpx

from . import Rating, make_rating
from .match import normalise

log = logging.getLogger("narratarr.ratings.audnex")

AUDNEX = "https://api.audnex.us"
TIMEOUT = httpx.Timeout(20, connect=8)
REQUEST_GAP = 1.0
MAX_BACKOFF = 30.0
RATING_TTL = 30 * 24 * 3600      # scores barely move
ASIN_TTL = 0                     # a title-to-ASIN mapping, once found, does not change
MISS_TTL = 7 * 24 * 3600         # how long a "no ASIN for this title" is remembered

TLDS = {"us": "com", "uk": "co.uk", "ca": "ca", "au": "com.au", "in": "in",
        "de": "de", "fr": "fr", "it": "it", "es": "es", "jp": "co.jp"}

_last_request = 0.0
_lock: asyncio.Lock | None = None


class AudnexUnavailable(Exception):
    """The mirror did not answer. Says nothing about the book, so it is never cached."""


def parse_score(record: dict | None) -> float | None:
    """The rating as a number, or None.

    `"0.0"` is the API saying "nobody has rated this yet". Turning that into 0.0 would put an
    unrated book at the bottom of a rating sort and show it as half a star, which is a lie in both
    directions.
    """
    raw = (record or {}).get("rating")
    if raw is None:
        return None
    try:
        score = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if not 0.0 < score <= 5.0:
        return None
    return score


def audible_url(asin: str, region: str) -> str:
    return f"https://www.audible.{TLDS.get(region, 'com')}/pd/{asin}"


def surname(name: str) -> str:
    parts = (name or "").replace(",", " ").split()
    return parts[-1].lower() if parts else ""


class AudnexProvider:
    name = "audnex"
    label = "Audible"

    def __init__(self, store, client: httpx.AsyncClient | None = None, region: str | None = None):
        self.store = store
        self._client = client
        self._region = region

    @property
    def region(self) -> str:
        if self._region:
            return self._region
        from .. import config

        r = getattr(config.load(), "audnex_region", "uk") or "uk"
        return r if r in TLDS else "uk"

    @property
    def enabled(self) -> bool:
        return True

    # ---- page path: cache only -------------------------------------------------------------

    def cached(self, title: str, author: str, asin: str = "") -> Rating | None:
        asin = asin or self._known_asin(title, author)
        if not asin:
            return None
        row = self.store.get(asin, self.name, ttl=RATING_TTL)
        if not row or row.get("state") != "rated":
            return None
        return self._rating(row["score"], asin)

    def _known_asin(self, title: str, author: str) -> str:
        from .store import key_for

        row = self.store.get_resolved(key_for(title, author))
        return (row or {}).get("remote_id", "") if row else ""

    def _rating(self, score: float, asin: str) -> Rating | None:
        return make_rating(
            score, None, audible_url(asin, self.region), self.name,
            label="Audible",
            tooltip=f"{float(score):.1f} average on Audible (ASIN {asin})",
        )

    # ---- background: the only place that fetches -------------------------------------------

    async def fetch_and_store(self, title: str, author: str, asin: str = "") -> Rating | None:
        from .store import key_for

        key = key_for(title, author)
        if not asin:
            asin = await self._resolve_asin(title, author)
            if not asin:
                return None  # _resolve_asin has already recorded what it decided
        record = await self._get_book(asin)
        if record is None:
            return None  # unavailable, or a 404 that says nothing worth caching
        score = parse_score(record)
        if score is None:
            self.store.put(asin, self.name, state="unrated")  # exists, but unrated
            return None
        rating = self._rating(score, asin)
        self.store.put(asin, self.name, state="rated", score=score, url=rating.url if rating else "")
        return rating

    async def lookup(self, title: str, author: str, asin: str = "") -> Rating | None:
        asin = asin or await self._resolve_asin(title, author)
        if not asin:
            return None
        return self._rating(score, asin) if (score := parse_score(await self._get_book(asin))) else None

    # ---- title -> ASIN, through our own Audible source ---------------------------------------

    async def _resolve_asin(self, title: str, author: str) -> str:
        """Ask Audible for "<title> <author>" and take the first result only if it is this book.

        The title must match exactly once normalised, and the surname must match. Anything weaker is
        a miss: a wrong ASIN here becomes a confident score on the wrong book, and a blank card is
        the better failure.
        """
        from .. import audible
        from .store import key_for

        key = key_for(title, author)
        known = self.store.get_resolved(key)
        if known is not None:
            return known.get("remote_id", "")

        try:
            found, _ = await audible.search_books(f"{title} {author}".strip(), client=self._client)
        except Exception as e:  # noqa: BLE001 - the catalogue being down is not our answer to cache
            log.info("Audible search failed for %r: %s", title, e)
            return ""
        for book in found or []:
            # Strict equality here, not the subtitle-tolerant `title_matches`: chief's rule for this
            # hop is "the normalised title matches", and a subtitle is where a different edition —
            # or a different book — hides. A wrong ASIN becomes a confident score on the wrong book.
            if normalise(title) != normalise(book.get("title", "")):
                continue
            # audible.py flattens the contributor list into one "A, B" string.
            names = [n.strip() for n in (book.get("author") or "").split(",") if n.strip()]
            if author and not any(surname(author) == surname(n) for n in names):
                continue
            asin = book.get("asin") or ""
            if asin:
                self.store.put_resolved(key, remote_id=asin)  # permanent: a mapping does not drift
                return asin
        self.store.put_resolved(key)  # a remembered miss; get_resolved applies RETRY_AFTER
        return ""

    # ---- pacing ------------------------------------------------------------------------------

    def _regions_to_try(self) -> list[str]:
        """The configured region first, then the marketplace the ASIN actually came from.

        Measured: a region that does not match an ASIN's marketplace answers 404 for it, and the
        reverse is true of ASINs from the other catalogue. Since every ASIN we hold came out of
        `audible.py`, its region is a fact we have rather than a guess — so a 404 in the configured
        region is worth one retry there before calling the book unknown.
        """
        regions = [self.region]
        try:
            from .. import audible

            home = audible.region()
        except Exception:  # noqa: BLE001 - config unavailable: configured region only
            home = ""
        if home and home not in regions:
            regions.append(home)
        return regions

    async def _get_book(self, asin: str) -> dict | None:
        for region in self._regions_to_try():
            found = await self._get_book_in(asin, region)
            if found is not None:
                return found
        return None

    async def _get_book_in(self, asin: str, region: str) -> dict | None:
        global _last_request, _lock
        if _lock is None:
            _lock = asyncio.Lock()
        backoff = 2.0
        url = f"{AUDNEX}/books/{asin}"
        for _ in range(3):
            async with _lock:
                wait = REQUEST_GAP - (time.monotonic() - _last_request)
                if wait > 0:
                    await asyncio.sleep(wait)
                _last_request = time.monotonic()
            try:
                if self._client is not None:
                    resp = await self._client.get(url, params={"region": region}, timeout=TIMEOUT)
                else:
                    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
                        resp = await c.get(url, params={"region": region})
            except httpx.HTTPError as e:
                log.info("Audnex unreachable: %s", e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue
            if resp.status_code == 404:
                return None  # not in this region; the caller may try the ASIN's own
            if resp.status_code in (403, 429) or resp.status_code >= 500:
                await asyncio.sleep(backoff + random.random())
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue
            if resp.status_code != 200:
                return None
            try:
                return resp.json()
            except ValueError:
                raise AudnexUnavailable("not JSON")
        raise AudnexUnavailable("no answer after retries")
