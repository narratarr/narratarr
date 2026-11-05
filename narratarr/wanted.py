"""Wanted books: finding releases for them, picking one automatically, and following authors and series.

A Wanted book that came from a catalog (Audible or Open Library) has no release
yet. Narratarr searches Prowlarr (and LibriVox, for public-domain audiobooks) for
it, scores each result against the book's title and author, and grabs the best one
that a configured download client can handle. Ebooks are searched for in Prowlarr's
ebook categories instead. Followed authors and series add their new books to Wanted
on their own.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import date
from urllib.parse import urlencode

from rapidfuzz import fuzz

from . import audible, config, db, downloaders, ebooks, languages, notify
from .search import clean_title, normalize
from .sources import CATALOGS, ProwlarrError, TorznabError, ebook_indexers, ebook_torznab, get_source

log = logging.getLogger("narratarr.wanted")

AUTO_MATCH = 85  # how sure we must be before grabbing a release on our own
SEARCHES_PER_ROUND = 3  # be gentle with indexer API limits
FOLLOW_REFRESH_SECONDS = 12 * 3600
AUDIO_FORMATS = {"M4B", "MP3", "FLAC", "AAC", "M4A", "OGG", "OPUS"}


def kind_of(b: dict) -> str:
    return b.get("kind") or "audiobook"


def release_kind(item: dict) -> str:
    """What a release holds, going by its format: "audiobook", "ebook", or "" when it doesn't say.

    (Prowlarr releases name an audio format first when they have one: "Dune (PDF + M4B)" is an M4B.)"""
    fmt = (item.get("format") or "").upper()
    return "audiobook" if fmt in AUDIO_FORMATS else "ebook" if fmt in ebooks.FORMATS else ""


def main_title(title: str) -> str:
    """'The Final Empire: Mistborn, Book 1' -> 'The Final Empire'."""
    return re.split(r"\s*[:(\[]|\s+-\s+", title, maxsplit=1)[0].strip() or title


def first_author(author: str) -> str:
    return re.split(r",|&| and ", author or "", maxsplit=1)[0].strip()


def match(book: dict, item: dict) -> int:
    """0-100: how sure we are that a search result is this book."""
    title = normalize(main_title(book["title"]))
    author = normalize(first_author(book["author"]))
    if item.get("author"):
        t = fuzz.token_set_ratio(title, normalize(main_title(item["title"])))
        a = fuzz.token_set_ratio(author, normalize(item["author"])) if author else 50
    else:  # Prowlarr releases are just a name: "Author - Title (Year) [M4B]"
        text = normalize(item.get("release") or item.get("title") or "")
        t = fuzz.partial_ratio(title, text) if len(title) >= 4 else fuzz.token_set_ratio(title, text)
        surname = author.split()[-1] if author else ""
        a = 100 if surname and surname in text.split() else fuzz.partial_ratio(author, text) if author else 50
    if t < 70:
        return round(t * 0.6)
    score = round(0.7 * t + 0.3 * a)
    return min(score, 75) if author and a < 75 else score  # the right title by someone else isn't a match


def usable(item: dict, s: config.Settings) -> bool:
    """Whether a configured download client can fetch this result."""
    if item.get("source") in ("librivox", "oldtimeradio"):
        return True
    if item.get("protocol") == "usenet":
        return downloaders.usenet_configured(s)
    return downloaders.torrent_configured(s)


def _preference(item: dict, kind: str = "audiobook") -> tuple:
    free = item.get("source") in ("librivox", "oldtimeradio")
    favorite = (item.get("format") or "").upper() == ("EPUB" if kind == "ebook" else "M4B")
    seeders = item.get("seeders")
    alive = 1 if seeders is None or seeders > 0 else 0
    return (item["match"] >= 95, alive, free, favorite, seeders if seeders is not None else 50)


async def find_releases(book: dict) -> list[dict]:
    """Search everywhere a book could come from; best candidates first."""
    s = config.load()
    kind = kind_of(book)
    title, author = main_title(book["title"]), first_author(book["author"])
    query = f"{title} {author}".strip()
    wanted_languages = languages.parse(s.languages)
    jobs = {}
    if kind == "audiobook":
        jobs["librivox"] = get_source("librivox").find(title, author)
    if s.prowlarr_url and s.prowlarr_api_key:
        jobs["prowlarr"] = (ebook_indexers() if kind == "ebook" else get_source("prowlarr")).search(query)
    if s.torznab_url and s.torznab_api_key:
        jobs["torznab"] = (ebook_torznab() if kind == "ebook" else get_source("torznab")).search(query)
    if not jobs:
        return []
    results = await asyncio.gather(*jobs.values(), return_exceptions=True)
    found, errors = [], []
    for name, res in zip(jobs, results):
        if isinstance(res, BaseException):
            errors.append(str(res) if isinstance(res, (ProwlarrError, TorznabError)) else f"Couldn't search {name}.")
            continue
        for listing in res.items:
            item = {**listing.to_dict(), "source": name}
            if release_kind(item) not in ("", kind):
                continue  # an indexer filed it under the wrong category
            item["match"] = match(book, item)
            item["usable"] = usable(item, s)
            # What language it's in, from the source or the release name (the book's own words don't count).
            item["language"] = languages.name(item.get("language")) or languages.detect(
                item.get("release") or item.get("title") or "", ignore=f"{book['title']} {book['author']}")
            item["language_ok"] = languages.allowed(item["language"], wanted_languages)
            if item["match"] >= 60:
                found.append(item)
    if not found and errors:
        raise ProwlarrError(errors[0])
    found.sort(key=lambda i: (i["usable"], i["language_ok"], *_preference(i, kind)), reverse=True)
    return found


def best(found: list[dict], skip: list[str] | tuple = ()) -> dict | None:
    """The release to take on our own: usable, in a wanted language, a sure match, not turned down before."""
    for item in found:
        if item["url"] in skip:
            continue
        if item["usable"] and item.get("language_ok", True) and item["match"] >= AUTO_MATCH and item.get("seeders") != 0:
            return item
    return None


async def grab(book_id: int, url: str, source_name: str, status: str = "queued") -> dict:
    """Attach a release to a Wanted book and queue it (or hold it for approval), keeping the book's own metadata."""
    book = db.get(book_id)
    src = get_source(source_name)
    d = await src.details(url)
    db.update(
        book_id,
        source=src.name, release_url=url, protocol=d.protocol, download_url=d.download_url, info_hash=d.info_hash,
        trackers=d.trackers, files=d.files, format=d.format or book["format"], bitrate=d.bitrate,
        size=d.size or book["size"], indexer=d.indexer, release=d.release or d.title,
        description=book["description"] or d.description, cover_url=book["cover_url"] or d.cover_url,
        status=status, error="", detail="", progress=0, client_job_id="", download_client="", rd_torrent_id="",
        search_note="", found_by_search=0,
    )
    return db.get(book_id)


