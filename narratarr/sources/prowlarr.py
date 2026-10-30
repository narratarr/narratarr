"""Prowlarr source: searches every indexer the user has added to Prowlarr.

Prowlarr hands back releases, not books, so each result carries how to get it:
torrents go through Real-Debrid, Usenet (NZB) releases through SABnzbd. When a
release is opened we match its name against Apple's audiobook catalog to find
the real title, author and cover, so the library folder comes out right.

API: https://prowlarr.com/docs/api/
"""

from __future__ import annotations

import re
from collections import OrderedDict
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urlparse, urlunparse

import httpx

from ..cache import Cache
from .base import Category, Details, Page

TIMEOUT = httpx.Timeout(90, connect=8)  # Prowlarr waits for every indexer to answer
MAX_FETCH_BYTES = 32 << 20  # .torrent and .nzb files are small; anything bigger isn't one
CACHE_SECONDS = 300
REMEMBER = 3000  # releases kept in memory so a result can still be opened or added after the search

_releases: OrderedDict[str, dict] = OrderedDict()
_search_cache: Cache[list[dict]] = Cache(CACHE_SECONDS, 200)
_offering: Cache[list[str]] = Cache(600, 20)  # which indexers offer which categories

_FORMAT_RE = re.compile(r"\b(m4b|mp3|flac|aac|m4a|ogg|opus|epub|azw3|azw|mobi|pdf|fb2|cbz|cbr|djvu)\b", re.I)
_AUDIO = {"m4b", "mp3", "flac", "aac", "m4a", "ogg", "opus"}
_BITRATE_RE = re.compile(r"\b(\d{2,3})\s?kb(?:ps|it)?s?\b", re.I)
_BRACKETS_RE = re.compile(r"\s*[\(\[\{][^\)\]\}]*[\)\]\}]")


class ProwlarrError(Exception):
    pass


def split_categories(text: str, fallback: str) -> list[str]:
    """ "3030, 3040" -> ["3030", "3040"]; the fallback when nothing usable is there."""
    return [c for c in re.split(r"[,\s]+", text or "") if c.isdigit()] or [fallback]


def offers(indexer: dict, cats: list[str]) -> bool:
    """Whether an indexer (as Prowlarr describes it) has any of these categories, or one inside them."""
    found = set()
    for c in (indexer.get("capabilities") or {}).get("categories") or []:
        found.add(str(c.get("id")))
        found.update(str(sub.get("id")) for sub in c.get("subCategories") or [])
    return bool(found & set(cats))


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def tidy_title(release: str) -> str:
    """'Frank.Herbert-Dune.(2006).[M4B]' -> 'Frank Herbert - Dune'."""
    t = release
    if " " not in t:  # scene-style names use dots for spaces
        t = re.sub(r"\s*-\s*", " - ", re.sub(r"[._]+", " ", t))
    t = _BRACKETS_RE.sub("", t)
    t = _FORMAT_RE.sub("", t)
    t = _BITRATE_RE.sub("", t)
    t = re.sub(r"\b(19|20)\d{2}\b\s*$", "", t)
    t = re.sub(r"[\s\-–:,]+$", "", " ".join(t.split()))
    return t or release


def parse_magnet(uri: str) -> tuple[str, list[str]]:
    qs = parse_qs(urlparse(uri).query)
    info_hash = ""
    for xt in qs.get("xt", []):
        if xt.lower().startswith("urn:btih:"):
            info_hash = xt[9:]
    if not re.fullmatch(r"[0-9a-fA-F]{40}", info_hash):
        info_hash = ""  # base32 hashes: let Real-Debrid read the magnet itself
    return info_hash.lower(), [unquote(t) for t in qs.get("tr", [])]


def strip_key(url: str) -> str:
    """Prowlarr puts its API key in download links; never store or show it."""
    if not url or url.startswith("magnet:"):
        return url
    u = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k.lower() != "apikey"]
    return urlunparse(u._replace(query=urlencode(query)))


def with_key(url: str, base: str, api_key: str) -> str:
    """Add the API key back, but only for links that point at the user's own Prowlarr."""
    u, b = urlparse(url), urlparse(base)
    if (u.scheme, u.netloc) != (b.scheme, b.netloc):
        return url
    return urlunparse(u._replace(query=urlencode([*parse_qsl(u.query, keep_blank_values=True), ("apikey", api_key)])))


