"""Tidy library: move the books already in the library into the folder layout chosen in Settings.

Nothing moves until it has been previewed. plan() works out where every finished book would go,
and why some can't go yet. apply() moves the ones picked, and undo() puts the last batch back.

A move is always a rename on the same disk, never a copy. That keeps it instant, and it keeps
each folder's identity on disk (its inode), which is how Audiobookshelf recognises a moved book
and keeps everyone's listening progress. A book that could only get there by copying to another
disk is held back instead. So are books whose new folder is already taken, books that hold another
book inside their folder, and books Narratarr is working on. Books with no author or title are
marked "needs a look", since tidying them would only file them under "Unknown Author".
"""

from __future__ import annotations

import errno
import logging
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import mutagen
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TALB, TCOM, TDRC, TPE1, TPE2, TXXX
from mutagen.mp4 import MP4, MP4FreeForm
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from . import config, db, ebooks, importer, layout, media, wanted

log = logging.getLogger("narratarr.tidy")

PREFER = ("library", "tags")  # what a folder is named from: Narratarr's details, or the files' own tags
LONGEST_PATH = 250  # Windows can't open longer paths without special settings
_LEFTOVERS = {".ds_store", "thumbs.db", "desktop.ini", "@eadir"}  # safe to remove from an emptied folder
MAX_COVER_BYTES = 20 << 20


class TidyError(Exception):
    pass


@dataclass
class Move:
    book: dict
    src: Path  # what is renamed: the book's folder, or (for some ebooks) just its file
    dst: Path
    new_path: Path  # the book's path once it has moved
    details: dict
    origin: str
    state: str = "move"  # move · tidy (already in place) · look (details missing) · held (can't move yet)
    reason: str = ""
    carries: list[dict] = field(default_factory=list)  # books inside src that move with it (its ebook)
    joins: int | None = None  # an ebook going into this audiobook's folder

    def hold(self, reason: str, state: str = "held") -> None:
        if self.state == "move":
            self.state, self.reason = state, reason


# ---- what a book's folder is named from ---------------------------------------------------------


def file_tags(folder: Path) -> dict:
    """A book's details as its audio files' tags have them. Only what the tags actually say."""
    files = media.audio_files(folder) if folder.is_dir() else []
    if not files:
        return {}
    raw = importer._tags(files[0])
    tags = {k: importer._readable(v) for k, v in raw.items()}
    author, narrator = importer.pick_people(tags.get("artist", ""), tags.get("albumartist", ""),
                                            tags.get("narrator") or tags.get("composer") or "")
    out = {
        "title": tags.get("album") or (tags.get("title") if len(files) == 1 else ""),
        "author": author,
        "narrators": [n.strip() for n in re.split(r"[,;&]", narrator) if n.strip()][:4],
        "series": [{"asin": "", "title": raw["series"], "sequence": raw.get("series_part", "")}] if raw.get("series") else [],
        "released": (raw.get("date") or "")[:10],
    }
    return {k: v for k, v in out.items() if v}


def details(book: dict, prefer: str = "library") -> tuple[dict, str]:
    """(the details a book's folder is named from, where they came from)."""
    d = {k: book.get(k) for k in ("title", "author", "narrators", "series", "released")}
    origin = "audible" if book.get("asin") else ("import" if book.get("source") == "local" else "download")
    if prefer == "tags" and wanted.kind_of(book) == "audiobook" and book.get("path"):
        found = file_tags(Path(book["path"]))
        if found:
            d.update(found)
            origin = "tags"
    return d, origin


def folder_for(d: dict, settings: config.Settings, root: Path) -> Path:
    parts = layout.folders(settings.folder_template, d) or ["Unknown Author", "Untitled"]
    return root.joinpath(*parts)


# ---- the plan -----------------------------------------------------------------------------------


def _inside(path: Path, folder: Path) -> bool:
    return folder in path.parents


def _same(a: Path, b: Path) -> bool:
    try:
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError:
        return False


def _empty_dir(p: Path) -> bool:
    return p.is_dir() and not any(p.iterdir())


def _device(p: Path) -> int | None:
    while not p.exists():
        if p.parent == p:
            return None
        p = p.parent
    return p.stat().st_dev


def _longest_inside(src: Path) -> int:
    """The longest path of a file below src, counted from src."""
    if src.is_file():
        return 0
    return max((len(str(f.relative_to(src))) + 1 for f in src.rglob("*")), default=0)


def _rel(p: Path, root: Path) -> str:
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return str(p)