def keep_looking(book: dict) -> dict:
    """Turn down the release a search found for a book and go back to looking. That release is
    remembered, so the next search doesn't pick it again."""
    source = next((n for n in CATALOGS if get_source(n).owns(book["source_url"].split("#")[0])), "")
    if not source:
        raise ValueError("This book didn't come from a catalog, so there's nothing to go back to looking for.")
    skipped = list(dict.fromkeys([*(book.get("skipped_releases") or []), book["release_url"]]))
    db.update(
        book["id"], source=source, status="wanted", release_url="", protocol="", download_url="", info_hash="",
        trackers=[], files=[], indexer="", release="", found_by_search=0, skipped_releases=[u for u in skipped if u],
        error="", detail="", search_note="Turned down a release. Still looking.", last_searched=time.time(),
    )
    return db.get(book["id"])


async def search_and_grab(book: dict, hold: bool = False) -> dict | None:
    """Look for a Wanted book once. Returns the chosen release, or None if nothing good turned up.

    With `hold`, the release isn't downloaded: the book waits under Awaiting approval with it
    picked, and the admins are told, so someone can check its language, format and size first."""
    db.update(book["id"], last_searched=time.time())
    try:
        found = await find_releases(book)
    except ProwlarrError as e:
        db.update(book["id"], search_note=str(e))
        raise
    pick = best(found, book.get("skipped_releases") or [])
    if not pick:
        close = [f for f in found if f["match"] >= AUTO_MATCH]
        note = (
            "Found matches, but none your download clients can fetch." if close
            else f"Nothing that matches yet ({len(found)} near misses)." if found else "Nothing found yet."
        )
        if not found and kind_of(book) == "ebook" and (s := config.load()).prowlarr_url and s.prowlarr_api_key:
            indexers = ebook_indexers()
            if await indexers.offering() == []:  # say why, rather than "nothing found" forever
                note = (f"None of your Prowlarr indexers offer ebooks (category {', '.join(indexers.cats)}). "
                        "Add one that does in Prowlarr.")
        db.update(book["id"], search_note=note)
        return None
    if hold:
        await grab(book["id"], pick["url"], pick["source"], "requested")
        db.update(book["id"], found_by_search=1)
        notify.emit("found", db.get(book["id"]))
        return pick
    await grab(book["id"], pick["url"], pick["source"])
    return pick


