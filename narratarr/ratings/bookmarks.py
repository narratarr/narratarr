"""Bookmarks as a ratings provider.

Bookmarks (bookmarks.reviews) publishes a critic consensus for a book: a weighted score on a 0-4
scale plus the counts behind it (rave, positive, mixed, pan). The dataset is a local file, so this
provider never touches the network: `fetch_and_store` is `cached`, and the background filler has
nothing to fill.

Matching goes through the same gate as every other provider, `strict_match` in `.match`. The old
standalone loader had its own fuzzy title matcher and surname check; that is gone deliberately. Two
matchers are two ways to be wrong, and the gate here is the one that has tests.

The floor is per source (`MIN_COUNT_BY_SOURCE` in `.__init__`): Bookmarks counts critic reviews, so
it clears at 3, where a Goodreads-style floor of 50 would have refused every entry in the dataset.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from . import Rating, make_rating
from .match import strict_match

log = logging.getLogger("narratarr.ratings.bookmarks")

# Where the dataset may live. First hit wins, so a file in the data dir beats one shipped in the repo.
JSON_NAMES = ("bookmarks.json",)
DB_NAMES = ("bookmarks.db",)


def _candidates(names: tuple[str, ...]) -> list[Path]:
    from .. import config

    roots = [Path(__file__).resolve().parent.parent.parent, config.DATA_DIR, Path("/config"), Path("/app/data")]
    out: list[Path] = []
    for root in roots:
        for name in names:
            path = Path(root) / name
            if path not in out:
                out.append(path)
    return out


def _from_json(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("books") or data.get("entries") or []
    return [e for e in data if isinstance(e, dict) and e.get("title")]


def _from_db(path: Path) -> list[dict]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT * FROM bookmarks").fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        out.append({
            "title": r["title"], "author": r["author"], "url": r["url"], "consensus": r["consensus"],
            "R": r["R"],
            "flags": {"rave": r["rave"], "positive": r["positive"], "mixed": r["mixed"], "pan": r["pan"]},
        })
    return out


def load_entries() -> list[dict]:
    """The dataset, from whichever copy exists. Empty when there is none, which means 'off'."""
    for path in _candidates(JSON_NAMES):
        if path.is_file():
            try:
                entries = _from_json(path)
                if entries:
                    return entries
            except (OSError, ValueError) as e:
                log.warning("bookmarks: %s is not readable (%s)", path, e)
    for path in _candidates(DB_NAMES):
        if path.is_file():
            try:
                entries = _from_db(path)
                if entries:
                    return entries
            except (OSError, sqlite3.Error) as e:
                log.warning("bookmarks: %s is not readable (%s)", path, e)
    return []


_cache: list[dict] | None = None


def entries() -> list[dict]:
    global _cache
    if _cache is None:
        _cache = load_entries()
    return _cache


def reset_for_tests() -> None:
    global _cache
    _cache = None


def reviews_of(entry: dict) -> int:
    return sum(int(v or 0) for v in (entry.get("flags") or {}).values())


class BookmarksProvider:
    """A local dataset behind the same contract as Audnex."""

    name = "bookmarks"
    label = "Bookmarks"

    def __init__(self, store=None):
        self.store = store  # nothing to cache: the dataset is the cache

    @property
    def enabled(self) -> bool:
        return bool(entries())

    def state(self, title: str, author: str, asin: str = "") -> str:
        return "none"  # an entry is either rated or absent, never "exists but unrated"

    def cached(self, title: str, author: str, asin: str = "") -> Rating | None:
        entry = self._find(title, author)
        if entry is None:
            return None
        score = float(entry.get("R") or 0.0)
        count = reviews_of(entry) or None
        consensus = str(entry.get("consensus") or "")
        rating = make_rating(
            score, count, str(entry.get("url") or ""), self.name,
            label=consensus,
            # Bookmarks scores out of 4, everything else out of 5. The tooltip says so, because
            # "3.7" next to Audible's "4.5" would otherwise read as a worse book on a shared scale.
            tooltip=f"{score:.1f} out of 4 from {count or 1} critics on Bookmarks"
                    + (f" — {consensus}" if consensus else ""),
            extra={"R": score, "consensus": consensus, "flags": entry.get("flags") or {}},
        )
        if rating is not None:
            rating.scale = 4.0
        return rating

    def _find(self, title: str, author: str) -> dict | None:
        """The one gate. Title must match outright and the query's author must be in the entry's."""
        for entry in entries():
            if strict_match(title, author, str(entry.get("title") or ""), str(entry.get("author") or "")):
                return entry
        return None

    async def fetch_and_store(self, title: str, author: str, asin: str = "") -> Rating | None:
        return self.cached(title, author, asin)  # local data: nothing to fetch
