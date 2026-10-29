from .. import config
from .archive import ArchiveSource, librivox, oldtimeradio
from .audible import AudibleCatalog
from .audiobookbay import AudiobookBay
from .base import Category, Details, Listing, Page, Source
from .prowlarr import Prowlarr, ProwlarrError
from .torznab import Torznab, TorznabError

ORDER = ("librivox", "oldtimeradio", "audiobookbay", "prowlarr", "torznab", "audible")


def all_sources() -> dict[str, Source]:
    s = config.load()
    return {
        "audiobookbay": AudiobookBay(s.abb_base_url),
        "librivox": librivox(),
        "oldtimeradio": oldtimeradio(),
        "prowlarr": Prowlarr(s.prowlarr_url, s.prowlarr_api_key, s.prowlarr_categories),
        "torznab": Torznab(s.torznab_url, s.torznab_api_key, s.torznab_categories),
        "audible": AudibleCatalog(),
    }


def get_source(name: str = "audiobookbay") -> Source:
    sources = all_sources()
    if name not in sources:
        raise ValueError(f"Unknown source: {name}")
    return sources[name]


def source_for_url(url: str) -> Source | None:
    """Which source a book link belongs to (archive.org links are told apart later by the book's source name)."""
    for source in all_sources().values():
        if source.owns(url):
            return source
    return None


__all__ = [
    "ArchiveSource", "AudibleCatalog", "AudiobookBay", "Category", "Details", "Listing", "Page", "Prowlarr", "ProwlarrError", "Source", "Torznab", "TorznabError",
    "all_sources", "get_source", "source_for_url",
]