def _released(b: dict) -> bool:
    try:
        return date.fromisoformat(b.get("released") or "1900-01-01") <= date.today()
    except ValueError:
        return True


def due_for_search() -> list[dict]:
    s = config.load()
    cutoff = time.time() - s.auto_search_hours * 3600
    due = [
        b for b in db.list_books()
        if b["status"] == "wanted" and b["source"] in CATALOGS and _released(b)
        and (b["last_searched"] or 0) < cutoff
    ]
    due.sort(key=lambda b: b["last_searched"] or 0)
    return due[:SEARCHES_PER_ROUND]


async def auto_search() -> int:
    """One round of automatic searching. Returns how many books were grabbed (or held for approval)."""
    s = config.load()
    if not (s.auto_search and ((s.prowlarr_url and s.prowlarr_api_key) or (s.torznab_url and s.torznab_api_key))):
        return 0
    grabbed = 0
    for b in due_for_search():
        try:
            if await search_and_grab(b, hold=s.found_needs_approval):
                grabbed += 1
        except Exception as e:  # one bad search shouldn't stop the round
            log.info("Automatic search for %s failed: %s", b["title"], e)
    return grabbed


# ---- filling in a library book's details from Audible -------------------------


async def audible_match(book: dict) -> dict | None:
    """Audible's record of this book: by its ASIN when it has one, else the search result whose title
    and author both match. Nothing at all rather than a near miss: a wrong series is worse than none."""
    if book.get("asin"):
        return await audible.book(book["asin"], r=audible.region_from_url(book.get("source_url") or "") or None)
    title, author = main_title(book.get("title") or ""), first_author(book.get("author") or "")
    if not title or not author:
        return None
    found, _ = await audible.search_books(f"{title} {author}")
    return next((b for b in found if _titles_match(b["title"], b["author"], book["title"], book["author"])), None)


def missing_details(book: dict) -> bool:
    return not (book.get("series") and book.get("narrators") and book.get("released"))


async def fill_from_audible(book: dict) -> list[str]:
    """Fill only what the book is missing (series, narrators, year, ASIN) from Audible. Returns what changed."""
    db.update(book["id"], details_checked=time.time())
    match = await audible_match(book)
    if not match:
        return []
    changes: dict = {}
    if not book.get("series") and match.get("series"):
        changes["series"] = [{**s, "title": audible.series_name(s["title"])} for s in match["series"][:1]]
    if not book.get("narrators") and match.get("narrators"):
        changes["narrators"] = match["narrators"][:4]
    if not book.get("released") and match.get("released"):
        changes["released"] = match["released"]
    if not book.get("asin") and match.get("asin"):
        changes["asin"] = match["asin"]
    if changes:
        db.update(book["id"], **changes)
    return list(changes)


# ---- library helpers ----------------------------------------------------------


def _titles_match(a_title: str, a_author: str, b_title: str, b_author: str) -> bool:
    """Whether two titles by the same author are the same book. "Dune" is "Dune (Unabridged)" and
    "Dune: Deluxe Edition", but "Star Wars: Thrawn" isn't "Star Wars: Thrawn: Alliances"."""
    if normalize(first_author(a_author)) != normalize(first_author(b_author)):
        return False
    a, b = normalize(clean_title(a_title)), normalize(clean_title(b_title))
    return a == b or a == normalize(main_title(b_title)) or normalize(main_title(a_title)) == b


