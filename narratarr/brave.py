"""Search AudiobookBay through the Brave Search API instead of the site's own search.

Brave already has the site indexed, corrects spelling on its own, and keeps
answering when AudiobookBay is slow or down. We ask for `<query> site:<host>`,
keep only book pages, and turn each hit into a Listing. Opening a book still
reads its page on AudiobookBay, since that's where the info hash lives.

Docs: https://api-dashboard.search.brave.com/app/documentation/web-search
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from urllib.parse import urlparse, urlunparse

import httpx

from .sources.audiobookbay import split_title
from .sources.base import Listing

API = "https://api.search.brave.com/res/v1/web/search"
MONID_API = "https://api.monid.ai"
PER_PAGE = 20
MAX_OFFSET = 9  # Brave only pages this far

# "Dune - Frank Herbert Audiobook M4B" / "... | AudioBook Bay" -> "Dune - Frank Herbert"
_TITLE_TAIL = re.compile(
    r"\s*(?:[|–-]\s*audio\s?book\s?bay.*|audiobook(?:\s+(?:m4b|mp3|m4a|flac|aac|free|download))*\s*(?:\.{3}|…)?)\s*$",
    re.I,
)
_FORMAT = re.compile(r"\b(M4B|MP3|M4A|FLAC|AAC)\b")


class BraveError(Exception):
    pass


def to_listing(result: dict, base_url: str) -> Listing | None:
    """A Brave hit -> Listing, or None if it isn't an AudiobookBay book page."""
    url = urlparse(result.get("url", ""))
    if "audiobookbay" not in url.netloc or not url.path.startswith("/abss/"):
        return None
    # Brave may have indexed a mirror domain; point it at the configured one.
    base = urlparse(base_url)
    canonical = urlunparse((base.scheme, base.netloc, url.path, "", "", ""))

    raw_title = re.sub(r"<[^>]+>", "", result.get("title", ""))
    fmt = _FORMAT.search(raw_title)
    title, author = split_title(_TITLE_TAIL.sub("", raw_title).strip())
    thumb = result.get("thumbnail") or {}
    return Listing(
        url=canonical,
        title=title,
        author=author,
        cover_url=thumb.get("src", ""),
        format=fmt.group(1).upper() if fmt else "",
        posted=result.get("page_age", "")[:10],
    )


async def search(
    query: str, api_key: str, base_url: str, page: int = 1, client: httpx.AsyncClient | None = None,
    *, use_monid: bool = True,
) -> dict:
    """One page of AudiobookBay results from Brave, plus Brave's spelling correction."""
    if use_monid and (key := os.environ.get("MONID_API_KEY")):
        if client:
            return await _search_monid(query, key, base_url, page, client)
        async with httpx.AsyncClient(timeout=15) as c:
            return await _search_monid(query, key, base_url, page, c)
    if not api_key:
        raise BraveError("Add your Brave Search API key in Settings first.")
    host = urlparse(base_url).netloc
    params = {
        "q": f"{query} site:{host}",
        "count": PER_PAGE,
        "offset": min(page - 1, MAX_OFFSET),
        "result_filter": "web",
        "safesearch": "off",
    }
    headers = {"Accept": "application/json", "X-Subscription-Token": api_key}
    try:
        if client:
            resp = await client.get(API, params=params, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=15) as c:
                resp = await c.get(API, params=params, headers=headers)
    except httpx.HTTPError as e:
        raise BraveError("Couldn't reach Brave Search.") from e

    _check_search_status(resp.status_code, resp.text if resp.status_code == 422 else "")

    data = resp.json()
    items, seen = [], set()
    for r in (data.get("web") or {}).get("results", []):
        listing = to_listing(r, base_url)
        if listing and listing.url not in seen:
            seen.add(listing.url)
            items.append(listing)

    q = data.get("query") or {}
    altered = re.sub(r"\s*site:\S+", "", q.get("altered") or "").strip()
    more = bool(q.get("more_results_available")) and page - 1 < MAX_OFFSET
    return {"items": items, "more": more, "altered": altered}


def _check_search_status(status: int, error: str) -> None:
    if status in (401, 403) or (status == 422 and "token" in error.lower()):
        raise BraveError("Brave rejected the API key. Check it in Settings.")
    if status == 429:
        raise BraveError("Brave Search rate limit reached. Wait a moment, or check your plan's monthly quota.")
    if status >= 400:
        raise BraveError(f"Brave Search error {status}.")


def _monid_results(output: dict, base_url: str, page: int) -> dict:
    results = output.get("results") or []
    items = {}
    for result in results:
        listing = to_listing({
            "url": result.get("link") or "",
            "title": result.get("title") or "",
            "page_age": result.get("published_date") or "",
        }, base_url)
        if listing:
            items.setdefault(listing.url, listing)
    # Search1API doesn't report whether another page exists.
    return {"items": list(items.values()), "more": len(results) == PER_PAGE and page <= MAX_OFFSET, "altered": ""}


async def _search_monid(query: str, key: str, base_url: str, page: int, client: httpx.AsyncClient) -> dict:
    host = urlparse(base_url).netloc
    body = {
        "query": f"{query} site:{host}",
        "max_results": PER_PAGE,
        "search_service": "bing",
        "page": min(max(page, 1), MAX_OFFSET + 1),
    }
    payload = {"provider": "search1api", "endpoint": "/search", "input": {"body": body}}
    headers = {"Authorization": f"Bearer {key}"}
    try:
        async with asyncio.timeout(15):
            resp = await client.post(
                f"{MONID_API}/v1/run",
                headers={**headers, "Content-Type": "application/json; charset=utf-8"},
                content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )
            resp.raise_for_status()
            run = resp.json()
            if resp.status_code == 202:
                run_id = run["runId"]
                while True:
                    await asyncio.sleep(2)
                    resp = await client.get(f"{MONID_API}/v1/runs/{run_id}", headers=headers)
                    resp.raise_for_status()
                    run = resp.json()
                    if run.get("status") in ("COMPLETED", "FAILED", "TIMED_OUT", "STOPPED", "BLOCKED"):
                        break
    except httpx.HTTPStatusError as e:
        raise BraveError(f"Monid Search error {e.response.status_code}.") from e
    except (httpx.HTTPError, TimeoutError) as e:
        raise BraveError("Couldn't complete the Monid search.") from e

    if run.get("status") not in (None, "COMPLETED"):
        raise BraveError(f"Monid search run ended with status {run['status']}.")
    provider = run.get("providerResponse") or {}
    _check_search_status(provider.get("httpStatus", 200), str(provider.get("error", "")))
    return _monid_results(run["output"], base_url, page)
