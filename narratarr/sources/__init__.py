from .. import config
from .archive import ArchiveSource, librivox, oldtimeradio
from .audible import AudibleCatalog
from .audiobookbay import AudiobookBay
from .base import Category, Details, Listing, Page, Source
from .openlibrary import OpenLibrary
from .prowlarr import Prowlarr, ProwlarrError
from .torznab import Torznab, TorznabError

ORDER = ("librivox", "oldtimeradio", "audiobookbay", "prowlarr", "torznab", "audible", "openlibrary")
CATALOGS = ("audible", "openlibrary")  # where book details come from; the book itself comes from your indexers


def _torznab_key(indexer: dict) -> str:
    """The source name one indexer's books are stored under. The id keeps two indexers apart."""
    return f"torznab:{indexer['id']}" if indexer.get("id") not in ("", "torznab") else "torznab"


def torznab_sources() -> dict[str, Torznab]:
    """One source per configured Torznab indexer, labelled with the name the user gave it."""
    s = config.load()
    return {
        _torznab_key(entry): Torznab(entry["url"], entry["api_key"], entry.get("categories") or "3030",
                                     name=entry.get("name") or "Torznab", source_id=entry.get("id") or "torznab")
        for entry in config.torznab_entries(s)
    }


def _legacy_torznab_alias(found: dict[str, Source]) -> dict[str, Source]:
    """The bare name `torznab`, for a book grabbed before the list existed.

    Such a book stores `torznab` as its source; without the alias it would resolve to nothing and
    could not be downloaded again. The alias is resolution only — the API and the UI list the real
    names, so the same indexer is never offered twice.
    """
    if "torznab" in found:
        return {}
    first = next((v for k, v in found.items() if k.startswith("torznab:")), None)
    return {"torznab": first} if first is not None else {}


def torznab_names() -> list[str]:
    """The names the UI and the API list: one per indexer, without the compatibility alias."""
    return [_torznab_key(e) for e in config.torznab_entries(config.load())]


def order() -> tuple[str, ...]:
    """The sources in the order the UI shows them, with one entry per Torznab indexer."""
    names = torznab_names()
    if not names:
        return ORDER
    at = ORDER.index("torznab")
    return ORDER[:at] + tuple(names) + ORDER[at + 1:]


def all_sources() -> dict[str, Source]:
    s = config.load()
    base: dict[str, Source] = {
        "audiobookbay": AudiobookBay(s.abb_base_url),
        "librivox": librivox(),
        "oldtimeradio": oldtimeradio(),
        "prowlarr": Prowlarr(s.prowlarr_url, s.prowlarr_api_key, s.prowlarr_categories),
        "audible": AudibleCatalog(),
        "openlibrary": OpenLibrary(),
    }
    torznab = torznab_sources()
    return {**base, **torznab, **_legacy_torznab_alias({**base, **torznab})}


def ebook_indexers() -> Prowlarr:
    """Prowlarr, searching the ebook categories instead of the audiobook ones."""
    s = config.load()
    return Prowlarr(s.prowlarr_url, s.prowlarr_api_key, s.prowlarr_ebook_categories, fallback="7020")


def ebook_torznab() -> Torznab:
    """The first Torznab indexer, searching the ebook categories instead of the audiobook ones."""
    s = config.load()
    entries = config.torznab_entries(s)
    first = entries[0] if entries else {"url": "", "api_key": "", "name": "Torznab", "id": "torznab"}
    return Torznab(first["url"], first["api_key"], first.get("ebook_categories") or "7020",
                   name=first.get("name") or "Torznab", source_id=first.get("id") or "torznab")


def ebook_torznab_sources() -> dict[str, Torznab]:
    """Every Torznab indexer, searching its ebook categories."""
    s = config.load()
    return {
        _torznab_key(e): Torznab(e["url"], e["api_key"], e.get("ebook_categories") or "7020",
                                 name=e.get("name") or "Torznab", source_id=e.get("id") or "torznab")
        for e in config.torznab_entries(s)
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
    "CATALOGS", "ArchiveSource", "AudibleCatalog", "AudiobookBay", "Category", "Details", "Listing", "OpenLibrary", "Page",
    "Prowlarr", "ProwlarrError", "Source", "Torznab", "TorznabError", "all_sources", "ebook_indexers", "ebook_torznab",
    "ebook_torznab_sources", "get_source", "order", "source_for_url", "torznab_names", "torznab_sources",
]
