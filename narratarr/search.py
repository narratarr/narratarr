"""Smart search: typo-tolerant, multi-page, ranked.

AudiobookBay's own search is an exact keyword match, so "projct hail marry"
finds nothing. To get around that we:

  1. search the source for the query as typed (several pages at once),
  2. ask Apple's public audiobook search (which tolerates typos well) what the
     user probably meant, and search the source again with the corrected
     title / author,
  3. merge everything, drop duplicates and rank by fuzzy similarity.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata

import httpx
from rapidfuzz import fuzz

from . import brave, config
from .cache import Cache
from .sources import Listing, Source

ITUNES = "https://itunes.apple.com/search"
PAGES_PER_BATCH = 2
_CACHE_SECONDS = 600
_cache: Cache[list[dict]] = Cache(_CACHE_SECONDS, 300)


def normalize(s: str) -> str:
    s = re.sub(r"['\u2019]", "", s)  # "Muad'Dib" -> "muaddib", "don't" -> "dont"
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^\w\s]", " ", s.lower())
    return " ".join(s.split())


def clean_title(title: str) -> str:
    """'Mistborn: The Final Empire (Unabridged) [Dramatized]' -> 'Mistborn: The Final Empire'."""
    title = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", title)
    return " ".join(title.split())


STOPWORDS = {"the", "a", "an", "of", "and", "by", "in", "on", "to", "for", "with", "at", "or", "de", "la", "le"}


def _content(s: str) -> str:
    """Drop little words so 'the' alone can't make two titles look alike."""
    words = [w for w in s.split() if w not in STOPWORDS]
    return " ".join(words) or s


def score(query: str, item: Listing) -> float:
    q = _content(normalize(query))
    title, author = _content(normalize(item.title)), _content(normalize(item.author))
    if not q:
        return 0.0
    both = f"{title} {author}"
    s = max(
        fuzz.token_set_ratio(q, both),
        fuzz.partial_ratio(q, title) if len(q) >= 4 else 0,
        fuzz.WRatio(q, title),
    )
    if q in both:
        s += 10  # exact phrase match beats fuzzy look-alikes
    if q == title:
        s += 15
    elif title.startswith(q):
        s += 5
    return float(s)


async def suggestions(query: str, limit: int = 8, client: httpx.AsyncClient | None = None) -> list[dict]:
    """Typo-tolerant title/author suggestions from Apple's audiobook catalog."""
    key = f"{normalize(query)}:{limit}"
    hit = _cache.get(key)
    if hit is not None:
        return hit

    params = {"term": query, "media": "audiobook", "limit": limit * 2}
    try:
        if client:
            resp = await client.get(ITUNES, params=params)
        else:
            async with httpx.AsyncClient(timeout=8) as c:
                resp = await c.get(ITUNES, params=params)
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except (httpx.HTTPError, ValueError):
        return []

    out, seen = [], set()
    for r in results:
        raw = r.get("collectionName") or ""
        if re.search(r"\b(summary|analysis|study guide)\b", raw, re.I):
            continue  # skip knock-off summaries of the real book
        title = clean_title(raw)
        author = (r.get("artistName") or "").strip()
        if not title or (normalize(title), normalize(author)) in seen:
            continue
        seen.add((normalize(title), normalize(author)))
        art = (r.get("artworkUrl100") or "").replace("100x100bb", "300x300bb")
        out.append({"title": title, "author": author, "cover_url": art})
        if len(out) >= limit:
            break
    _cache.set(key, out)
    return out


def correct_words(query: str, sugg: list[dict]) -> str:
    """Fix each misspelled word using the vocabulary of the suggestions.

    'harry poter' -> 'harry potter', 'brandon sandersen mistborn' -> 'brandon sanderson mistborn'
    """
    vocab = {w for sg in sugg[:5] for w in normalize(f"{sg['title']} {sg['author']}").split() if len(w) > 2}
    fixed = []
    for word in normalize(query).split():
        if word in vocab or len(word) <= 2:
            fixed.append(word)
            continue
        best = max(vocab, key=lambda v: fuzz.ratio(word, v), default="")
        fixed.append(best if best and fuzz.ratio(word, best) >= 75 else word)
    return " ".join(fixed)


