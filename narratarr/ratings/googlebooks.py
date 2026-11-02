"""Google Books ratings, from the documented API.

Wanted over Goodreads for three reasons: one request returns the average, the number of ratings and
the ISBNs, so finding the book and rating it collapse into a single call; the API is documented and
intended for this; and nothing about it is a breach for whoever runs the next release.

    https://www.googleapis.com/books/v1/volumes?q=intitle:<t>+inauthor:<a>&key=<KEY>

Without `GOOGLE_BOOKS_API_KEY` this provider is simply unconfigured: no request is made, no book is
queued, and the UI shows the empty slot it would show for a book with no rating. That is deliberate —
an unconfigured provider must not look like a broken one.
"""

from __future__ import annotations

import logging
import os
import re

import httpx

from . import make_rating
from .match import strict_match

log = logging.getLogger("narratarr.ratings.googlebooks")

ENDPOINT = "https://www.googleapis.com/books/v1/volumes"
TIMEOUT = httpx.Timeout(20, connect=8)
MAX_RESULTS = 5


def api_key() -> str:
    """Read at call time, not import time, so a key added to the environment is picked up."""
    return os.environ.get("GOOGLE_BOOKS_API_KEY", "").strip()


def best_volume(payload: dict, title: str, author: str) -> dict | None:
    """The one volume that is this book, or None.

    The API returns a ranked list, not an answer. Anything that is not an exact title-and-author
    match is dropped by `strict_match`, and a list where the right book is third is fine — a list
    where it is absent is None.
    """
    for item in (payload or {}).get("items", []) or []:
        info = item.get("volumeInfo") or {}
        volume_title = info.get("title") or ""
        subtitle = info.get("subtitle") or ""
        authors = info.get("authors") or []
        # "Etna: Book 2" is a different book from "Etna". A digit in the subtitle is how a series
        # marker announces itself, so such a volume is refused outright rather than retried as a
        # bare title — that retry is exactly how a series neighbour gets rated as its predecessor.
        if subtitle and re.search(r"\d", subtitle):
            continue
        full_title = f"{volume_title}: {subtitle}" if subtitle else volume_title
        if not strict_match(title, author, full_title, authors) and not strict_match(
            title, author, volume_title, authors
        ):
            continue
        count = info.get("ratingsCount")
        score = info.get("averageRating")
        if score is None:
            continue
        return {
            "score": float(score),
            "count": int(count or 0),
            "title": volume_title,
            "authors": authors,
            "isbn": _isbn(info.get("industryIdentifiers") or []),
            "url": info.get("canonicalVolumeLink") or info.get("infoLink") or "",
        }
    return None


def _isbn(identifiers: list[dict]) -> str:
    by_type = {i.get("type"): (i.get("identifier") or "") for i in identifiers}
    return by_type.get("ISBN_13") or by_type.get("ISBN_10") or ""


class GoogleBooksProvider:
    name = "googlebooks"
    label = "Google Books"

    def __init__(self, store, client: httpx.AsyncClient | None = None):
        self.store = store
        self._client = client

    @property
    def enabled(self) -> bool:
        return bool(api_key())

    # ---- page path: cache only -------------------------------------------------------------

    def cached(self, title: str, author: str) -> object | None:
        from .store import key_for

        row = self.store.get(key_for(title, author), self.name)
        if not row or row.get("state") != "rated":
            return None
        return make_rating(row["score"], row["count"] or 0, row["url"], self.name)

    # ---- background: the only place that fetches -------------------------------------------

    async def fetch_and_store(self, title: str, author: str):
        from .store import key_for

        if not self.enabled:
            return None
        key = key_for(title, author)
        payload = await self._search(title, author)
        if payload is None:
            return None  # a failed request says nothing about the book
        volume = best_volume(payload, title, author)
        if volume is None:
            self.store.put(key, self.name, state="unrated")
            return None
        rating = make_rating(volume["score"], volume["count"], volume["url"], self.name)
        if rating is None:
            self.store.put(key, self.name, state="unrated", score=volume["score"],
                           count=volume["count"], isbn=volume["isbn"])
            return None
        self.store.put(key, self.name, state="rated", score=rating.score, count=rating.count,
                       url=rating.url, isbn=volume["isbn"])
        return rating

    async def lookup(self, title: str, author: str) -> object | None:
        if not self.enabled:
            return None
        volume = best_volume(await self._search(title, author) or {}, title, author)
        return make_rating(volume["score"], volume["count"], volume["url"], self.name) if volume else None

    async def _search(self, title: str, author: str) -> dict | None:
        params = {"q": f'intitle:"{title}"' + (f'+inauthor:"{author}"' if author else ""),
                  "maxResults": MAX_RESULTS, "key": api_key()}
        try:
            if self._client is not None:
                resp = await self._client.get(ENDPOINT, params=params, timeout=TIMEOUT)
            else:
                async with httpx.AsyncClient(timeout=TIMEOUT) as c:
                    resp = await c.get(ENDPOINT, params=params)
        except httpx.HTTPError as e:
            log.info("Google Books unreachable: %s", e)
            return None
        if resp.status_code == 429:
            log.warning("Google Books is rate-limiting this instance (429). A key raises the quota.")
            return None
        if resp.status_code != 200:
            log.info("Google Books returned %s", resp.status_code)
            return None
        try:
            return resp.json()
        except ValueError:
            return None