def _url_for(r: dict) -> str:
    """A stable link for a release: the indexer's page when there is one."""
    for key in ("infoUrl", "guid", "commentUrl"):
        v = r.get(key) or ""
        if v.startswith(("http://", "https://")):
            return v
    return f"prowlarr:{r.get('indexerId', 0)}:{r.get('guid') or r.get('title', '')}"


def to_details(r: dict) -> Details:
    release = r.get("title") or "Untitled"
    protocol = r.get("protocol") or ""
    info_hash = (r.get("infoHash") or "").lower()
    trackers: list[str] = []
    magnet = r.get("magnetUrl") or ""
    if magnet.startswith("magnet:"):
        h, trackers = parse_magnet(magnet)
        info_hash = info_hash or h
    found = [m.group(1) for m in _FORMAT_RE.finditer(release)]
    # "Dune (PDF + M4B)" is an audiobook with a companion PDF: its audio format is the one that counts.
    # "Opus" is also a word ("Magnum Opus [EPUB]"), so it only counts when nothing else is named.
    fmt = next((f for f in found if f.lower() in _AUDIO and f.lower() != "opus"), found[0] if found else "")
    if fmt.lower() == "opus" and len(found) > 1:
        fmt = found[1]
    rate = _BITRATE_RE.search(release)
    seeders = r.get("seeders")
    return Details(
        url=_url_for(r),
        title=tidy_title(release),
        cover_url=r.get("posterUrl") or "",
        format=fmt.upper(),
        bitrate=f"{rate.group(1)} Kbps" if rate else "",
        size=_fmt_size(int(r.get("size") or 0)) if r.get("size") else "",
        posted=(r.get("publishDate") or "")[:10],
        indexer=r.get("indexer") or "",
        protocol=protocol,
        seeders=int(seeders) if protocol == "torrent" and seeders is not None else None,
        release=release,
        info_hash=info_hash,
        trackers=trackers,
        download_url="" if info_hash else strip_key(r.get("downloadUrl") or magnet),
    )


def remember(r: dict) -> None:
    url = _url_for(r)
    _releases[url] = r
    _releases.move_to_end(url)
    while len(_releases) > REMEMBER:
        _releases.popitem(last=False)


