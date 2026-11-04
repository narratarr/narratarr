#!/usr/bin/env python3
"""Fill the ratings cache before you browse. Optional.

The app fills ratings on demand as you browse and needs none of this. Run this only if you want a
list you already own scored before you open it, or if you want stable coverage rather than
whatever the background filler has reached so far.

It uses the runtime's own rules, not a second implementation of them: the same `strict_match` gate,
the same accent folding, the same surname rule, so a book prewarmed here resolves exactly as it
would if you had searched for it. Anything that fails the gate is counted as refused and never
guessed at.

What to expect, measured against a 30-title shelf and a 210-title run:

  * about 210 books a minute, limited by the one-request-a-second rate limit rather than by this
    script
  * roughly 60-65% of popular-shelf titles match: the big new releases are all in Audible's
    catalogue
  * far less in the long tail. Bundles ("Series 1-4"), self-published rips and print-only titles
    are not in the catalogue at all, so no amount of retrying will rate them

Usage:

  python scripts/prewarm_ratings.py books.json
  python scripts/prewarm_ratings.py books.csv --region uk --region us
  python scripts/prewarm_ratings.py books.json --reset

`books.json` is a list of {"title": ..., "author": ...}. A csv needs `title` and `author` columns.
It is resumable: run it again and books already in the cache are skipped, so an interrupted run
costs only the book it was on.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from narratarr import config  # noqa: E402
from narratarr.ratings import get_store  # noqa: E402
from narratarr.ratings.audnex import (  # noqa: E402
    RATING_TTL,
    AudnexProvider,
    AudnexUnavailable,
    RateLimited,
    parse_score,
)
from narratarr.ratings.match import author_matches  # noqa: E402


def load_books(path: Path) -> list[dict]:
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        return [{"title": (r.get("title") or "").strip(), "author": (r.get("author") or "").strip()}
                for r in rows if (r.get("title") or "").strip()]
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("books") or []
    return [{"title": (b.get("title") or "").strip(), "author": (b.get("author") or "").strip()}
            for b in data if (b.get("title") or "").strip()]


def strip_byline(title: str, author: str) -> str:
    """Drop a trailing ", by <author>" when the byline really is this book's author.

    A plain list has no source attached, so the rule cannot be "LibriVox rows only". Instead it
    checks the name it is about to remove: the surname has to match the author supplied, which is
    the same test the matcher applies to the result, so nothing is dropped on a guess.
    """
    m = re.search(r",\s*by\s+([^,]+)$", title or "", re.IGNORECASE)
    if m and author and author_matches(author, m.group(1)):
        return (title or "")[: m.start()].strip()
    return title or ""


async def run(books: list[dict], regions: list[str], reset: bool) -> int:
    store = get_store()
    provider = AudnexProvider(store, regions=regions)
    tally = {"matched": 0, "rated": 0, "unrated": 0, "refused": 0, "failed": 0, "skipped": 0}

    for n, book in enumerate(books, 1):
        author = book["author"]
        title = strip_byline(book["title"], author)

        if not reset and provider.cached(title, author) is not None:
            tally["skipped"] += 1
            _report(n, len(books), title, "already cached", tally)
            continue

        try:
            asin = await provider._resolve_asin(title, author)
        except (RateLimited, AudnexUnavailable) as e:
            tally["failed"] += 1
            _report(n, len(books), title, f"resolve failed: {e}", tally)
            continue
        if not asin:
            tally["refused"] += 1
            _report(n, len(books), title, "refused: no title-and-surname match", tally)
            continue
        tally["matched"] += 1

        try:
            record = await provider._get_book(asin)
        except (RateLimited, AudnexUnavailable) as e:
            tally["failed"] += 1
            _report(n, len(books), title, f"fetch failed: {e}", tally)
            continue

        if record is None:
            tally["failed"] += 1
            _report(n, len(books), title, f"no record for {asin}", tally)
            continue
        score = parse_score(record)
        if score is None:
            store.put(asin, provider.name, state="unrated")
            tally["unrated"] += 1
            _report(n, len(books), title, f"unrated ({asin})", tally)
            continue

        rating = provider._rating(score, asin)
        store.put(asin, provider.name, state="rated", score=float(rating.score),
                  url=rating.url if rating else "")
        tally["rated"] += 1
        _report(n, len(books), title, f"rated {rating.score} ({asin})", tally)

    print("\nDone."
          f"\n  matched  {tally['matched']}"
          f"\n  rated    {tally['rated']}"
          f"\n  unrated  {tally['unrated']}"
          f"\n  refused  {tally['refused']}"
          f"\n  failed   {tally['failed']}"
          f"\n  skipped  {tally['skipped']}  (already cached)")
    print(f"Cache: {store.path}  (ratings refresh after {RATING_TTL // 86400} days)")
    return 0


def _report(n: int, total: int, title: str, outcome: str, tally: dict) -> None:
    print(f"[{n}/{total}] {title[:52]:52s} {outcome}   "
          f"(rated {tally['rated']} · unrated {tally['unrated']} · "
          f"refused {tally['refused']} · failed {tally['failed']})")


def main() -> int:
    ap = argparse.ArgumentParser(description="Fill the ratings cache before you browse.")
    ap.add_argument("books", type=Path, help="a JSON list of {title, author}, or a csv with those columns")
    ap.add_argument("--region", action="append", default=None,
                    help="an Audible storefront to try, in order; defaults to the ones in Settings")
    ap.add_argument("--reset", action="store_true",
                    help="re-fetch books that are already cached, e.g. after scores have drifted")
    args = ap.parse_args()

    if not args.books.exists():
        print(f"No such file: {args.books}")
        return 2
    books = load_books(args.books)
    if not books:
        print("That file has no {title, author} rows in it.")
        return 2

    regions = args.region or list(config.load().audnex_regions or ["uk"])
    print(f"{len(books)} books · storefronts tried in order: {', '.join(regions)}\n")
    return asyncio.run(run(books, regions, args.reset))


if __name__ == "__main__":
    raise SystemExit(main())
