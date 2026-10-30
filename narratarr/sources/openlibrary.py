"""Open Library as a Discover tab: books that aren't on Audible, mostly for ebooks.

Like the Audible catalog, this is only where book details come from. Wanting a book
adds it to Wanted, and Narratarr then looks for it on your indexers (see wanted.py).
Free, no account. API: https://openlibrary.org/developers/api
"""

from __future__ import annotations

import asyncio
import re

import httpx

from .. import updates
from ..cache import Cache
from .base import Category, Details, Page

BASE = "https://openlibrary.org"
COVERS = "https://covers.openlibrary.org/b/id"
TIMEOUT = httpx.Timeout(20, connect=8)
PER_PAGE = 40
FIELDS = "key,title,subtitle,author_name,cover_i,first_publish_year,number_of_pages_median"
SUBJECTS = [
    ("fantasy", "Fantasy"),
    ("science_fiction", "Science fiction"),
    ("mystery_and_detective_stories", "Mystery"),
    ("thriller", "Thriller"),
    ("romance", "Romance"),
    ("historical_fiction", "Historical fiction"),
    ("biography", "Biography"),
    ("history", "History"),
    ("science", "Science"),
    ("business", "Business"),
    ("young_adult_fiction", "Young adult"),
    ("classics", "Classics"),
]
_WORK = re.compile(r"^https://openlibrary\.org/works/(OL\d+W)(?:[/?#].*)?$")
_KNOCKOFF = re.compile(r"\b(summary|study guide|analysis of|workbook for)\b", re.I)

_cache: Cache = Cache(1800, 300)


def work_url(key: str) -> str:
    return f"{BASE}/works/{key.rsplit('/', 1)[-1]}"


def work_id(url: str) -> str:
    m = _WORK.match(url or "")
    return m.group(1) if m else ""


def cover(cover_id) -> str:
    return f"{COVERS}/{int(cover_id)}-L.jpg" if isinstance(cover_id, int) and cover_id > 0 else ""


def _description(raw) -> str:
    text = raw.get("value", "") if isinstance(raw, dict) else raw if isinstance(raw, str) else ""
    text = re.split(r"\n\s*-{4,}", text)[0]  # "See also" link lists follow a rule
    text = re.sub(r"\[([^\]]+)\]\[\d+\]", r"\1", text)  # [Book][1] -> Book
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1", text)  # [Book](url) -> Book
    text = re.sub(r"(?m)^\s*\[\d+\]:\s*\S+\s*$", "", text)
    paras = [" ".join(p.split()) for p in re.split(r"\r?\n\s*\r?\n", text.replace("*", ""))]
    return "\n\n".join(p for p in paras if p)[:5000]


def to_details(doc: dict, description: str = "") -> Details:
    """A work from search, trending or a subject list. They spell some fields differently."""
    authors = doc.get("author_name") or [a.get("name", "") for a in doc.get("authors") or [] if isinstance(a, dict)]
    year = doc.get("first_publish_year")
    pages = doc.get("number_of_pages_median")
    return Details(
        url=work_url(doc.get("key") or ""),
        title=doc.get("title") or "Untitled",
        author=", ".join(a for a in authors[:3] if a),
        cover_url=cover(doc.get("cover_i") or doc.get("cover_id")),
        posted=str(year) if isinstance(year, int) else "",
        description=description or (doc.get("subtitle") or ""),
        length=f"{pages} pages" if isinstance(pages, int) and pages > 0 else "",
    )


async def _get(path: str, params: dict | None = None, client: httpx.AsyncClient | None = None):
    key = (path, tuple(sorted((params or {}).items())))
    hit = _cache.get(key)
    if hit is not None:
        return hit
    headers = {"User-Agent": f"Narratarr/{updates.current_version()} (+https://github.com/narratarr/narratarr)"}
    if client:
        resp = await client.get(f"{BASE}{path}", params=params, headers=headers)
    else:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as c:
            resp = await c.get(f"{BASE}{path}", params=params, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise httpx.DecodingError("Open Library sent something unexpected.")
    _cache.set(key, data)
    return data


def _keep(docs: list) -> list[dict]:
    """Real books only: something with a title and an author, and no summaries of other people's books."""
    return [d for d in docs if isinstance(d, dict) and str(d.get("key", "")).startswith("/works/")
            and d.get("title") and not _KNOCKOFF.search(d["title"])
            and (d.get("author_name") or d.get("authors"))]


class OpenLibrary:
    name = "openlibrary"
    label = "Open Library"
    description = "Find books that aren’t on Audible, mostly for ebooks, and add them to Wanted."
    default_label = "Trending today"
    delivery = "wanted"

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client

    def owns(self, url: str) -> bool:
        return bool(work_id(url))

    def categories(self) -> list[Category]:
        return [Category(slug, name) for slug, name in SUBJECTS]

    async def latest(self, page: int = 1, category: str | None = None) -> Page:
        if category in dict(SUBJECTS):
            data = await _get(f"/subjects/{category}.json", {"limit": PER_PAGE, "offset": (page - 1) * PER_PAGE},
                              self._client)
            total = -(-int(data.get("work_count") or 0) // PER_PAGE)
        else:
            data = await _get("/trending/daily.json", {"limit": PER_PAGE, "page": page}, self._client)
            total = 5
        works = _keep(data.get("works") or [])
        return Page(items=[to_details(w) for w in works], page=page, total_pages=max(1, min(total, 25)))

    async def search(self, query: str, page: int = 1) -> Page:
        data = await _get("/search.json", {"q": query, "fields": FIELDS, "limit": PER_PAGE, "page": page}, self._client)
        total = -(-int(data.get("numFound") or 0) // PER_PAGE)
        return Page(items=[to_details(d) for d in _keep(data.get("docs") or [])], page=page,
                    total_pages=max(1, min(total, 10)))

    async def details(self, url: str) -> Details:
        wid = work_id(url)
        if not wid:
            raise ValueError("That isn't an Open Library book.")
        work, found = await asyncio.gather(
            _get(f"/works/{wid}.json", None, self._client),
            _get("/search.json", {"q": f"key:/works/{wid}", "fields": FIELDS, "limit": 1}, self._client),
        )
        docs = found.get("docs") or []
        doc = docs[0] if docs else {"key": f"/works/{wid}", "title": work.get("title") or ""}
        d = to_details(doc, _description(work.get("description")))
        if not d.cover_url and work.get("covers"):
            d.cover_url = cover(next((c for c in work["covers"] if isinstance(c, int) and c > 0), 0))
        subjects = [s for s in work.get("subjects") or [] if isinstance(s, str) and ":" not in s]
        d.categories = subjects[:4]
        return d