def _key(p: Path) -> str:
    return str(p).lower()  # compared without case, so "dune" and "Dune" count as one folder everywhere


class _Places:
    """Which books live at a path, and which live somewhere below a folder."""

    def __init__(self, where: dict[int, Path], root: Path):
        self.at: dict[str, set[int]] = {}
        self.below: dict[str, set[int]] = {}
        for book_id, p in where.items():
            self.at.setdefault(_key(p), set()).add(book_id)
            for a in p.parents:
                self.below.setdefault(_key(a), set()).add(book_id)
                if a == root:
                    break

    def under(self, folder: Path, exempt: set[int]) -> bool:
        return bool(self.below.get(_key(folder), set()) - exempt)

    def taken(self, path: Path, exempt: set[int]) -> bool:
        """Some other book lives at path, or in a folder that path is inside."""
        return any(self.at.get(_key(a), set()) - exempt for a in (path, *path.parents))


def _own_folder(path: Path, root: Path, places: _Places, me: int) -> bool:
    """An ebook's folder holds just it, plus the cover and description Narratarr put beside it."""
    folder = path.parent
    if folder == root or not _inside(folder, root):
        return False
    if places.taken(folder, {me}) or places.under(folder, {me}):
        return False
    return all(x == path or ebooks.sidecar(x) or ebooks.clutter(x) for x in folder.iterdir())


def plan(settings: config.Settings, prefer: str = "library", books: list[dict] | None = None,
         busy: set[int] | frozenset = frozenset()) -> list[Move]:
    """Where every finished book would go under the current folder layout, and which can't move yet."""
    root = Path(settings.library_dir).expanduser().resolve()
    books = [b for b in (db.list_books() if books is None else books) if b.get("path")]
    where = {b["id"]: Path(b["path"]).resolve() for b in books}
    done = [b for b in books if b["status"] == "completed"]
    companions = wanted.companions(books)
    places = _Places(where, root)
    moves: dict[int, Move] = {}

    # Audiobooks first: an ebook may be carried inside one, or go to join it.
    audiobooks = [b for b in done if wanted.kind_of(b) == "audiobook"]
    carried: set[int] = set()
    for b in audiobooks:
        src = where[b["id"]]
        d, origin = details(b, prefer)
        dst = folder_for(d, settings, root)
        m = moves[b["id"]] = Move(b, src, dst, dst, d, origin)
        if src.is_dir() and _inside(src, root):
            for e in books:
                if e["id"] != b["id"] and wanted.kind_of(e) == "ebook" and _inside(where[e["id"]], src):
                    m.carries.append(e)
                    carried.add(e["id"])
        if src == dst:
            m.state = "tidy"

    for b in done:
        if wanted.kind_of(b) != "ebook" or b["id"] in carried:
            continue
        path = where[b["id"]]
        d, origin = details(b, prefer)
        partner = companions.get(b["id"])
        pm = moves.get(partner["id"]) if partner else None
        if pm is not None:
            # The same book in both formats shares one folder, the way Audiobookshelf expects.
            folder = pm.dst if pm.state in ("move", "tidy") else pm.src
            m = moves[b["id"]] = Move(b, path, folder / path.name, folder / path.name, d, origin, joins=partner["id"])
            if pm.state not in ("move", "tidy"):
                m.hold("Waiting on its audiobook: the two belong in one folder")
        else:
            folder = folder_for(d, settings, root)
            if path.parent.is_dir() and _own_folder(path, root, places, b["id"]):
                m = moves[b["id"]] = Move(b, path.parent, folder, folder / path.name, d, origin)
            else:
                m = moves[b["id"]] = Move(b, path, folder / path.name, folder / path.name, d, origin)
        if m.new_path == path:
            m.state = "tidy"

    # Who claims which folder, so no two books end up sharing one.
    claims: dict[str, list[Move]] = {}
    for m in moves.values():
        if m.state == "move" and m.joins is None:
            claims.setdefault(str(m.dst).lower(), []).append(m)

    for m in moves.values():
        b = m.book
        cur = where[b["id"]]
        if m.state != "move":
            pass
        elif not _inside(cur, root):
            m.hold("It isn't inside your library folder")
        elif not cur.exists():
            m.hold("Its files aren't there any more")
        elif b["id"] in busy or any(c["id"] in busy for c in m.carries):
            m.hold("Narratarr is working on it right now")
        elif wanted.kind_of(b) == "audiobook" and not cur.is_dir():
            m.hold("It's a single audio file rather than a folder")
        elif not (m.details.get("author") or "").strip() or not (m.details.get("title") or "").strip():
            m.hold("It has no author" if (m.details.get("title") or "").strip() else "It has no title", "look")
        if m.state != "move":
            continue

        exempt = {b["id"], *(c["id"] for c in m.carries)}
        if m.joins is not None:
            exempt.add(m.joins)
        if m.src.is_dir() and places.under(m.src, exempt):
            m.hold("Another book is inside its folder")
        elif places.taken(m.dst, exempt):
            m.hold("That folder belongs to another book")
        elif places.under(m.dst, exempt):
            m.hold("Another book is already in that folder")
        elif m.joins is None and len(claims.get(str(m.dst).lower(), [])) > 1:
            m.hold("Another book would go to the same folder")
        elif m.dst.exists() and not _same(m.src, m.dst) and not _empty_dir(m.dst):
            m.hold("Something is already there with that name")
        elif _device(m.src) != _device(m.dst.parent):
            m.hold("That's on another disk: it would have to be copied, and Audiobookshelf would lose its place")
        elif len(str(m.dst)) + _longest_inside(m.src) > LONGEST_PATH:
            m.hold("The new path would be too long for Windows")

    order = {"move": 0, "look": 1, "held": 2, "tidy": 3}
    return sorted(moves.values(), key=lambda m: (order[m.state], (m.book["author"] or "").lower(), (m.book["title"] or "").lower()))


