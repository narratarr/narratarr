"""Audible's public catalog: book details, authors and series, for Wanted books and following.

Uses the same read-only catalog endpoints as Audiobookshelf's Audible provider,
plus Audnexus (api.audnex.us) to look authors up by name. No account needed.
"""

from __future__ import annotations

import asyncio
import re

import httpx

from . import config
from .cache import Cache

TLDS = {
    "us": "com", "uk": "co.uk", "ca": "ca", "au": "com.au", "in": "in",
    "de": "de", "fr": "fr", "it": "it", "es": "es", "jp": "co.jp",
}
LANGUAGES = {"de": "german", "fr": "french", "it": "italian", "es": "spanish", "jp": "japanese"}
GROUPS = "contributors,media,product_attrs,product_desc,product_extended_attrs,series"
AUDNEXUS = "https://api.audnex.us"
TIMEOUT = httpx.Timeout(20, connect=8)
CACHE_SECONDS = 1800
BOOK_TYPES = {"SinglePartBook", "MultiPartBook"}

_cache: Cache = Cache(CACHE_SECONDS, 400)


class AudibleError(Exception):
    pass


def region() -> str:
    r = config.load().audible_region
    return r if r in TLDS else "us"


def api_base(r: str | None = None) -> str:
    return f"https://api.audible.{TLDS[r or region()]}/1.0/catalog"


def book_url(asin: str, r: str | None = None) -> str:
    return f"https://www.audible.{TLDS[r or region()]}/pd/{asin}"


def asin_from_url(url: str) -> str:
    m = re.search(r"audible\.[a-z.]+/pd/(?:[^/]+/)?([A-Z0-9]{10})", url or "")
    return m.group(1) if m else ""


