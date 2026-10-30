"""Bringing in books the user already has: a folder of books, an existing library, or a watched inbox.

An audiobook is a folder with audio files in it (multi-disc "CD1"/"Disc 2" folders
count as one book), or a single audio file on its own. Title, author and
narrator come from the files' tags when they have them, otherwise from the
folder names ("Author/Title" or "Author - Title").

An ebook is an ebook file that isn't inside an audiobook's folder (one that is
comes along with its audiobook). Several formats of one book side by side
("Dune.epub", "Dune.mobi") count once, as the best of them. Its details come from
the EPUB itself when it has them, otherwise from the names.
"""

from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import mutagen

from . import ebooks
from .media import AUDIO_TYPES, audio_files

_DISC_RE = re.compile(r"^(cd|disc|disk|part|vol(ume)?)[\s._-]*\d+$", re.I)
_SKIP_DIRS = {".narratarr-tmp", "@eaDir", ".Trash", "#recycle"}


def url_for(path: Path) -> str:
    return path.resolve().as_uri()


def path_of(url: str) -> Path:
    return Path(unquote(urlparse(url).path))


def _is_audio(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in AUDIO_TYPES and not p.name.startswith("._")


def _hidden(f: Path, root: Path) -> bool:
    return any(part in _SKIP_DIRS or part.startswith(".") for part in f.relative_to(root).parts[:-1])


def find_books(root: Path) -> list[Path]:
    """Every audiobook under root: folders holding audio, plus loose audio files sitting directly in a folder of books."""
    root = root.resolve()
    books: set[Path] = set()
    for f in root.rglob("*"):
        if _hidden(f, root):
            continue
        if not _is_audio(f):
            continue
        folder = f.parent
        if _DISC_RE.match(folder.name) and folder != root:
            folder = folder.parent
        books.add(f if folder == root else folder)
    # A folder of books inside another book's folder (e.g. extras) belongs to the outer one.
    ordered = sorted(books, key=lambda p: len(p.parts))
    kept: list[Path] = []
    for b in ordered:
        if not any(k.is_dir() and k in b.parents for k in kept):
            kept.append(b)
    return sorted(kept, key=lambda p: str(p).lower())


def _stem(p: Path) -> str:
    return " ".join(re.sub(r"[\W_]+", " ", p.stem.lower()).split())


def find_ebooks(root: Path, audiobooks: list[Path]) -> list[Path]:
    """Every ebook under root that isn't part of an audiobook: one file per book, the best format of it."""
    root = root.resolve()
    folders = [b for b in audiobooks if b.is_dir()]
    groups: dict[tuple[Path, str], list[Path]] = {}
    for f in root.rglob("*"):
        if _hidden(f, root) or not ebooks.is_ebook(f) or any(b in f.parents for b in folders):
            continue
        groups.setdefault((f.parent, _stem(f)), []).append(f)
    return sorted((ebooks.best(g) for g in groups.values()), key=lambda p: str(p).lower())


def read_ebook_meta(book: Path, root: Path) -> dict:
    meta = ebooks.epub_meta(book) if book.suffix.lower() == ".epub" else {}
    title, author = meta.get("title", ""), meta.get("author", "")
    folder = book.parent
    guess_author, guess_title = "", book.stem
    if " - " in book.stem:
        guess_author, guess_title = (x.strip() for x in book.stem.split(" - ", 1))
    elif folder != root and _stem(folder) == _stem(book):  # Author/Title/Title.epub
        guess_author = folder.parent.name if folder.parent != root and root in folder.parents else ""
    elif folder != root:  # Author/Title.epub
        guess_author = folder.name
    description = meta.get("description", "")
    if not description and folder != root and (folder / "desc.txt").is_file() and _stem(folder) == _stem(book):
        description = (folder / "desc.txt").read_text(errors="ignore").strip()
    return {
        "path": str(book),
        "title": title or guess_title,
        "author": author or guess_author,
        "narrators": [],
        "description": description,
        "files": 1,
        "bytes": book.stat().st_size,
        "format": book.suffix.lstrip(".").upper(),
        "single_file": True,
        "kind": "ebook",
    }


def _tags(f: Path) -> dict:
    try:
        audio = mutagen.File(f, easy=True)
    except Exception:
        return {}
    if not audio or not audio.tags:
        return {}
    first = lambda k: (audio.tags.get(k) or [""])[0].strip()  # noqa: E731
    return {
        "album": first("album"),
        "title": first("title"),
        "albumartist": first("albumartist"),
        "artist": first("artist"),
        "composer": first("composer"),  # Audible exports and many taggers put the narrator here
    }


def _readable(tag: str) -> str:
    """Tags like 'the_raven' or 'poe_ea' are IDs, not names; the folder name is better then."""
    return tag if tag and "_" not in tag and (" " in tag or tag[:1].isupper()) else ""


def read_meta(book: Path, root: Path) -> dict:
    files = [book] if book.is_file() else audio_files(book)
    tags = {k: _readable(v) for k, v in (_tags(files[0]) if files else {}).items()}
    name = book.stem if book.is_file() else book.name
    title = tags.get("album") or (tags.get("title") if len(files) == 1 else "") or ""
    author = tags.get("albumartist") or tags.get("artist") or ""
    narrator = tags.get("composer") or ""
    if not title or not author:
        guess_author, guess_title = "", name
        if " - " in name:
            guess_author, guess_title = (x.strip() for x in name.split(" - ", 1))
        elif book.parent != root and book.is_dir():
            guess_author = book.parent.name  # Author/Title layout
        title = title or guess_title
        author = author or guess_author
    folder = book.parent if book.is_file() else book
    if not narrator and book.is_dir() and (folder / "reader.txt").is_file():
        narrator = (folder / "reader.txt").read_text(errors="ignore").strip()[:200]
    description = ""
    if book.is_dir() and (folder / "desc.txt").is_file():
        description = (folder / "desc.txt").read_text(errors="ignore").strip()
    size = sum(f.stat().st_size for f in files)
    return {
        "path": str(book),
        "title": title,
        "author": author,
        "narrators": [n.strip() for n in re.split(r"[,;&]", narrator) if n.strip()][:4],
        "description": description,
        "files": len(files),
        "bytes": size,
        "format": (files[0].suffix.lstrip(".").upper() if files else ""),
        "single_file": book.is_file(),
        "kind": "audiobook",
    }


def scan(root: Path) -> list[dict]:
    root = root.resolve()
    audiobooks = find_books(root)
    found = [read_meta(b, root) for b in audiobooks] + [read_ebook_meta(e, root) for e in find_ebooks(root, audiobooks)]
    return sorted(found, key=lambda f: f["path"].lower())


def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def as_book(found: dict, mode: str, status: str) -> dict:
    """A library row for a book found on disk. mode: "copy", "move" or "inplace"."""
    path = Path(found["path"])
    book = {
        "source": "local",
        "source_url": url_for(path),
        "title": found["title"] or path.stem,
        "author": found["author"],
        "narrators": found["narrators"],
        "description": found["description"],
        "format": found["format"],
        "size": fmt_size(found["bytes"]),
        "import_mode": mode,
        "status": status,
        "kind": found.get("kind", "audiobook"),
    }
    if mode == "inplace":
        book.update(path=str(path), progress=100, completed_at=time.time())
    return book


def quiet_for(path: Path, seconds: float) -> bool:
    """True when nothing in path changed for a while (so a copy into the watch folder has finished)."""
    paths = [path] if path.is_file() else [path, *path.rglob("*")]
    newest = max((p.stat().st_mtime for p in paths if p.exists()), default=0)
    return time.time() - newest >= seconds


def bring_in(src: Path, into: Path, move: bool, report=None, stop=None) -> None:
    """Copy or move a book's files into our temp folder, keeping sub-folders (discs) intact.

    Safe to run again after an interruption: files already brought in are skipped.
    `stop` is a threading.Event; set it to stop between files.
    """
    files = [src] if src.is_file() else [p for p in src.rglob("*") if p.is_file() and not p.name.startswith(".")]
    total = sum(f.stat().st_size for f in files) or 1
    done = 0
    for f in files:
        if stop is not None and stop.is_set():
            return
        target = into / (f.name if src.is_file() else f.relative_to(src))
        target.parent.mkdir(parents=True, exist_ok=True)
        if report:
            report(done, total, f.name)
        if target.exists() and target.stat().st_size == f.stat().st_size:
            if move:
                f.unlink()  # already brought in by an earlier, interrupted attempt
        elif move:
            shutil.move(str(f), str(target))
        else:
            shutil.copy2(f, target)
        done += target.stat().st_size
    if move and src.is_dir():
        leftovers = [p for p in src.rglob("*") if p.is_file() and not p.name.startswith(".")]
        if not leftovers:  # only the empty shell (and hidden junk) is left
            shutil.rmtree(src, ignore_errors=True)


def embedded_cover(book_dir: Path) -> tuple[bytes, str] | None:
    """The cover art inside the first audio files, as (bytes, mime type)."""
    for f in audio_files(book_dir)[:3]:
        try:
            audio = mutagen.File(f)
        except Exception:
            continue
        if audio is None or audio.tags is None:
            continue
        if "covr" in audio.tags:  # MP4 / M4B
            art = audio.tags["covr"][0]
            return bytes(art), "image/png" if getattr(art, "imageformat", 0) == 14 else "image/jpeg"
        pics = getattr(audio, "pictures", None) or [v for k, v in audio.tags.items() if k.startswith("APIC")]
        if pics:
            return pics[0].data, getattr(pics[0], "mime", "") or "image/jpeg"
    return None


def extract_cover(book_dir: Path) -> bool:
    """Save the cover art embedded in the audio as cover.jpg, if the folder has none."""
    if any(book_dir.glob("cover.*")):
        return True
    art = embedded_cover(book_dir)
    if not art:
        return False
    (book_dir / ("cover.png" if "png" in art[1] else "cover.jpg")).write_bytes(art[0])
    return True