def preview(settings: config.Settings, prefer: str = "library", busy: set[int] | frozenset = frozenset()) -> dict:
    root = Path(settings.library_dir).expanduser().resolve()
    moves = plan(settings, prefer, busy=busy)
    counts = {s: 0 for s in ("move", "tidy", "look", "held")}
    rows = []
    for m in moves:
        counts[m.state] += 1
        rows.append({
            "id": m.book["id"],
            "kind": wanted.kind_of(m.book),
            "title": m.book["title"],
            "author": m.book["author"],
            "state": m.state,
            "reason": m.reason,
            "origin": m.origin,
            "from": _rel(Path(m.book["path"]).resolve(), root),
            "to": _rel(m.new_path, root),
            "carries": [c["title"] for c in m.carries],
            "joins": m.joins,
        })
    batch, done = db.last_tidy_batch()
    return {"root": str(root), "template": settings.folder_template, "prefer": prefer, "counts": counts,
            "books": rows, "undo": len(done)}


# ---- moving ---------------------------------------------------------------------------------------


def _leftover(p: Path) -> bool:
    return p.name.lower() in _LEFTOVERS or p.name.startswith("._")


def prune(folder: Path, root: Path) -> None:
    """Remove folders a move left empty (apart from .DS_Store and friends), up to the library folder."""
    while folder != root and _inside(folder, root) and folder.is_dir():
        entries = list(folder.iterdir())
        if not all(_leftover(e) for e in entries):
            return
        for e in entries:
            shutil.rmtree(e, ignore_errors=True) if e.is_dir() else e.unlink(missing_ok=True)
        try:
            folder.rmdir()
        except OSError:
            return
        folder = folder.parent


def rename(src: Path, dst: Path, root: Path) -> None:
    """Move src to dst with renames only. Across disks this fails rather than copying."""
    case_only = dst.exists() and _same(src, dst)
    nested = _inside(dst, src) or _inside(src, dst)
    if dst.exists() and not case_only and not nested:
        if not _empty_dir(dst):
            raise TidyError(f"Something is already at {_rel(dst, root)}")
        dst.rmdir()
    if case_only or nested:
        # "dune" → "Dune" on a disk that ignores case, or a folder moving into (or out of) itself:
        # step aside first.
        tmp = root / f".narratarr-tidy-{secrets.token_hex(4)}"
        os.rename(src, tmp)
        try:
            prune(src.parent, root)
            if dst.exists():
                if not _empty_dir(dst):
                    raise TidyError(f"Something is already at {_rel(dst, root)}")
                dst.rmdir()
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.rename(tmp, dst)
        except BaseException:
            src.parent.mkdir(parents=True, exist_ok=True)
            os.rename(tmp, src)
            raise
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(src, dst)
        except OSError as e:
            if e.errno == errno.EXDEV:
                raise TidyError("That's on another disk, so it would have to be copied") from e
            raise
    prune(src.parent, root)


@dataclass
class Job:
    running: bool = False
    total: int = 0
    done: int = 0
    current: str = ""
    moved: int = 0
    failed: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    finished_at: float | None = None
    undone: int | None = None

    def as_dict(self) -> dict:
        return {**self.__dict__, "undo": len(db.last_tidy_batch()[1])}


