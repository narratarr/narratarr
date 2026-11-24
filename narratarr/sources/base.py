"""The interface every book source implements.

Keeping sources behind this interface means the rest of the app (library,
download pipeline, UI) doesn't care where a book comes from.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Protocol
from urllib.parse import quote


@dataclass
class Listing:
    url: str
    title: str
    author: str = ""
    cover_url: str = ""
    language: str = ""
    categories: list[str] = field(default_factory=list)
    format: str = ""
    bitrate: str = ""
    size: str = ""
    posted: str = ""
    # Prowlarr results only: which indexer, "torrent" or "usenet", and the raw release name.
    indexer: str = ""
    protocol: str = ""
    seeders: int | None = None
    release: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Page:
    items: list[Listing]
    page: int
    total_pages: int

    def to_dict(self) -> dict:
        return {
            "items": [i.to_dict() for i in self.items],
            "page": self.page,
            "total_pages": self.total_pages,
        }


@dataclass
class Details(Listing):
    narrators: list[str] = field(default_factory=list)
    description: str = ""
    length: str = ""
    info_hash: str = ""
    trackers: list[str] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)  # direct downloads: {url, name, size}
    download_url: str = ""  # Prowlarr: where to fetch the .nzb / .torrent (without the API key)
    asin: str = ""  # Audible catalog books
    series: list[dict] = field(default_factory=list)  # [{asin, title, sequence}]
    released: str = ""

    def magnet(self) -> str:
        if not self.info_hash:
            raise ValueError("No info hash available for this book")
        parts = [f"xt=urn:btih:{self.info_hash}", f"dn={quote(self.title)}"]
        parts += [f"tr={quote(t, safe='')}" for t in self.trackers]
        return "magnet:?" + "&".join(parts)


@dataclass
class Category:
    slug: str
    name: str


class Source(Protocol):
    name: str
    label: str
    description: str
    default_label: str  # what the unfiltered Discover view is called
    delivery: str  # "torrent" (via Real-Debrid), "direct" (plain HTTPS files) or "indexer" (Prowlarr: per release)

    def owns(self, url: str) -> bool: ...
    def categories(self) -> list[Category]: ...
    async def latest(self, page: int = 1, category: str | None = None) -> Page: ...
    async def search(self, query: str, page: int = 1) -> Page: ...
    async def details(self, url: str) -> Details: ...
