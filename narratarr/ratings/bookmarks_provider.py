"""Bookmarks as a ratings provider, unchanged behind the interface.

The loader, the fuzzy matcher and the surname gate all stay in `narratarr/bookmarks.py`; this is the
thin adapter that turns one of its entries into a `Rating`. Nothing about the Bookmarks result may
change, so the score stays on its 0–4 scale, no vote count is shown, and the second half of the pill
is still the consensus line.

It goes through `make_rating` like every other provider. It used to bypass it: the floor was a
global 50, which is a Goodreads-scale number, and every entry here carries 7 to 10 critic reviews,
so the floor would have blanked the whole dataset. The floor is now per source — Bookmarks clears
at 3 — so the gate is safe to use and there is one place that decides, not two.
"""

from __future__ import annotations

from .. import bookmarks
from . import Rating, make_rating


class BookmarksProvider:
    name = "bookmarks"
    label = "Bookmarks"

    def __init__(self, store=None):
        self.store = store  # Bookmarks is a local dataset; there is nothing to cache remotely

    @property
    def enabled(self) -> bool:
        return bool(bookmarks._get_entries())

    def cached(self, title: str, author: str, asin: str = "") -> Rating | None:
        entry = bookmarks.lookup(title, author)
        if not entry:
            return None
        r = float(entry["R"])
        flags = entry.get("flags") or {}
        reviews = sum(int(v or 0) for v in flags.values())
        consensus = entry.get("consensus", "")
        return make_rating(
            r, reviews or None, entry.get("url", ""), "bookmarks",
            label=consensus,
            tooltip=f"{r:.1f} from {reviews} critics on Bookmarks — {consensus}".strip(" —"),
            extra={"R": r, "consensus": consensus, "flags": flags},
        )

    async def fetch_and_store(self, title: str, author: str, asin: str = "") -> Rating | None:
        return self.cached(title, author)  # local data: nothing to fetch