def in_library(asin: str, title: str, author: str, books: list[dict] | None = None,
               kind: str = "audiobook") -> dict | None:
    """The library's copy of a book in one format (any edition, from any source), if it has one."""
    for b in books if books is not None else db.list_books():
        if kind_of(b) == kind and ((asin and b.get("asin") == asin)
                                   or _titles_match(title, author, b["title"], b["author"])):
            return b
    return None


def owned_index(owned: list[dict]) -> tuple[set, dict]:
    """Books from somewhere else (Audiobookshelf), indexed so matching an author's whole list is quick."""
    by_asin = {o["asin"] for o in owned if o.get("asin")}
    by_author: dict[str, list[dict]] = {}
    for o in owned:
        by_author.setdefault(normalize(first_author(o["author"])), []).append(o)
    return by_asin, by_author


def in_owned(b: dict, index: tuple[set, dict]) -> bool:
    by_asin, by_author = index
    if b.get("asin") and b["asin"] in by_asin:
        return True
    return any(_titles_match(b["title"], b["author"], o["title"], o["author"])
               for o in by_author.get(normalize(first_author(b.get("author") or "")), []))


# The same book in its other format shares a folder with it, so this is stricter than in_library:
# two different ASINs are two different editions, which shouldn't share a folder.


def _full_key(b: dict) -> tuple[str, str]:
    return normalize(clean_title(b["title"])), normalize(first_author(b["author"]))


def _main_key(b: dict) -> tuple[str, str]:
    return normalize(main_title(b["title"])), normalize(first_author(b["author"]))


def same_book(a: dict, b: dict) -> bool:
    if a.get("asin") and b.get("asin"):
        return a["asin"] == b["asin"]
    return _titles_match(a["title"], a["author"], b["title"], b["author"])


def companion(book: dict, books: list[dict] | None = None) -> dict | None:
    """The same book in the other format: the ebook of an audiobook, or the other way round."""
    other = "ebook" if kind_of(book) == "audiobook" else "audiobook"
    for b in books if books is not None else db.list_books():
        if kind_of(b) == other and same_book(book, b):
            return b
    return None


def companions(books: list[dict]) -> dict[int, dict]:
    """Every book's other format in the library, by book id (companion() for a whole list at once)."""
    by_asin: dict[tuple, dict] = {}
    by_full: dict[tuple, list[dict]] = {}
    by_main: dict[tuple, list[dict]] = {}
    for b in books:
        if b.get("asin"):
            by_asin.setdefault((kind_of(b), b["asin"]), b)
        by_full.setdefault((kind_of(b), *_full_key(b)), []).append(b)
        by_main.setdefault((kind_of(b), *_main_key(b)), []).append(b)
    found = {}
    for b in books:
        other = "ebook" if kind_of(b) == "audiobook" else "audiobook"
        hit = by_asin.get((other, b["asin"])) if b.get("asin") else None
        if hit is None:
            near = (by_full.get((other, *_full_key(b)), []) + by_main.get((other, *_full_key(b)), [])
                    + by_full.get((other, *_main_key(b)), []))
            hit = next((c for c in near if same_book(b, c)), None)
        if hit:
            found[b["id"]] = hit
    return found


def url_for(url: str, kind: str) -> str:
    """Each format of a catalog book is its own library row, so the ebook's link gets a marker."""
    return url if kind == "audiobook" else f"{url}#ebook"


def add_wanted(b: dict, kind: str = "audiobook", source: str = "audible", status: str = "wanted") -> dict | None:
    """Add a catalog book to Wanted in one format, unless the library already has it in that format."""
    url = url_for(b["url"], kind)
    if in_library(b.get("asin") or "", b["title"], b["author"], kind=kind) or db.get_by_url(url):
        return None
    audio = kind == "audiobook"
    return db.insert({
        "source": source, "source_url": url, "asin": b.get("asin") or "", "title": b["title"], "author": b["author"],
        "narrators": b.get("narrators") or [] if audio else [], "cover_url": b.get("cover_url") or "",
        "description": b.get("description") or "", "language": b.get("language") or "",
        "series": b.get("series") or [], "released": b.get("released") or "", "status": status, "kind": kind,
        # Audible's runtime is the audiobook's; Open Library's page count is the book's.
        "length": b.get("length") or "" if audio == (source == "audible") else "",
    })


