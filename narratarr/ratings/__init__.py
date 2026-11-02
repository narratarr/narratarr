"""Ratings, from whichever provider the instance is set to.

The contract is deliberately small: a provider turns a title/author into a `Rating` or nothing.
Everything else — the pill, the sort, the cache, the background filler — lives here so a new
provider only has to answer one question.

Two rules the rest of the app relies on:

* **Nothing here makes the page wait.** `enrich()` reads the cache and, on a miss, hands the book to
  the background filler. The item comes back unrated and the next search shows the rating.
* **A rating with too few votes is not a rating.** Anything under `MIN_COUNT` is returned as a miss,
  so a book with 12 ratings cannot sit at the top of a Discover page.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .match import MIN_COUNT
from .store import Store

log = logging.getLogger("narratarr.ratings")

PRIOR_SCORE = 3.9       # the average a new book is treated as having, so few votes pull toward it
PRIOR_WEIGHT = 1000     # how many real votes the prior is worth
UNKNOWN_COUNT_WEIGHT = 100  # what a rating with no count is worth: real, but weak evidence


@dataclass
class Rating:
    """One book's score, as a provider sees it."""

    score: float
    count: int | None = None
    scale: float = 5.0
    source: str = ""
    url: str = ""
    label: str = ""            # what the second part of the pill says, e.g. "5.0M" or a consensus
    tooltip: str = ""          # the full sentence, for the title attribute
    extra: dict = field(default_factory=dict)

    def bayes(self) -> float:
        """The sortable score.

        A raw average cannot rank: twelve people giving 5.0 would beat two million giving 4.3. The
        prior is worth PRIOR_WEIGHT votes at PRIOR_SCORE, so a thinly-rated book is pulled towards
        the mean and only separates from it once it has real weight behind it.

        Some sources give a bare number and no count (Audnex returns "4.9" and nothing else). That
        is real evidence with unknown weight, so it is treated as a small fixed number of votes
        rather than as zero — zero would bury every such book at the prior and make the sort
        meaningless for the whole provider.
        """
        n = UNKNOWN_COUNT_WEIGHT if self.count is None else self.count
        return (PRIOR_WEIGHT * PRIOR_SCORE + n * float(self.score)) / (PRIOR_WEIGHT + n)

    def to_dict(self) -> dict:
        return {
            "score": round(float(self.score), 2),
            # Goodreads averages are quoted to two places ("4.30"); Bookmarks' R to one ("3.9").
            "score_text": f"★ {float(self.score):.2f}" if self.source == "goodreads"
            else f"★ {float(self.score):.1f}",
            "count": self.count,
            "scale": self.scale,
            "source": self.source,
            "url": self.url,
            "label": self.label,
            "display": self.tooltip or f"{self.score:.1f} on {self.source}",
            "tooltip": self.tooltip,
            "bayes": round(self.bayes(), 4),
            **self.extra,
        }


def compact_count(n: int | None) -> str:
    """5044634 -> '5.0M'. The pill has one line, so the number has to be short."""
    if not n:
        return ""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1000:.1f}K"
    if n >= 1000:
        return f"{n / 1000:.1f}K"
    return str(n)


# ---- providers ----------------------------------------------------------------------------


def available() -> dict[str, str]:
    """Provider keys the user can pick in Settings."""
    return {"bookmarks": "Bookmarks", "goodreads": "Goodreads (unofficial)"}


def make_rating(score, count, url: str, source: str, label: str = "", tooltip: str = "") -> Rating | None:
    """The one place a score becomes a rating, and the one place the vote floor is applied.

    Below MIN_COUNT ratings a score is noise, and a thin 5.0 outranks a solid 4.3 on any honest
    list. Refusing here means no caller has to remember the rule.
    """
    if score is None:
        return None
    # A count is optional: some sources publish a bare average. The vote floor can only be applied
    # to a count we actually have, so a missing one is not a reason to throw the score away.
    if count is not None:
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = None
    if count is not None and count < MIN_COUNT:
        return None
    source_label = {"goodreads": "Goodreads", "bookmarks": "Bookmarks"}.get(source, source)
    if tooltip:
        pass
    elif count is None:
        tooltip = f"{float(score):.2f} average on {source_label}"
    else:
        tooltip = f"{float(score):.2f} average from {count:,} ratings on {source_label}"
    return Rating(
        score=float(score),
        count=count,
        scale=5.0 if source != "bookmarks" else 4.0,
        source=source,
        url=url or "",
        label=label or compact_count(count),
        tooltip=tooltip,
    )


def provider(settings=None):
    """The provider the instance is set to, or None when ratings are off."""
    from .. import config

    s = settings if settings is not None else config.load()
    name = getattr(s, "ratings_provider", "") or ""
    store = get_store(s)
    if name == "bookmarks":
        from .bookmarks_provider import BookmarksProvider

        return BookmarksProvider(store)
    if name == "goodreads":
        from .goodreads import GoodreadsProvider

        return GoodreadsProvider(store)
    return None


_store = None


def get_store(settings=None):
    """One cache per process, under the data dir. The path is gitignored."""
    global _store
    from .. import config

    if _store is None:
        _store = Store(config.DATA_DIR / "ratings.sqlite")
    return _store


def reset_for_tests() -> None:
    global _store
    _store = None
    _filler.clear()


# ---- the page path ------------------------------------------------------------------------

_queue: asyncio.Queue = asyncio.Queue()
_filler: set[asyncio.Task] = set()


def enrich(item: dict, provider_obj=None, settings=None) -> dict:
    """Attach `rating` to a Discover item from the cache alone, and queue a miss for later.

    Always sets the key — the UI reserves a slot per card and reads `item.rating` without a guard.
    """
    item["rating"] = None
    p = provider_obj if provider_obj is not None else provider(settings)
    if p is None:
        return item
    try:
        rating = p.cached(item.get("title") or "", item.get("author") or "")
    except Exception as e:  # a ratings problem must never fail a search
        log.warning("ratings lookup failed for %r: %s", item.get("title"), e)
        return item
    if rating is not None:
        item["rating"] = rating.to_dict()
    elif p.enabled:
        _enqueue(item.get("title") or "", item.get("author") or "")
    return item


def sort_items(items: list[dict]) -> list[dict]:
    """Sort by the Bayesian score, unrated last."""
    return sorted(items, key=lambda i: (i.get("rating") or {}).get("bayes", -1), reverse=True)


def _enqueue(title: str, author: str) -> None:
    if not title or _queue.qsize() > 500:
        return
    _queue.put_nowait((title, author))


async def _drain() -> None:
    """One book at a time, at the provider's own pace. Never started in tests."""
    p = provider()
    while True:
        title, author = await _queue.get()
        try:
            if p is not None:
                await p.fetch_and_store(title, author)
        except Exception as e:
            log.warning("ratings fetch failed for %r: %s", title, e)
        finally:
            _queue.task_done()


def start_filler() -> None:
    task = asyncio.create_task(_drain())
    _filler.add(task)
    task.add_done_callback(_filler.discard)


def stop_filler() -> None:
    for task in list(_filler):
        task.cancel()
    _filler.clear()
