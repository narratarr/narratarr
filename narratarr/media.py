"""Reading a downloaded book's audio files and chapters, for the built-in player."""

from __future__ import annotations

import re
import struct
from functools import lru_cache
from pathlib import Path

import mutagen
from mutagen.mp4 import MP4

AUDIO_TYPES = {
    ".m4b": "audio/mp4",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
}


def _natural_key(path: Path) -> list:
    """Sort 'Part 2' before 'Part 10'."""
    rel = str(path).lower()
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", rel)]


def audio_files(book_dir: Path) -> list[Path]:
    if not book_dir.is_dir():
        return []
    files = [p for p in book_dir.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_TYPES]
    return sorted(files, key=lambda p: _natural_key(p.relative_to(book_dir)))


# ---- QuickTime chapter tracks ------------------------------------------------
# mutagen reads Nero-style chapters ("chpl"), but many audiobooks (LibriVox's
# .m4b files included) store chapters as a QuickTime text track instead: a
# track whose samples are the chapter titles, referenced from the audio track
# via tref/chap. This reads just enough of the MP4 box structure to get them.

_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"tref", b"udta", b"edts"}


def _boxes(data: bytes, start: int = 0, end: int | None = None):
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", data[pos : pos + 8])
        header = 8
        if size == 1:
            size = struct.unpack(">Q", data[pos + 8 : pos + 16])[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header:
            return
        yield kind, pos + header, pos + size
        pos += size


def _read_moov(f) -> bytes | None:
    f.seek(0, 2)
    file_end = f.tell()
    pos = 0
    while pos + 8 <= file_end:
        f.seek(pos)
        size, kind = struct.unpack(">I4s", f.read(8))
        header = 8
        if size == 1:
            size = struct.unpack(">Q", f.read(8))[0]
            header = 16
        elif size == 0:
            size = file_end - pos
        if size < header:
            return None
        if kind == b"moov":
            f.seek(pos)
            return f.read(size) if size < 64 * 1024 * 1024 else None
        pos += size
    return None


def _track(data: bytes, start: int, end: int) -> dict:
    t: dict = {"chap": []}

    def walk(s: int, e: int) -> None:
        for kind, a, b in _boxes(data, s, e):
            body = data[a:b]
            if kind in _CONTAINERS:
                walk(a, b)
            elif kind == b"tkhd":
                t["id"] = struct.unpack(">I", body[12:16] if body[0] == 0 else body[20:24])[0]
            elif kind == b"chap":
                t["chap"] = list(struct.unpack(f">{len(body) // 4}I", body[: len(body) // 4 * 4]))
            elif kind == b"mdhd":
                t["timescale"] = struct.unpack(">I", body[12:16] if body[0] == 0 else body[20:24])[0]
            elif kind == b"hdlr":
                t["handler"] = body[8:12]
            elif kind == b"stts":
                n = struct.unpack(">I", body[4:8])[0]
                t["stts"] = [struct.unpack(">II", body[8 + i * 8 : 16 + i * 8]) for i in range(n)]
            elif kind == b"stsz":
                fixed, n = struct.unpack(">II", body[4:12])
                t["stsz"] = [fixed] * n if fixed else list(struct.unpack(f">{n}I", body[12 : 12 + n * 4]))
            elif kind == b"stsc":
                n = struct.unpack(">I", body[4:8])[0]
                t["stsc"] = [struct.unpack(">III", body[8 + i * 12 : 20 + i * 12]) for i in range(n)]
            elif kind == b"stco":
                n = struct.unpack(">I", body[4:8])[0]
                t["stco"] = list(struct.unpack(f">{n}I", body[8 : 8 + n * 4]))
            elif kind == b"co64":
                n = struct.unpack(">I", body[4:8])[0]
                t["stco"] = list(struct.unpack(f">{n}Q", body[8 : 8 + n * 8]))

    walk(start, end)
    return t


def qt_chapters(path: Path) -> list[tuple[float, str]]:
    """Chapters from a QuickTime chapter text track, or [] if there isn't one."""
    try:
        with open(path, "rb") as f:
            moov = _read_moov(f)
            if not moov:
                return []
            tracks = [_track(moov, a, b) for kind, a, b in _boxes(moov, 8) if kind == b"trak"]
            wanted = {i for t in tracks for i in t["chap"]}
            chap = next((t for t in tracks if t.get("id") in wanted), None) or next(
                (t for t in tracks if t.get("handler") in (b"text", b"sbtl") and t.get("stco")), None
            )
            if not chap or not all(k in chap for k in ("timescale", "stts", "stsz", "stsc", "stco")):
                return []

            # Sample start times.
            times, t = [], 0
            for count, delta in chap["stts"]:
                for _ in range(count):
                    times.append(t)
                    t += delta

            # Sample file offsets, from chunk offsets + samples-per-chunk runs.
            offsets, sizes, sample = [], chap["stsz"], 0
            runs = chap["stsc"] + [(len(chap["stco"]) + 1, 0, 0)]
            for (first, per_chunk, _), (next_first, _, _) in zip(runs, runs[1:]):
                for chunk in range(first, next_first):
                    if chunk - 1 >= len(chap["stco"]):
                        break
                    pos = chap["stco"][chunk - 1]
                    for _ in range(per_chunk):
                        if sample >= len(sizes):
                            break
                        offsets.append(pos)
                        pos += sizes[sample]
                        sample += 1

            chapters = []
            for start, offset, size in zip(times, offsets, sizes):
                f.seek(offset)
                raw = f.read(size)
                if len(raw) < 2:
                    continue
                n = struct.unpack(">H", raw[:2])[0]
                text = raw[2 : 2 + n]
                title = text.decode("utf-16") if text[:2] in (b"\xfe\xff", b"\xff\xfe") else text.decode("utf-8", "replace")
                chapters.append((start / chap["timescale"], title.strip()))
            return chapters
    except (OSError, struct.error, ValueError, IndexError, ZeroDivisionError):
        return []


def _file_info(path: Path) -> tuple[float, str, list[tuple[float, str]], int | None]:
    """(duration, tag title, embedded chapters, bitrate in bits/s) for one audio file."""
    duration, title, chapters = 0.0, "", []
    bitrate = None
    try:
        audio = mutagen.File(path)
    except Exception:
        audio = None
    if audio is not None and audio.info is not None:
        duration = float(getattr(audio.info, "length", 0) or 0)
        rate = getattr(audio.info, "bitrate", 0) or 0
        if rate > 0:
            bitrate = round(rate)
        tags = audio.tags or {}
        for key in ("\xa9nam", "TIT2", "title"):
            if key in tags:
                value = tags[key]
                title = str(value[0] if isinstance(value, list) else value)
                break
        if isinstance(audio, MP4):
            if audio.chapters:
                chapters = [(float(c.start), c.title or "") for c in audio.chapters]
            if len(chapters) < 2:
                chapters = qt_chapters(path) or chapters
    return duration, title, chapters, bitrate


@lru_cache(maxsize=64)
def _describe(book_dir: str, signature: tuple) -> dict:
    root = Path(book_dir)
    files, chapters, offset = [], [], 0.0
    paths = audio_files(root)
    for index, path in enumerate(paths):
        duration, tag_title, embedded, bitrate = _file_info(path)
        files.append(
            {
                "index": index,
                "name": str(path.relative_to(root)),
                "duration": duration,
                "offset": offset,
                "type": AUDIO_TYPES[path.suffix.lower()],
                "bitrate": bitrate,
            }
        )
        if embedded:
            for start, title in embedded:
                chapters.append({
                    "title": title or f"Chapter {len(chapters) + 1}",
                    "start": offset + start,
                    "source": "embedded",
                })
        elif len(paths) > 1:
            chapters.append({"title": tag_title or path.stem, "start": offset, "source": "file"})
        offset += duration
    # Weight each file by its duration. A missing rate or duration means we
    # cannot report an average for the whole book accurately.
    bitrate = None
    if files and all(f["bitrate"] is not None and f["duration"] > 0 for f in files):
        bitrate = round(sum(f["bitrate"] * f["duration"] for f in files) / offset)
    return {"files": files, "chapters": chapters, "duration": offset, "bitrate": bitrate}


def describe(book_dir: Path) -> dict:
    """Files, chapters and duration on a book-wide timeline; bitrates in bits/s.

    The book's bitrate is a duration-weighted average, or None when incomplete.
    Chapter sources distinguish embedded markers from fallback file boundaries.
    """
    paths = audio_files(book_dir)
    # Re-read when files change, otherwise serve from cache.
    signature = tuple((str(p), p.stat().st_size, p.stat().st_mtime) for p in paths)
    return _describe(str(book_dir), signature)