JOB = Job()


def apply(settings: config.Settings, ids: list[int], prefer: str = "library", busy=frozenset(),
          tags: bool = False, sidecars: bool = False, job: Job | None = None) -> Job:
    """Move the picked books. The plan is worked out again here: what the browser sent is only which books."""
    job = job or Job()
    root = Path(settings.library_dir).expanduser().resolve()
    wanted_ids = set(ids)
    everything = [m for m in plan(settings, prefer, busy=busy) if m.book["id"] in wanted_ids]
    moves = [m for m in everything if m.state == "move"]
    for m in everything:  # changed since the preview: say why it stayed
        if m.state in ("held", "look"):
            job.failed.append({"id": m.book["id"], "title": m.book["title"], "error": m.reason})
    moves.sort(key=lambda m: m.joins is not None)  # audiobooks before the ebooks joining them
    planned = {m.book["id"] for m in moves}
    moved_ok: set[int] = set()
    batch = secrets.token_hex(8)
    job.total, job.done, job.moved = len(moves), 0, 0

    for m in moves:
        b = m.book
        job.current = b["title"]
        try:
            if m.joins is not None and m.joins not in moved_ok and not (m.dst.parent.is_dir()):
                raise TidyError("Its audiobook didn't move, so it stayed where it was"
                                if m.joins in planned else "Pick its audiobook too: the two move together")
            fresh = db.get(b["id"])
            if not fresh or fresh["status"] != "completed" or fresh["path"] != b["path"]:
                raise TidyError("It changed while tidying, so it was left alone")
            paths = [[b["id"], b["path"], str(m.new_path)]]
            for c in m.carries:
                paths.append([c["id"], c["path"], str(m.dst / Path(c["path"]).resolve().relative_to(m.src))])
            move_id = db.add_tidy_move(batch, str(m.src), str(m.dst), paths)
            try:
                rename(m.src, m.dst, root)
            except BaseException:
                db.mark_tidy_undone(move_id)
                raise
            for book_id, _, after in paths:
                db.update(book_id, path=after)
        except (OSError, TidyError) as e:
            job.failed.append({"id": b["id"], "title": b["title"], "error": str(e)})
            log.warning("Tidy: couldn't move %s: %s", b["title"], e)
        else:
            job.moved += 1
            moved_ok.add(b["id"])
            log.info("Tidy: moved %s to %s", b["title"], _rel(m.dst, root))
            if wanted.kind_of(b) == "audiobook":
                if sidecars:
                    job.notes += side_files(m.dst, b)
                if tags:
                    try:
                        write_tags(m.dst, m.details)
                    except Exception as e:  # a file mutagen can't save: the move itself still stands
                        job.notes.append(f"{b['title']}: couldn't update the tags ({e})")
        job.done += 1
    job.current = ""
    return job


def undo(settings: config.Settings, busy=frozenset()) -> dict:
    """Put the last batch of moves back where they came from, newest first."""
    root = Path(settings.library_dir).expanduser().resolve()
    _, moves = db.last_tidy_batch()
    back, failed = 0, []
    for r in reversed(moves):
        src, dst = Path(r["src"]), Path(r["dst"])
        ids = [p[0] for p in r["paths"]]
        try:
            if any(i in busy for i in ids):
                raise TidyError("Narratarr is working on it right now")
            if not dst.exists():
                raise TidyError("It isn't where Tidy put it any more")
            rename(dst, src, root)
        except (OSError, TidyError) as e:
            failed.append({"path": _rel(dst, root), "error": str(e)})
            continue
        for book_id, before, after in r["paths"]:
            b = db.get(book_id)
            if b and b["path"] == after:
                db.update(book_id, path=before)
        db.mark_tidy_undone(r["id"])
        back += 1
    return {"back": back, "failed": failed}


# ---- the extras: side files and tags --------------------------------------------------------------


def side_files(folder: Path, book: dict) -> list[str]:
    """The cover, description and narrator files Audiobookshelf reads, where the folder lacks them."""
    notes = []
    try:
        if book.get("description") and not (folder / "desc.txt").exists():
            (folder / "desc.txt").write_text(book["description"])
        if book.get("narrators") and not (folder / "reader.txt").exists():
            (folder / "reader.txt").write_text(", ".join(book["narrators"]))
        if not importer.extract_cover(folder) and str(book.get("cover_url") or "").startswith(("http://", "https://")):
            _download_cover(book["cover_url"], folder)
    except (OSError, httpx.HTTPError) as e:
        notes.append(f"{book['title']}: couldn't add its cover or description ({e})")
    return notes


