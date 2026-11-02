"""Finding the Goodreads page for a title, in order of how much it can be trusted.

1. **ISBN** — ask Open Library for the book's ISBN, then use `/book/isbn/<isbn>`, which redirects to
   the canonical page. This is the only step that can be *right* rather than merely plausible.
2. **Wikidata** — Goodreads ids are a property there (P2969), so this is a lookup, not a guess.
3. **Local** — a book already in the cache with a matching title and surname, which costs nothing
   and cannot be wrong about a book we have already seen.

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

log = logging.getLogger("narratarr.ratings.resolver")

OPENLIBRARY = "https://openlibrary.org/search.json"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
UA = "Narratarr/0.0 (self-hosted audiobook manager; ratings resolver)"
TIMEOUT = httpx.Timeout(20, connect=8)
GOODREADS_ID = "P2969"  # the Wikidata property that holds a Goodreads book id

_last_request = 0.0
_lock: asyncio.Lock | None = None


class Unavailable(Exception):
    """A lookup that could not be made, as opposed to one that was made and found nothing.

    The difference matters: caching "could not ask" as "no such book" turns a flaky minute at Open
    Library into a week of missing ratings for every book searched during it.
    """


def _surname(name: str) -> str:
    parts = (name or "").replace(",", " ").split()
    return parts[-1].lower() if parts else ""


def _looks_like(title: str, author: str, other_title: str, other_author: str) -> bool:
    """The same surname gate the Bookmarks matcher uses, so the two agree on what "same book" means."""
    from rapidfuzz.fuzz import token_sort_ratio

    if author and other_author and _surname(author) != _surname(other_author):
        return False
    return token_sort_ratio((title or "").lower(), (other_title or "").lower()) >= 88


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
            try:
                found = await self._by_wikidata(title, author)
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
            if not _looks_like(title, author, doc.get("title", ""),
                               (doc.get("author_name") or [""])[0]):
                continue
            isbns = [i for i in (doc.get("isbn") or []) if i.replace("X", "").isdigit()]
            if not isbns:
                continue
            thirteen = [i for i in isbns if len(i) == 13]
            isbn = (thirteen or isbns)[0]
            return {"url": f"https://www.goodreads.com/book/isbn/{isbn}", "isbn": isbn, "via": "openlibrary"}
        return None

    # ---- 2. Wikidata ------------------------------------------------------------------------

    async def _by_wikidata(self, title: str, author: str) -> dict | None:
        search = await self._get_json(
            WIKIDATA_API,
            {"action": "wbsearchentities", "search": f"{title} {author}".strip(),
             "language": "en", "format": "json", "limit": 5, "type": "item"},
        )
        for hit in (search or {}).get("search", []):
            qid = hit.get("id")
            if not qid:
                continue
            entity = await self._get_json(
                WIKIDATA_API,
                {"action": "wbgetentities", "ids": qid, "props": "claims", "format": "json"},
            )
            claims = (((entity or {}).get("entities") or {}).get(qid) or {}).get("claims") or {}
            for claim in claims.get(GOODREADS_ID, []):
                try:
                    value = claim["mainsnak"]["datavalue"]["value"]
                except (KeyError, TypeError):
                    continue
                if isinstance(value, str) and value.strip():
                    remote = value.strip()
                    return {"url": f"https://www.goodreads.com/book/show/{remote}",
                            "isbn": "", "remote_id": remote, "via": "wikidata"}
        return None

    # ---- 3. what we have already seen --------------------------------------------------------

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
            if _looks_like(title, author, other_title, other_author):
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
