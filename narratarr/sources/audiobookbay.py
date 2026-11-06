"""AudiobookBay source: scrapes listing pages and book pages for metadata + info hash."""

from __future__ import annotations

import asyncio
import base64
import binascii
import re
from urllib.parse import quote_plus, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

from ..cache import Cache
from .base import Category, Details, Listing, Page

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

CATEGORIES = [
    Category(slug, name)
    for slug, name in [
        ("bestsellers", "Bestsellers"),
        ("fantasy", "Fantasy"),
        ("sci-fi", "Sci-Fi"),
        ("thriller", "Thriller"),
        ("mystery", "Mystery"),
        ("romance", "Romance"),
        ("horror", "Horror"),
        ("historical-fiction", "Historical Fiction"),
        ("litrpg", "LitRPG"),
        ("teen-young-adult", "Teen & Young Adult"),
        ("children", "Children"),
        ("classic", "Classic"),
        ("full-cast", "Full Cast"),
        ("autobiography-biographies", "Biographies"),
        ("history", "History"),
        ("science", "Science"),
        ("business", "Business"),
        ("self-help", "Self-help"),
        ("true-crime", "True Crime"),
        ("humor", "Humor"),
        ("poetry", "Poetry"),
    ]
]

TIMEOUT = httpx.Timeout(15, connect=8)
CACHE_SECONDS = 300
_page_cache: Cache[str] = Cache(CACHE_SECONDS, 200)
_polite = asyncio.Semaphore(3)

_POSTED_RE = re.compile(
    r"Posted:\s*(?P<posted>.+?)\s*Format:\s*(?P<format>.+?)\s*/\s*Bitrate:\s*(?P<bitrate>.+?)\s*"
    r"File Size:\s*(?P<size>[\d.,]+\s*\w+)",
    re.S,
)
_PAGE_RE = re.compile(r"/page/(\d+)/")
_HASH_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def _text(el: Tag | None) -> str:
    return " ".join(el.get_text(" ", strip=True).split()) if el else ""


def split_title(raw: str, hint: str = "") -> tuple[str, str]:
    """'Dune - Frank Herbert' -> ('Dune', 'Frank Herbert').

    Most posts are "Title - Author", but some are "Author - Title". The cover's
    alt text is usually "Author Title", so it's used as a hint to tell them apart.
    """
    if " - " not in raw:
        return raw.strip(), ""
    first, last = (" ".join(x.split()) for x in raw.rsplit(" - ", 1))
    hint = " ".join(hint.replace("\u2019", "'").split()).lower()
    norm = lambda x: x.replace("\u2019", "'").lower()
    if hint and not hint.startswith(norm(last)) and hint.endswith(norm(first)):
        return last, first
    return first, last


def _split_terms(s: str) -> list[str]:
    return [t.strip() for t in re.split(r"[\xa0,]+", s) if t.strip()]


def _reveal(soup: BeautifulSoup) -> BeautifulSoup:
    """Some posts arrive as base64 inside a hidden `.re-ab` element, which the site's own script decodes
    and shows. Do the same, so those posts are parsed like any other. Nothing is run: it's only text.

    The element is usually the post itself (`<div class="post re-ab">` holding the post's inside), so
    its contents are swapped and the element kept: replacing it would lose the `div.post` around them."""
    for el in soup.select(".re-ab"):
        raw = re.sub(r"[^A-Za-z0-9+/=]", "", el.get_text())
        try:
            html = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True).decode("utf-8", "replace")
        except (ValueError, binascii.Error):
            continue  # not base64 after all: leave it as it is
        if "<" not in html:
            continue  # decoded, but not markup: leave it as it is
        el.clear()
        el.append(BeautifulSoup(html, "html.parser"))
    return soup


def parse_listing(html: str, base_url: str, page: int = 1) -> Page:
    soup = _reveal(BeautifulSoup(html, "html.parser"))
    items: list[Listing] = []
    for post in soup.select("div.post"):
        link = post.select_one(".postTitle h2 a, .postTitle h1 a")
        if not link or "/abss/" not in (link.get("href") or ""):
            continue
        img = post.select_one(".postContent img")
        title, author = split_title(_text(link), img.get("alt", "") if img else "")
        info = post.select_one(".postInfo")
        info_text = info.get_text(" ") if info else ""
        cats = re.search(r"Category:(.*?)Language:", info_text, re.S)
        lang = re.search(r"Language:\s*(.*?)(?:Keywords:|$)", info_text, re.S)
        listing = Listing(
            url=urljoin(base_url + "/", link["href"]),
            title=title,
            author=author,
            cover_url=img.get("src", "") if img else "",
            language=" ".join(lang.group(1).split()) if lang else "",
            categories=_split_terms(cats.group(1)) if cats else [],
        )
        for p in post.select(".postContent p"):
            m = _POSTED_RE.search(_text(p))
            if m:
                listing.posted = m["posted"]
                listing.format = m["format"]
                listing.bitrate = m["bitrate"]
                listing.size = re.sub(r"s$", "", m["size"])  # "1.12 GBs" -> "1.12 GB"
                break
        items.append(listing)

    pages = [int(n) for a in soup.select(".wp-pagenavi a") for n in _PAGE_RE.findall(a.get("href", ""))]
    total = max([page, *pages])
    return Page(items=items, page=page, total_pages=total)


