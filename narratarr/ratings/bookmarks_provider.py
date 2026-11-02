"""Bookmarks as a ratings provider, unchanged behind the interface.

The loader, the fuzzy matcher and the surname gate all stay in `narratarr/bookmarks.py`; this is the
thin adapter that turns one of its entries into a `Rating`. Nothing about the Bookmarks result may
change, so the score stays on its 0–4 scale, no vote count is shown, and the second half of the pill
is still the consensus line.

It deliberately does not go through `make_rating`: the 50-vote floor exists to stop a handful of
ratings topping a list, but a Bookmarks R is already a weighted figure built from a critique
consensus, and the dataset is curated. Applying the floor here would blank every existing rating,
which is exactly what "existing behaviour must not change" forbids.
"""

from __future__ import annotations

from .. import bookmarks
from . import Rating


class BookmarksProvider:
    name = "bookmarks"
    label = "Bookmarks"

    def __init__(self, store=None):
        self.store = store  # Bookmarks is a local dataset; there is nothing to cache remotely

    @property
    def enabled(self) -> bool:
        return bool(bookmarks._get_entries())

    def cached(self, title: str, author: str) -> Rating | None:
        entry = bookmarks.lookup(title, author)
        if not entry:
            return None
        r = float(entry["R"])
        flags = entry.get("flags") or {}
        reviews = sum(int(v or 0) for v in flags.values())
        return Rating(
            score=r,
            count=reviews or None,
            scale=4.0,
            source="bookmarks",
            url=entry.get("url", ""),
            label=entry.get("consensus", ""),
            tooltip=f"{r:.1f} from {reviews} critics on Bookmarks — {entry.get('consensus', '')}".strip(" —"),
            extra={"R": r, "consensus": entry.get("consensus", ""), "flags": flags},
        )

    async def fetch_and_store(self, title: str, author: str) -> Rating | None:
        return self.cached(title, author)  # local data: nothing to fetch