def corrected_queries(query: str, sugg: list[dict]) -> tuple[list[str], str]:
    """Pick extra source queries from the suggestions, plus a 'did you mean' label."""
    if not sugg:
        return [], ""
    q = normalize(query)
    corrected = correct_words(query, sugg)
    title_main = normalize(sugg[0]["title"].split(":")[0])

    extra = [corrected]
    # If the query names a specific book, search its exact title too.
    if fuzz.partial_ratio(corrected, title_main) >= 80:
        extra.append(title_main)
    extra = [e for e in dict.fromkeys(extra) if e and e != q]
    return extra, corrected if corrected != q else ""


async def _search_pages(source: Source, query: str, first: int, count: int) -> tuple[list[Listing], int]:
    pages = await asyncio.gather(
        *(source.search(query, p) for p in range(first, first + count)), return_exceptions=True
    )
    errors = [p for p in pages if isinstance(p, BaseException)]
    if errors and (len(errors) == len(pages) or (first == 1 and isinstance(pages[0], BaseException))):
        # The source is unreachable: let the caller report it. Page 1 failing counts even when later
        # pages "succeed", since a source that answers in one page (Torznab) returns those empty.
        raise errors[0] if len(errors) == len(pages) else pages[0]
    items, total = [], 0
    for p in pages:
        if isinstance(p, BaseException):
            continue  # a later page failing (or not existing) shouldn't sink the search
        items.extend(p.items)
        total = max(total, p.total_pages)
    return items, total


def relevant(query: str, sugg: list[dict]) -> list[dict]:
    """Drop suggestions that only loosely relate to what was typed."""
    q = normalize(query)
    keep = [
        sg for sg in sugg
        if fuzz.token_set_ratio(q, normalize(f"{sg['title']} {sg['author']}")) >= 60
        or fuzz.partial_ratio(q, normalize(sg["title"])) >= 75
    ]
    return keep or sugg[:1]