def parse_details(html: str, url: str) -> Details:
    soup = _reveal(BeautifulSoup(html, "html.parser"))
    post = soup.select_one("div.post") or soup
    authors = [_text(a) for a in post.select(".desc span.author")]
    narrators = list(dict.fromkeys(_text(n) for n in post.select(".desc span.narrator")))
    img = post.select_one('.postContent img[itemprop="image"]') or post.select_one(".postContent img")
    lang = post.select_one('[itemprop="inLanguage"]')
    fmt = post.select_one(".desc span.format")
    posted = post.select_one('meta[itemprop="datePublished"]')
    title, author = split_title(_text(post.select_one(".postTitle h1")), img.get("alt", "") if img else "")

    desc_parts, bitrate, length = [], "", ""
    for p in post.select(".desc > p"):
        txt = p.get_text("\n", strip=True)
        if txt.startswith("Written by"):
            continue
        if m := re.search(r"Bit ?Rate:\s*(.+)", txt, re.I):
            bitrate = m.group(1).split("\n")[0].strip()
        if m := re.search(r"Length:\s*(.+)", txt):
            length = m.group(1).split("\n")[0].strip()
        if txt.startswith(("Bit Rate", "Bitrate")):
            continue
        desc_parts.append(" ".join(txt.split()))

    info_hash, size, trackers = "", "", []
    for row in soup.select("table tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 2:
            continue
        label, value = _text(cells[0]).rstrip(":").lower(), _text(cells[1])
        if label == "info hash" and _HASH_RE.match(value):
            info_hash = value.lower()
        elif label in ("tracker", "announce url") and value not in trackers:
            trackers.append(value)
        elif label == "file size" and not size:
            size = re.sub(r"s$", "", value)

    return Details(
        url=url,
        title=title,
        author=", ".join(authors) or author,
        cover_url=img.get("src", "") if img else "",
        language=_text(lang),
        categories=[_text(a) for a in post.select('.postInfo a[rel~="tag"]')],
        format=_text(fmt),
        bitrate=bitrate,
        size=size,
        posted=posted.get("content", "") if posted else "",
        narrators=narrators,
        description="\n\n".join(desc_parts),
        length=length,
        info_hash=info_hash,
        trackers=trackers,
    )


class AudiobookBay:
    name = "audiobookbay"
    label = "AudiobookBay"
    description = "Torrents, downloaded through Real-Debrid."
    default_label = "Fresh arrivals"
    delivery = "torrent"

    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self._client = client

    def owns(self, url: str) -> bool:
        return urlparse(url).netloc == urlparse(self.base_url).netloc and "/abss/" in url

    def categories(self) -> list[Category]:
        return CATEGORIES

    async def _get(self, url: str) -> str:
        hit = _page_cache.get(url)
        if hit is not None:
            return hit
        async with _polite:  # never hammer the site with more than a few requests at once
            for attempt in (1, 2):  # the site is occasionally slow; give it one more chance
                try:
                    if self._client:
                        resp = await self._client.get(url)
                    else:
                        async with httpx.AsyncClient(
                            headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=TIMEOUT
                        ) as client:
                            resp = await client.get(url)
                    break
                except httpx.ConnectError:
                    if attempt == 2:
                        raise
        resp.raise_for_status()
        _page_cache.set(url, resp.text)
        return resp.text

    async def latest(self, page: int = 1, category: str | None = None) -> Page:
        root = f"{self.base_url}/audio-books/type/{quote_plus(category)}" if category else self.base_url
        url = f"{root}/page/{page}/" if page > 1 else f"{root}/"
        return parse_listing(await self._get(url), self.base_url, page)

    async def search(self, query: str, page: int = 1) -> Page:
        q = quote_plus(query.strip().lower())
        url = f"{self.base_url}/page/{page}/?s={q}" if page > 1 else f"{self.base_url}/?s={q}"
        return parse_listing(await self._get(url), self.base_url, page)

    async def details(self, url: str) -> Details:
        if not self.owns(url):
            raise ValueError("URL does not belong to this source")
        return parse_details(await self._get(url), url)
