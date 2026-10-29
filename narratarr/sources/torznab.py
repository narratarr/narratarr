"""Torznab source: any indexer that speaks the Torznab protocol.

Jackett, Prowlarr's own Torznab endpoint and standalone indexers all answer the same RSS 2.0
document, so this is written against the protocol rather than against one vendor. The reference for
the shapes below is a real Jackett response, kept as `tests/fixtures/torznab_search.xml`.

Three things in that document need care:

* results arrive as **magnets** or as **download links**, sometimes both in one response, and the
  magnets carry an upper-case infohash;
* the API key parameter is **not always called `apikey`** — Jackett uses `jackett_apikey` and rejects
  `apikey` outright. It also appears in cover links, so every URL kept or shown is scrubbed;
* dates are RFC 822 here, where Prowlarr gives ISO 8601.

Docs: https://torznab.github.io/spec-1.3-draft/
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import OrderedDict
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from ..cache import Cache
from .base import Category, Details, Page
from .prowlarr import _BITRATE_RE, _FORMAT_RE, _fmt_size, fetch as prowlarr_fetch, parse_magnet, tidy_title

NS = "{http://torznab.com/schemas/2015/feed}"
TIMEOUT = httpx.Timeout(90, connect=8)  # a Jackett "all indexers" search waits for every one
CACHE_SECONDS = 300
REMEMBER = 3000

_releases: OrderedDict[str, dict] = OrderedDict()
_search_cache: Cache[list[dict]] = Cache(CACHE_SECONDS, 200)


class TorznabError(Exception):
    pass


def strip_key(url: str) -> str:
    """Drop the API key from a URL. Jackett calls it `jackett_apikey`, Prowlarr `apikey`, and the
    parameter name is exactly the thing that must not leak into stored links, the library or the UI."""
    if not url or url.startswith("magnet:"):
        return url
    u = urlparse(url)
    kept = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if not k.lower().endswith("apikey")]
    return urlunparse(u._replace(query=urlencode(kept)))


def with_key(url: str, base: str, api_key: str) -> str:
    """Put the API key back, but only for links pointing at the user's own indexer.

    Both parameter names go on: the Torznab spec does not fix the name, Jackett only honours
    `jackett_apikey` on its download links, and other indexers only honour `apikey`. Sending both
    to the same host we are already sending the key to costs nothing and avoids picking a vendor.
    """
    if not url or url.startswith("magnet:") or not api_key:
        return url
    u, b = urlparse(url), urlparse(base)
    if (u.scheme, u.netloc) != (b.scheme, b.netloc):
        return url
    query = [*parse_qsl(u.query, keep_blank_values=True), ("apikey", api_key), ("jackett_apikey", api_key)]
    return urlunparse(u._replace(query=urlencode(query)))


def _posted(raw: str) -> str:
    """'Sun, 16 May 2021 11:33:22 +0100' -> '2021-05-16'. ISO input (Prowlarr's habit) passes through."""
    if not raw:
        return ""
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return raw[:10]
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.date().isoformat()


def _text(item: ET.Element, tag: str) -> str:
    return (item.findtext(tag) or "").strip()


def _parse_item(item: ET.Element) -> dict:
    """One <item> as a flat dict, the way Prowlarr hands its releases around."""
    attrs = {a.get("name") or "": (a.get("value") or "") for a in item.findall(f"{NS}attr")}
    link = _text(item, "link")
    enclosure = item.find("enclosure")
    if not link and enclosure is not None:  # the spec's home for the .torrent link
        link = (enclosure.get("url") or "").strip()
    info_hash = (attrs.get("infohash") or "").strip().lower()
    if not info_hash and link.startswith("magnet:"):
        info_hash, _ = parse_magnet(link)
    posted = _posted(_text(item, "pubDate"))
    return {
        "title": _text(item, "title"),
        "link": link,
        "guid": _text(item, "guid"),
        "comments": _text(item, "comments"),
        "size": _text(item, "size"),
        "posted": posted,
        "indexer": _text(item, "jackettindexer") or attrs.get("indexer", ""),
        "infohash": info_hash,
        "seeders": attrs.get("seeders"),
        "protocol": "usenet" if link.lower().endswith(".nzb") else "torrent",
        "categories": [c.text.strip() for c in item.findall("category") if (c.text or "").strip()],
    }


def _url_for(r: dict) -> str:
    """A stable link for a release: the tracker's own page where there is one, else its guid."""
    for key in ("comments", "guid"):
        v = (r.get(key) or "").strip()
        if v.startswith(("http://", "https://")):
            return strip_key(v)
    return f"torznab:{r.get('indexer') or ''}:{r.get('title') or ''}"


def to_details(r: dict) -> Details:
    release = r.get("title") or "Untitled"
    fmt = _FORMAT_RE.search(release)
    rate = _BITRATE_RE.search(release)
    trackers: list[str] = []
    if (r.get("link") or "").startswith("magnet:"):
        _, trackers = parse_magnet(r["link"])
    seeders = r.get("seeders")
    return Details(
        url=_url_for(r),
        title=tidy_title(release),
        format=fmt.group(1).upper() if fmt else "",
        bitrate=f"{rate.group(1)} Kbps" if rate else "",
        size=_fmt_size(int(r["size"])) if str(r.get("size") or "").isdigit() else "",
        posted=r.get("posted") or "",
        indexer=r.get("indexer") or "",
        protocol=r.get("protocol") or "",
        seeders=int(seeders) if r.get("protocol") == "torrent" and str(seeders or "").isdigit() else None,
        release=release,
        info_hash=r.get("infohash") or "",
        trackers=trackers,
        # Without an info hash the .torrent has to be fetched; the link is stored without the key.
        download_url="" if r.get("infohash") else strip_key(r.get("link") or ""),
    )


def remember(r: dict) -> None:
    url = _url_for(r)
    _releases[url] = r
    _releases.move_to_end(url)
    while len(_releases) > REMEMBER:
        _releases.popitem(last=False)


async def fetch(url: str, base: str, api_key: str, transport: httpx.AsyncBaseTransport | None = None):
    """Download a release: ("magnet", uri) | ("torrent", bytes) | ("nzb", bytes).

    The key is added here, and Prowlarr's fetch does the rest with an empty base so it cannot add a
    second one. That function already knows the awkward parts: a 302 to a magnet, and telling a
    bencoded .torrent from an .nzb by looking at the bytes.
    """
    if url.startswith("magnet:"):
        return "magnet", url
    return await prowlarr_fetch(with_key(url, base, api_key), "", "", transport,
                                who="your indexer")


class Torznab:
    name = "torznab"
    label = "Torznab"
    description = "Searches any Torznab indexer: Jackett, or Prowlarr's Torznab endpoint."
    default_label = "Latest from your indexers"
    delivery = "indexer"

    def __init__(self, url: str, api_key: str, categories: str = "3030",
                 client: httpx.AsyncClient | None = None):
        self.base = url.rstrip("/")
        self.api_key = api_key
        self.cats = [c for c in re.split(r"[,\s]+", categories or "") if c.isdigit()] or ["3030"]
        self._client = client

    # ---- Source interface ----------------------------------------------------

    def owns(self, url: str) -> bool:
        return url in _releases

    def categories(self) -> list[Category]:
        return []

    async def latest(self, page: int = 1, category: str | None = None) -> Page:
        if page > 1:
            return Page(items=[], page=page, total_pages=1)
        found = await self._search("")
        found.sort(key=lambda r: r.get("posted") or "", reverse=True)
        return Page(items=[to_details(r) for r in found], page=1, total_pages=1)

    async def search(self, query: str, page: int = 1) -> Page:
        # Torznab indexers answer in one go; there is no page 2 to ask them for.
        if page > 1 or not query.strip():
            return Page(items=[], page=page, total_pages=1)
        found = await self._search(query.strip())
        return Page(items=[to_details(r) for r in found], page=1, total_pages=1)

    async def details(self, url: str) -> Details:
        r = _releases.get(url)
        if r is None:
            raise TorznabError("This search result has expired. Search for it again.")
        d = to_details(r)
        from .. import search  # imported here: search depends on this package

        match = await search.identify(d.title)
        if match:
            d.title, d.author = match["title"], match["author"]
            d.cover_url = d.cover_url or match["cover_url"]
        elif m := re.fullmatch(r"(.+?)\s+by\s+(.+)\s*", d.title, re.I):
            d.title, d.author = m.group(1), m.group(2)
        return d

    # ---- Torznab API ---------------------------------------------------------

    async def _get(self, params: list[tuple[str, str]]) -> ET.Element:
        if not (self.base and self.api_key):
            raise TorznabError("Add your Torznab address and API key in Settings first.")
        try:
            if self._client:
                resp = await self._client.get(self.base, params=params, timeout=TIMEOUT)
            else:
                async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                    resp = await client.get(self.base, params=params)
        except httpx.TimeoutException:
            raise TorznabError("Your indexer took too long to answer. One of its sources may be slow or down.")
        except httpx.HTTPError:
            raise TorznabError(f"Couldn't reach the indexer at {self.base}. Check the address in Settings.")
        if resp.status_code in (401, 403):
            raise TorznabError("Your indexer rejected the API key. Check it in Settings.")
        body = resp.content
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            if resp.status_code >= 400:
                raise TorznabError(f"Your indexer returned an error ({resp.status_code}).")
            raise TorznabError("That address doesn't look like a Torznab indexer. Check it in Settings.")
        if root.tag == "error":  # Torznab reports a bad search as an XML <error>, often on a 200
            raise TorznabError(f"Your indexer refused the search: {root.get('description') or 'no reason given'}.")
        if resp.status_code >= 400:
            raise TorznabError(f"Your indexer returned an error ({resp.status_code}).")
        return root

    async def _search(self, query: str) -> list[dict]:
        key = (self.base, query.lower(), tuple(self.cats))
        found = _search_cache.get(key)
        if found is None:
            params = [("t", "search"), ("q", query), ("limit", "100")]
            params += [("cat", c) for c in self.cats]
            params.append(("apikey", self.api_key))
            root = await self._get(params)
            found = [
                r for r in (_parse_item(i) for i in root.findall("./channel/item"))
                if r["link"] or r["infohash"]
            ]
            if found:  # an empty answer may just mean a source was briefly down; ask again next time
                _search_cache.set(key, found)
        for r in found:
            remember(r)
        return list(found)

    async def status(self) -> dict:
        """`t=caps` proves the address and the key in one call, and says whether books are searchable."""
        root = await self._get([("t", "caps"), ("apikey", self.api_key)])
        server = root.find("server")
        book = root.find("searching/book-search")
        return {
            "title": (server.get("title") if server is not None else "") or "indexer",
            "book_search": (book is not None and book.get("available") == "yes"),
            "categories": len(root.findall("categories/category")),
        }