def _plain(html: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>|</p>", "\n", html or "")
    text = re.sub(r"<[^>]+>", "", text)
    paras = [" ".join(p.split()) for p in text.split("\n")]
    return "\n\n".join(p for p in paras if p)


def _runtime(minutes) -> str:
    m = int(minutes or 0)
    if not m:
        return ""
    h, m = divmod(m, 60)
    unit = lambda n, word: f"{n} {word}{'' if n == 1 else 's'}"  # noqa: E731
    return f"{unit(h, 'hour')} and {unit(m, 'minute')}" if h else unit(m, "minute")


def to_book(p: dict, r: str | None = None) -> dict:
    images = p.get("product_images") or {}
    cover = images.get("500") or next(iter(images.values()), "")
    title = p.get("title") or ""
    series = [
        {"asin": s.get("asin", ""), "title": s.get("title", ""), "sequence": s.get("sequence", "")}
        for s in p.get("series") or []
    ]
    return {
        "asin": p.get("asin", ""),
        "url": book_url(p.get("asin", ""), r),
        "title": title,
        "subtitle": p.get("subtitle") or "",
        "author": ", ".join(a["name"] for a in p.get("authors") or [] if a.get("name")),
        "author_asins": [a.get("asin") for a in p.get("authors") or [] if a.get("asin")],
        "narrators": [n["name"] for n in p.get("narrators") or [] if n.get("name")],
        "cover_url": re.sub(r"\._SL\d+_", "._SL500_", cover),
        "description": _plain(p.get("publisher_summary") or ""),
        "released": p.get("release_date") or "",
        "length": _runtime(p.get("runtime_length_min")),
        "minutes": int(p.get("runtime_length_min") or 0),
        "language": (p.get("language") or "").title(),
        "series": series,
        "kind": p.get("content_delivery_type") or "",
        "abridged": (p.get("format_type") or "") == "abridged",
    }


async def _get(url: str, params: dict | None = None, client: httpx.AsyncClient | None = None):
    key = url + repr(sorted((params or {}).items()))
    hit = _cache.get(key)
    if hit is not None:
        return hit
    try:
        if client:
            resp = await client.get(url, params=params)
        else:
            async with httpx.AsyncClient(timeout=TIMEOUT) as c:
                resp = await c.get(url, params=params)
    except httpx.HTTPError:
        raise AudibleError("Couldn't reach Audible's catalog. Try again in a moment.")
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise AudibleError(f"Audible's catalog returned an error ({resp.status_code}).")
    data = resp.json()
    _cache.set(key, data)
    return data


def _wanted_language(b: dict, r: str) -> bool:
    want = LANGUAGES.get(r, "english")
    return not b["language"] or b["language"].lower() == want


_EDITION = re.compile(
    r"\s*(\([^)]*\)|\[[^\]]*\]|:\s*[^:]*\b(annotated|unabridged|edition|illustrated|classics?|version|"
    r"retold|abridged)\b.*)$", re.I,
)


def edition_free(title: str) -> str:
    """'The Call of the Wild (Annotated)' and 'The Call of the Wild: Classic Edition' are the same book."""
    prev = None
    while prev != title:
        prev, title = title, _EDITION.sub("", title).strip()
    return title or prev


def _keep(books: list[dict], r: str) -> list[dict]:
    """Books only (no podcasts), in the region's language, one edition per title."""
    out, seen = [], set()
    for b in books:
        if b["kind"] not in BOOK_TYPES or not _wanted_language(b, r):
            continue
        key = re.sub(r"\W+", " ", edition_free(b["title"]).lower()).strip()
        if key in seen:
            continue
        seen.add(key)
        out.append(b)
    return out


# Audible numbers result pages from 0; Narratarr's pages start at 1, so every request sends page - 1.


async def search_books(query: str, page: int = 1, client=None) -> tuple[list[dict], int]:
    r = region()
    params = {
        "keywords": query, "num_results": 40, "page": page - 1, "products_sort_by": "Relevance", "response_groups": GROUPS,
    }
    data = await _get(f"{api_base(r)}/products", params, client) or {}
    total = int(data.get("total_results") or 0)
    return _keep([to_book(p, r) for p in data.get("products") or []], r), max(1, min(10, -(-total // 40)))


async def products(asins: list[str], client=None) -> list[dict]:
    r = region()
    out = []
    for i in range(0, len(asins), 50):
        chunk = asins[i : i + 50]
        data = await _get(f"{api_base(r)}/products", {"asins": ",".join(chunk), "response_groups": GROUPS}, client)
        out += [to_book(p, r) for p in (data or {}).get("products") or [] if p.get("title")]
    return out


async def book(asin: str, client=None) -> dict | None:
    found = await products([asin], client)
    return found[0] if found else None


async def search_authors(name: str, client=None) -> list[dict]:
    data = await _get(f"{AUDNEXUS}/authors", {"name": name, "region": region()}, client) or []
    unique = list({a["asin"]: a for a in data if a.get("asin")}.values())[:6]

    async def detail(a: dict) -> dict:
        try:
            info = await _get(f"{AUDNEXUS}/authors/{a['asin']}", {"region": region()}, client) or {}
        except AudibleError:
            info = {}
        return {
            "asin": a["asin"], "name": info.get("name") or a["name"], "image": info.get("image") or "",
            "description": _plain(info.get("description") or "")[:600],
        }

    found = await asyncio.gather(*(detail(a) for a in unique))
    # Audible often has several author pages for one person; show each name once, the fullest page first.
    best: dict[str, dict] = {}
    for a in sorted(found, key=lambda a: (not a["image"], not a["description"])):
        best.setdefault(" ".join(a["name"].lower().split()), a)
    return list(best.values())


async def author(asin: str, client=None) -> dict:
    info = await _get(f"{AUDNEXUS}/authors/{asin}", {"region": region()}, client) or {}
    return {
        "asin": asin, "name": info.get("name") or "", "image": info.get("image") or "",
        "description": _plain(info.get("description") or ""),
    }


async def author_books(asin: str, name: str, client=None, pages: int = 4) -> list[dict]:
    """An author's books, newest first (Audible's author search matches by name, so filter by ID)."""
    r = region()
    found: list[dict] = []
    for page in range(1, pages + 1):
        params = {
            "author": name, "num_results": 50, "page": page - 1, "products_sort_by": "-ReleaseDate",
            "response_groups": GROUPS,
        }
        data = await _get(f"{api_base(r)}/products", params, client) or {}
        batch = [to_book(p, r) for p in data.get("products") or []]
        # Match the author's ID, or their exact name: classic authors are credited under several IDs.
        found += [b for b in batch if asin in b["author_asins"] or name.lower() in [a.strip().lower() for a in b["author"].split(",")]]
        if len(batch) < 50:
            break
    return _keep(found, r)


async def series_books(asin: str, client=None) -> tuple[str, list[dict]]:
    """A series' name and its books, in reading order."""
    params = {"response_groups": "relationships,product_desc,product_attrs"}
    data = await _get(f"{api_base()}/products/{asin}", params, client) or {}
    product = data.get("product") or {}
    children = [
        rel for rel in product.get("relationships") or []
        if rel.get("relationship_to_product") == "child" and rel.get("asin")
    ]
    children.sort(key=lambda rel: float(rel.get("sort") or 999))
    order = {rel["asin"]: i for i, rel in enumerate(children)}
    books = sorted(await products(list(order), client), key=lambda b: order.get(b["asin"], 999))
    return product.get("title") or "", _keep(books, region())