def want_other_format(book: dict, status: str = "wanted") -> tuple[dict, bool]:
    """Add the other format of a library book to Wanted. Returns (that row, whether it's new)."""
    kind = "ebook" if kind_of(book) == "audiobook" else "audiobook"
    have = companion(book)
    if have:
        return have, False
    if book["source"] in CATALOGS:
        source, url = book["source"], book["source_url"].split("#")[0]
    elif book.get("asin"):
        source, url = "audible", audible.book_url(book["asin"])
    else:  # a book from a release or your own files: Open Library stands in as its catalog
        source, url = "openlibrary", "https://openlibrary.org/search?" + urlencode(
            {"title": main_title(book["title"]), "author": first_author(book["author"])})
    added = add_wanted({**book, "url": url, "length": book["length"] if kind_of(book) == kind else ""}, kind, source,
                       status)
    if added:
        return added, True
    # Already there: under this link, or as the same book in a form in_library recognises.
    return db.get_by_url(url_for(url, kind)) or in_library(book.get("asin") or "", book["title"], book["author"],
                                                           kind=kind), False


# ---- following ----------------------------------------------------------------


async def _books_for(kind: str, asin: str, name: str = "") -> tuple[str, str, list[dict]]:
    if kind == "author":
        info = await audible.author(asin)
        name = info["name"] or name
        return name, info["image"], await audible.author_books(asin, name)
    title, books = await audible.series_books(asin)
    return title or name, (books[0]["cover_url"] if books else ""), books


async def follow(kind: str, asin: str, name: str = "", add_existing: bool = False) -> tuple[dict, int]:
    """Start following. Upcoming books always go to Wanted; existing ones only if asked."""
    name, image, books = await _books_for(kind, asin, name)
    f = db.add_follow(kind, asin, name, image, [b["asin"] for b in books])
    added = sum(1 for b in books if (add_existing or not _released(b)) and add_wanted(b))
    return f, added


async def refresh(f: dict) -> int:
    """Add any books a followed author or series has gained since we last looked."""
    _, image, books = await _books_for(f["kind"], f["asin"], f["name"])
    known = set(f["known"])
    added = sum(1 for b in books if b["asin"] not in known and add_wanted(b))
    db.update_follow(f["id"], known=sorted(known | {b["asin"] for b in books}), checked_at=time.time(),
                     image=image or f["image"])
    return added


async def refresh_due() -> int:
    added = 0
    for f in db.list_follows():
        if time.time() - (f["checked_at"] or 0) >= FOLLOW_REFRESH_SECONDS:
            try:
                added += await refresh(f)
            except audible.AudibleError as e:
                log.info("Couldn't refresh %s: %s", f["name"], e)
    return added


def follow_counts(f: dict, books: list[dict]) -> dict:
    books = [b for b in books if kind_of(b) == "audiobook"]  # following is for audiobooks
    if f["kind"] == "author":
        name = normalize(f["name"])
        mine = [b for b in books if name in [normalize(a) for a in re.split(r",|&| and ", b["author"] or "")]]
    else:
        mine = [b for b in books if any(s.get("asin") == f["asin"] for s in b.get("series") or [])]
    newest = sorted(mine, key=lambda b: b.get("added_at") or 0, reverse=True)
    return {
        "in_library": sum(b["status"] == "completed" for b in mine),
        "wanted": sum(b["status"] not in ("completed",) for b in mine),
        "downloading": sum(b["status"] in db.ACTIVE for b in mine),
        "requested": sum(b["status"] == "requested" for b in mine),
        # A book of theirs that turned up in the last month, for a "New book" tag.
        "new": any(time.time() - (b.get("added_at") or 0) < 30 * 86400 and b["source"] in CATALOGS for b in mine),
        # Up to three of their covers, newest first, for the series cards.
        "covers": [f"/api/library/{b['id']}/cover" if b.get("path") else b.get("cover_url") or "" for b in newest[:3]],
    }