async def smart_search(source: Source, query: str, batch: int = 1, use_suggestions: bool = True) -> dict:
    """Return ranked results for one batch (several source pages) of a query."""
    first = (batch - 1) * PAGES_PER_BATCH + 1
    raw_task = asyncio.ensure_future(_search_pages(source, query, first, PAGES_PER_BATCH))

    # While the literal search runs, work out what the user probably meant and
    # start searching for that too, so typos don't cost extra waiting.
    extra_tasks, label = [], ""
    if use_suggestions and batch == 1:
        extra, label = corrected_queries(query, await suggestions(query))
        extra_tasks = [asyncio.ensure_future(_search_pages(source, e, 1, 1)) for e in extra]

    try:
        items, total = await raw_task
    except httpx.HTTPError:
        if not extra_tasks:
            raise
        items, total = [], 0

    did_you_mean, queries = "", [query]
    if extra_tasks:
        best = max((score(query, i) for i in items), default=0)
        more = await asyncio.gather(*extra_tasks, return_exceptions=True)
        found = [m for m in more if not isinstance(m, BaseException)]
        if not found and not items:
            raise next(m for m in more if isinstance(m, BaseException))
        # Use the corrected results when we fixed a typo, or the literal search was weak.
        if found and (label or len(items) < 6 or best < 75):
            for m_items, _ in found:
                items.extend(m_items)
            queries += [e for e in extra]
            did_you_mean = label if best < 75 else ""

    total_batches = max(1, -(-total // PAGES_PER_BATCH)) if total else 1
    return {"items": rank(items, queries), "page": batch, "total_pages": total_batches, "did_you_mean": did_you_mean}


async def latest_batch(source: Source, batch: int = 1, category: str | None = None, pages: int = 3) -> dict:
    """Several listing pages at once, for sites that show only a handful of books per page."""
    first = (batch - 1) * pages + 1
    results = await asyncio.gather(*(source.latest(p, category) for p in range(first, first + pages)), return_exceptions=True)
    errors = [r for r in results if isinstance(r, BaseException)]
    if errors and len(errors) == len(results):
        raise errors[0]
    items, seen, total = [], set(), 0
    for r in results:
        if isinstance(r, BaseException):
            continue  # a page past the end, or one slow page, shouldn't sink the rest
        total = max(total, r.total_pages)
        for item in r.items:
            if item.url not in seen:
                seen.add(item.url)
                items.append(item.to_dict())
    return {"items": items, "page": batch, "total_pages": max(1, -(-total // pages))}


async def careful_search(source: Source, query: str, use_suggestions: bool = True) -> dict:
    """For sources where every query costs something (Prowlarr spends each indexer's API allowance):
    search as typed, and only try a corrected spelling when that finds little."""
    items = list((await source.search(query, 1)).items)
    did_you_mean, queries = "", [query]
    best = max((score(query, i) for i in items), default=0)
    if use_suggestions and (len(items) < 3 or best < 75):
        extra, label = corrected_queries(query, await suggestions(query))
        if extra:
            try:
                more = (await source.search(extra[0], 1)).items
            except Exception:
                more = []  # the search as typed already worked; don't fail over the extra one
            if more:
                items += more
                queries.append(extra[0])
                did_you_mean = label or extra[0]
    return {"items": rank(items, queries), "page": 1, "total_pages": 1, "did_you_mean": did_you_mean}


_RELEASE_NOISE = re.compile(r"\b(unabridged|abridged|audiobook|retail|m4b|mp3|flac|aac|\d{2,3}\s?kbps|(19|20)\d{2})\b", re.I)


async def identify(release: str) -> dict | None:
    """Work out which book a release name is, from Apple's catalog: {title, author, cover_url} or None.

    Strict on purpose: both the title and the author must appear in the release name,
    because a wrong match would file the book under the wrong folder. Honors the
    smart-search switch, since it sends the release name to Apple.
    """
    if not config.load().smart_search:
        return None
    words = normalize(_RELEASE_NOISE.sub(" ", clean_title(release)))
    if len(words) < 3:
        return None
    for sg in await suggestions(words, limit=5):
        title = normalize(sg["title"].split(":")[0])
        author = normalize(sg["author"])
        if not (title and author):
            continue
        if fuzz.partial_ratio(title, words) >= 90 and fuzz.token_set_ratio(author, words) >= 85:
            # Keep a subtitle only if the release has it too; it may name a different edition.
            main, _, sub = sg["title"].partition(":")
            keep = sub.strip() and fuzz.partial_ratio(normalize(sub), words) >= 90
            return {**sg, "title": sg["title"] if keep else main.strip()}
    return None


def rank(items: list[Listing], queries: list[str]) -> list[dict]:
    """Dedupe and sort by best fuzzy match; ties keep the incoming order."""
    seen, ranked = set(), []
    for pos, item in enumerate(items):
        if item.url in seen:
            continue
        seen.add(item.url)
        ranked.append((max(score(q, item) for q in queries), pos, item))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    return [{**i.to_dict(), "score": round(s)} for s, _, i in ranked]


async def brave_smart_search(
    query: str, api_key: str, base_url: str, batch: int = 1, use_suggestions: bool = True, client=None
) -> dict:
    """Search through Brave. Brave fixes most typos itself; Apple's catalog covers the rest."""
    res = await brave.search(query, api_key, base_url, batch, client)
    did_you_mean = res["altered"]
    queries = [query] + ([did_you_mean] if did_you_mean else [])

    # Nothing at all? Try the corrected spelling from Apple's catalog (one extra Brave query).
    if not res["items"] and batch == 1 and use_suggestions:
        extra, label = corrected_queries(query, await suggestions(query))
        if extra:
            retry = await brave.search(extra[0], api_key, base_url, 1, client)
            if retry["items"]:
                res, did_you_mean = retry, label or extra[0]
                queries.append(extra[0])

    return {
        "items": rank(res["items"], queries),
        "page": batch,
        "total_pages": batch + 1 if res["more"] else batch,
        "did_you_mean": did_you_mean,
    }