class Prowlarr:
    name = "prowlarr"
    label = "Prowlarr"
    description = "Searches the indexers you’ve added to Prowlarr."
    default_label = "Latest from your indexers"
    delivery = "indexer"

    def __init__(self, url: str, api_key: str, categories: str = "3030", client: httpx.AsyncClient | None = None,
                 fallback: str = "3030"):
        self.base = url.rstrip("/")
        self.api_key = api_key
        self.cats = split_categories(categories, fallback)
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
        found.sort(key=lambda r: r.get("publishDate") or "", reverse=True)
        return Page(items=[to_details(r) for r in found], page=1, total_pages=1)

    async def search(self, query: str, page: int = 1) -> Page:
        # Prowlarr returns everything in one go; there is no page 2 to ask indexers for.
        if page > 1 or not query.strip():
            return Page(items=[], page=page, total_pages=1)
        return Page(items=[to_details(r) for r in await self._search(query.strip())], page=1, total_pages=1)

    async def details(self, url: str) -> Details:
        r = _releases.get(url)
        if r is None:
            raise ProwlarrError("This search result has expired. Search for it again.")
        d = to_details(r)
        from .. import search  # imported here: search depends on this package

        match = await search.identify(d.title)
        if match:
            d.title, d.author = match["title"], match["author"]
            d.cover_url = d.cover_url or match["cover_url"]
        elif m := re.fullmatch(r"(.+?)\s+by\s+(.+)", d.title, re.I):
            d.title, d.author = m.group(1), m.group(2)  # "The Raven by Edgar Allan Poe"
        return d

    # ---- Prowlarr API --------------------------------------------------------

    async def request(self, path: str, params: list | None = None) -> list | dict:
        if not (self.base and self.api_key):
            raise ProwlarrError("Add your Prowlarr address and API key in Settings first.")
        headers = {"X-Api-Key": self.api_key}
        try:
            if self._client:
                resp = await self._client.get(f"{self.base}{path}", params=params, headers=headers, timeout=TIMEOUT)
            else:
                async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                    resp = await client.get(f"{self.base}{path}", params=params, headers=headers)
        except httpx.TimeoutException:
            raise ProwlarrError("Prowlarr took too long to answer. One of your indexers may be slow or down.")
        except httpx.HTTPError:
            raise ProwlarrError(f"Couldn't reach Prowlarr at {self.base}. Check the address in Settings.")
        if resp.status_code == 401:
            raise ProwlarrError("Prowlarr rejected the API key. Check it in Settings.")
        if resp.status_code >= 400:
            raise ProwlarrError(f"Prowlarr returned an error ({resp.status_code}).")
        try:
            return resp.json()
        except ValueError:
            raise ProwlarrError("That address doesn’t look like Prowlarr. Check it in Settings.")

    async def _search(self, query: str) -> list[dict]:
        key = (self.base, query.lower(), tuple(self.cats))
        found = _search_cache.get(key)
        if found is None:
            params = [("query", query), ("type", "search"), ("limit", "100"), *(("categories", c) for c in self.cats)]
            data = await self.request("/api/v1/search", params)
            found = [
                r for r in (data if isinstance(data, list) else [])
                if r.get("protocol") in ("torrent", "usenet")
                and (r.get("downloadUrl") or r.get("magnetUrl") or r.get("infoHash"))
            ]
            if found:  # an empty answer may just mean an indexer was briefly down; ask again next time
                _search_cache.set(key, found)
        for r in found:
            remember(r)
        return list(found)

    async def _enabled(self) -> list[dict]:
        indexers = await self.request("/api/v1/indexer")
        return [i for i in indexers if isinstance(i, dict) and i.get("enable")] if isinstance(indexers, list) else []

    async def status(self, ebook_categories: str = "7020") -> dict:
        info = await self.request("/api/v1/system/status")
        enabled = await self._enabled()
        cats = split_categories(ebook_categories, "7020")
        return {"version": info.get("version", ""), "indexers": len(enabled),
                "ebook_indexers": sorted((i.get("name") or "?" for i in enabled if offers(i, cats)), key=str.lower),
                "ebook_categories": cats}

    async def offering(self) -> list[str] | None:
        """The enabled indexers that search this instance's categories; None if Prowlarr can't say right now."""
        key = (self.base, tuple(self.cats))
        names = _offering.get(key)
        if names is None:
            try:
                names = sorted((i.get("name") or "?" for i in await self._enabled() if offers(i, self.cats)), key=str.lower)
            except ProwlarrError:
                return None
            _offering.set(key, names)
        return names


async def fetch(url: str, base: str, api_key: str, transport: httpx.AsyncBaseTransport | None = None):
    """Download a release: ("magnet", uri) | ("torrent", bytes) | ("nzb", bytes)."""
    if url.startswith("magnet:"):
        return "magnet", url
    target = with_key(url, base, api_key)
    async with httpx.AsyncClient(timeout=60, transport=transport) as client:
        for _ in range(6):
            if not target.startswith(("http://", "https://")):
                raise ProwlarrError("The indexer pointed somewhere Narratarr won't follow.")
            try:
                resp = await client.get(target)
            except httpx.HTTPError:
                raise ProwlarrError("Couldn't reach Prowlarr to fetch this release.")
            if resp.is_redirect:
                location = resp.headers.get("location", "")
                if location.startswith("magnet:"):
                    return "magnet", location
                target = str(resp.url.join(location))
                continue
            if resp.status_code == 401:
                raise ProwlarrError("Prowlarr rejected the API key. Check it in Settings.")
            if resp.status_code >= 400:
                raise ProwlarrError(f"The indexer wouldn't hand over this release ({resp.status_code}). Try another one.")
            body = resp.content
            if len(body) > MAX_FETCH_BYTES:
                raise ProwlarrError("The indexer sent something far too big to be a .torrent or .nzb file.")
            head = body[:4096].lstrip().lower()
            if b"<nzb" in head:
                return "nzb", body
            if head.startswith(b"d") and b"4:infod" in body:  # a bencoded .torrent
                return "torrent", body
            raise ProwlarrError("The indexer didn't return a .torrent or .nzb file for this release.")
    raise ProwlarrError("Too many redirects while fetching this release.")
