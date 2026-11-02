"""Finding the Goodreads page for a title, in order of how much it can be trusted.

1. **ISBN** — ask Open Library for the book's ISBN, then use `/book/isbn/<isbn>`, which redirects to
   the canonical page. This is the only step that can be *right* rather than merely plausible.
2. **Local** — a book already in the cache with a matching title and author, which costs nothing
   and cannot be wrong about a book we have already seen.

There used to be a Wikidata hop between those two. It was removed: a third way to be wrong, for
little gain, and every extra step is another chance to confidently rate the wrong novel.

Goodreads' own search is not used: it is disallowed by their robots.txt and answers a redirect
loop to anyone unauthenticated. Every result is cached, including dead ends, so a book is never
resolved twice.
"""

from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import quote

import httpx

from .match import strict_match

log = logging.getLogger("narratarr.ratings.resolver")

OPENLIBRARY = "https://openlibrary.org/search.json"
UA = "Narratarr/0.0 (self-hosted audiobook manager; ratings resolver)"
TIMEOUT = httpx.Timeout(20, connect=8)

_last_request = 0.0
_lock: asyncio.Lock | None = None


class Unavailable(Exception):
    """A lookup that could not be made, as opposed to one that was made and found nothing.

    The difference matters: caching "could not ask" as "no such book" turns a flaky minute at Open
    Library into a week of missing ratings for every book searched during it.
    """


class Resolver:
    def __init__(self, store, client: httpx.AsyncClient | None = None, gap: float = 1.0):
        self.store = store
        self._client = client
        self.gap = gap

    async def resolve(self, title: str, author: str, isbn: str | None = None) -> dict | None:
        """{'url','isbn','via'} for the book, or None. Cached, including dead ends."""
        from .store import key_for

        key = key_for(title, author)
        cached = self.store.get_resolved(key)
        if cached:
            if cached.get("remote_id"):
                return {"url": f"https://www.goodreads.com/book/show/{cached['remote_id']}",
                        "isbn": cached.get("isbn", ""), "via": "cache"}
            if cached.get("isbn"):
                return {"url": f"https://www.goodreads.com/book/isbn/{cached['isbn']}",
                        "isbn": cached["isbn"], "via": "cache"}
            return None

        found = None
        unavailable = False
        if isbn:  # handed to us: no resolving needed
            found = {"url": f"https://www.goodreads.com/book/isbn/{isbn}", "isbn": isbn, "via": "given"}
        if not found:
            try:
                found = await self._by_isbn(title, author)
            except Unavailable:
                unavailable = True
        if not found:
            found = self._by_local(title, author)

        if found or not unavailable:
            # Only a definite answer — or a definite nothing — is worth remembering.
            self.store.put_resolved(key, isbn=(found or {}).get("isbn", ""),
                                    remote_id=(found or {}).get("remote_id", ""))
        return found

    # ---- 1. Open Library --------------------------------------------------------------------

    async def _by_isbn(self, title: str, author: str) -> dict | None:
        params = {"title": title, "limit": 5, "fields": "title,author_name,isbn"}
        if author:
            params["author"] = author
        data = await self._get_json(OPENLIBRARY, params)
        for doc in (data or {}).get("docs", []):
            if not strict_match(title, author, doc.get("title", ""), doc.get("author_name") or []):
                continue
            isbns = [i for i in (doc.get("isbn") or []) if i.replace("X", "").isdigit()]
            if not isbns:
                continue
            thirteen = [i for i in isbns if len(i) == 13]
            isbn = (thirteen or isbns)[0]
            return {"url": f"https://www.goodreads.com/book/isbn/{isbn}", "isbn": isbn, "via": "openlibrary"}
        return None

    # ---- 2. what we have already seen --------------------------------------------------------

    def _by_local(self, title: str, author: str) -> dict | None:
        """A book resolved earlier for a different spelling of the same title costs nothing to reuse."""
        try:
            rows = self.store._con().execute(
                "SELECT key, isbn, remote_id FROM resolved WHERE remote_id != '' OR isbn != ''"
            ).fetchall()
        except Exception:
            return None
        for row in rows:
            other_title, _, other_author = row["key"].partition("|")
            if strict_match(title, author, other_title, other_author):
                if row["remote_id"]:
                    return {"url": f"https://www.goodreads.com/book/show/{row['remote_id']}",
                            "isbn": row["isbn"], "remote_id": row["remote_id"], "via": "local"}
                return {"url": f"https://www.goodreads.com/book/isbn/{row['isbn']}",
                        "isbn": row["isbn"], "via": "local"}
        return None

    # ---- pacing -----------------------------------------------------------------------------

    async def _get_json(self, url: str, params: dict) -> dict | None:
        global _last_request, _lock
        if _lock is None:
            _lock = asyncio.Lock()
        async with _lock:
            wait = self.gap - (time.monotonic() - _last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            _last_request = time.monotonic()
        try:
            if self._client is not None:
                resp = await self._client.get(url, params=params, headers={"User-Agent": UA}, timeout=TIMEOUT)
            else:
                async with httpx.AsyncClient(timeout=TIMEOUT) as c:
                    resp = await c.get(url, params=params, headers={"User-Agent": UA})
        except httpx.HTTPError as e:
            log.info("resolver: %s unreachable (%s)", url, e)
            raise Unavailable(str(e))
        if resp.status_code >= 500 or resp.status_code == 429:
            raise Unavailable(f"{resp.status_code} from {url}")
        if resp.status_code != 200:
            return None  # a real "no", e.g. 404 for an unknown id
        try:
            return resp.json()
        except ValueError:
            raise Unavailable("not JSON")


__all__ = ["Resolver", "quote"]