def _download_cover(url: str, folder: Path) -> None:
    with httpx.Client(follow_redirects=True, timeout=20) as client, client.stream("GET", url) as resp:
        kind = resp.headers.get("content-type", "").split(";")[0]
        if not resp.is_success or not kind.startswith("image/"):
            return
        data, size = [], 0
        for chunk in resp.iter_bytes(1 << 16):
            size += len(chunk)
            if size > MAX_COVER_BYTES:
                return
            data.append(chunk)
    ext = {"image/png": "png", "image/webp": "webp"}.get(kind, "jpg")
    (folder / f"cover.{ext}").write_bytes(b"".join(data))


def tag_values(d: dict) -> dict[str, str]:
    series = d.get("series") or []
    first = series[0] if series and isinstance(series[0], dict) else {}
    released = str(d.get("released") or "")
    return {
        "album": str(d.get("title") or "").strip(),
        "author": str(d.get("author") or "").strip(),
        "narrator": ", ".join((d.get("narrators") or [])[:4]).strip(),
        "series": str(first.get("title") or "").strip(),
        "part": layout._number(first.get("sequence")),
        "year": released[:4] if released[:4].isdigit() else "",
    }


def write_tags(folder: Path, d: dict) -> int:
    """Put the book's details into its audio files, where a tag is missing or different.

    Only ever fills in or corrects: a detail the book doesn't have leaves the tag alone. The year is
    only added where there is none, since files often carry a fuller date. Returns the files changed."""
    want = {k: v for k, v in tag_values(d).items() if v}
    changed = 0
    for f in media.audio_files(folder):
        audio = mutagen.File(f)
        if audio is None:
            continue
        if audio.tags is None:
            audio.add_tags()
        if _set_tags(audio, want):
            audio.save()
            changed += 1
    return changed


def _set_tags(audio, want: dict[str, str]) -> bool:
    if isinstance(audio, MP4):
        return _set_mp4(audio.tags, want)
    if isinstance(audio.tags, ID3):
        return _set_id3(audio.tags, want)
    if isinstance(audio, (FLAC, OggVorbis, OggOpus)):
        return _set_vorbis(audio.tags, want)
    return False


_MP4 = {"album": ["©alb"], "author": ["©ART", "aART"], "narrator": ["©wrt", "----:com.apple.iTunes:NARRATOR"],
        "series": ["----:com.apple.iTunes:SERIES"], "part": ["----:com.apple.iTunes:SERIES-PART"],
        "year": ["©day"]}


def _set_mp4(tags, want: dict[str, str]) -> bool:
    changed = False
    for name, value in want.items():
        for key in _MP4[name]:
            old = tags.get(key)
            text = (bytes(old[0]).decode("utf-8", "ignore") if key.startswith("----") else str(old[0])) if old else ""
            if (name == "year" and text) or text == value:
                continue
            tags[key] = [MP4FreeForm(value.encode())] if key.startswith("----") else [value]
            changed = True
    return changed


_ID3 = {"album": [("TALB", TALB)], "author": [("TPE1", TPE1), ("TPE2", TPE2)], "narrator": [("TCOM", TCOM), ("TXXX:NARRATOR", "NARRATOR")],
        "series": [("TXXX:SERIES", "SERIES")], "part": [("TXXX:SERIES-PART", "SERIES-PART")], "year": [("TDRC", TDRC)]}


def _set_id3(tags, want: dict[str, str]) -> bool:
    changed = False
    for name, value in want.items():
        for key, frame in _ID3[name]:
            old = tags.get(key)
            text = str(old.text[0]) if old is not None and old.text else ""
            if (name == "year" and text) or text == value:
                continue
            tags.setall(key, [TXXX(encoding=3, desc=frame, text=[value]) if isinstance(frame, str) else frame(encoding=3, text=[value])])
            changed = True
    return changed


_VORBIS = {"album": ["ALBUM"], "author": ["ARTIST", "ALBUMARTIST"], "narrator": ["COMPOSER", "NARRATOR"],
           "series": ["SERIES"], "part": ["SERIES-PART"], "year": ["DATE"]}


def _set_vorbis(tags, want: dict[str, str]) -> bool:
    changed = False
    for name, value in want.items():
        for key in _VORBIS[name]:
            old = tags.get(key) or []
            text = str(old[0]) if old else ""
            if (name == "year" and text) or text == value:
                continue
            tags[key] = [value]
            changed = True
    return changed


def finished(job: Job) -> None:
    job.running = False
    job.finished_at = time.time()
