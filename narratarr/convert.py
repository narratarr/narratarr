"""Turn a download's MP3 tracks into one M4B before it is filed.

Runs between "fill" and "move/rename": `Worker._finish()` (`worker.py`) calls `to_m4b()` while the
download is still in the temp folder, so the library receives a single tagged M4B instead of a
folder of MP3s. The MP3s are never modified — for a torrent they are hard links to the files the
client is seeding, and writing through them would rewrite what is being seeded.

Nothing here fails a download. Any problem returns None and the caller files the MP3s as it always
has.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from pathlib import Path

from . import config, media, tidy
from .importer import _tags

log = logging.getLogger("narratarr.convert")

CONVERT_TIMEOUT = 30 * 60  # a long audiobook is minutes of CPU, not hours
BITRATE_RANGE = (48, 320)
CHAPTER_TIMEBASE = "1/1000"


def available() -> bool:
    """Is ffmpeg usable here? Docker images built before this feature won't have it."""
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def needs_conversion(folder: Path) -> bool:
    """True only for a folder of MP3s and nothing else playable.

    A payload that already has an M4B, or holds FLAC/AAC/OGG, is left alone: converting those is a
    different decision nobody asked for, and a folder with both formats already has what this
    feature exists to produce.
    """
    files = media.audio_files(folder)
    if not files:
        return False
    return all(f.suffix.lower() == ".mp3" for f in files)


def _concat_escape(path: Path) -> str:
    """One line for ffmpeg's concat demuxer."""
    return str(path.resolve()).replace("'", "'\\''")


def _meta_escape(value: str) -> str:
    """ffmetadata is line-oriented; a newline in a tag would end the field."""
    return " ".join(str(value).split())


def _chapter_titles(files: list[Path]) -> list[str]:
    titles: list[str] = []
    for index, f in enumerate(files, start=1):
        try:
            title = (_tags(f) or {}).get("title") or ""
        except Exception:  # noqa: BLE001 - an unreadable tag is not a reason to skip the file
            title = ""
        titles.append(_meta_escape(title) or f"Chapter {index}")
    return titles


def build_inputs(files: list[Path], durations: list[float], book: dict) -> tuple[str, str]:
    """The concat list and the ffmetadata document for one ffmpeg pass.

    Chapters come from the track boundaries: each MP3 becomes one chapter, starting where the
    previous one ended. Cumulative milliseconds, so the timeline is exact even when ffmpeg's
    own rounding of a single track is not.
    """
    if len(files) != len(durations):
        raise ValueError("a duration per track is needed to build chapters")
    listing = "\n".join(f"file '{_concat_escape(f)}'" for f in files) + "\n"
    titles = _chapter_titles(files)
    lines = [
        ";FFMETADATA1",
        f"title={_meta_escape(book.get('title') or '')}",
        f"artist={_meta_escape(book.get('author') or '')}",
        f"album={_meta_escape(book.get('title') or '')}",
    ]
    album_artist = _meta_escape(book.get("author") or "")
    if album_artist:
        lines.append(f"album_artist={album_artist}")
    start = 0
    for seconds, title in zip(durations, titles):
        end = start + max(round(seconds * 1000), 1)
        lines += [
            "[CHAPTER]",
            f"TIMEBASE={CHAPTER_TIMEBASE}",
            f"START={start}",
            f"END={end}",
            f"title={title}",
        ]
        start = end
    return listing, "\n".join(lines) + "\n"


def _duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=120, check=True,
    )
    return float(out.stdout.strip())


def _cover(folder: Path) -> Path | None:
    for name in ("cover.jpg", "cover.jpeg", "cover.png"):
        candidate = folder / name
        if candidate.is_file():
            return candidate
    return None


def _cmd(list_file: Path, meta_file: Path, cover: Path | None, out: Path, settings) -> list[str]:
    # -map_metadata 1 / -map_chapters 1 take tags and chapters from the ffmetadata input, so the
    # chapter list survives the concat. Re-encoding to AAC is required: MP3 and MP4 do not share a
    # codec, whatever the container.
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-f", "ffmetadata", "-i", str(meta_file),
    ]
    if cover is not None:
        cmd += ["-i", str(cover), "-map", "0:a", "-map", "2:v", "-c:v", "mjpeg",
                "-disposition:v", "attached_pic"]
    else:
        cmd += ["-map", "0:a"]
    cmd += [
        "-map_metadata", "1", "-map_chapters", "1",
        "-c:a", "aac",
        "-b:a", f"{int(settings.convert_bitrate_kbps)}k",
        "-ac", str(int(settings.convert_channels)),
        "-movflags", "+faststart",
        str(out),
    ]
    return cmd


def _encode(folder: Path, listing: str, meta: str, out: Path, book: dict, settings) -> None:
    work = folder / ".narratarr-convert"
    work.mkdir(exist_ok=True)
    list_file, meta_file = work / "list.txt", work / "meta.txt"
    list_file.write_text(listing)
    meta_file.write_text(meta)
    cover = _cover(folder)
    try:
        subprocess.run(_cmd(list_file, meta_file, cover, out, settings),
                       capture_output=True, text=True, timeout=CONVERT_TIMEOUT, check=True)
        return
    except subprocess.CalledProcessError:
        if cover is None:
            raise
        # A download's cover.jpg is whatever the uploader put there, and ffmpeg refuses a file it
        # cannot decode. Losing the art is a shrug; losing the whole conversion is not.
        log.info("Book %s: cover art in the download is unusable, converting without it", book.get("id"))
        out.unlink(missing_ok=True)
        subprocess.run(_cmd(list_file, meta_file, None, out, settings),
                       capture_output=True, text=True, timeout=CONVERT_TIMEOUT, check=True)


async def to_m4b(book: dict, folder: Path, settings: config.Settings, report=None) -> Path | None:
    """Convert `folder`'s MP3 tracks into one M4B beside them. None means "file them as they are".

    Order matters. Encode first, verify, and only then unlink the MP3s: a failure at any point
    leaves the download untouched and the caller falls back to today's behaviour. The MP3s are
    removed by name, never rewritten — for a torrent those names are hard links to seeding files,
    and dropping a name does not disturb the data or the client.
    """
    if not settings.convert_mp3_to_m4b or not available():
        return None
    if book.get("kind") == "ebook" or not needs_conversion(folder):
        return None

    files = media.audio_files(folder)
    out = folder / f"{_safe_title(book)}.m4b"
    if out.exists():
        log.info("Book %s: %s already exists, not converting", book.get("id"), out.name)
        return None

    try:
        if report:
            report("Reading track lengths…")
        durations = [await asyncio.to_thread(_duration, f) for f in files]
        listing, meta = build_inputs(files, durations, book)
        if report:
            report(f"Converting {len(files)} tracks to M4B…")
        await asyncio.to_thread(_encode, folder, listing, meta, out, book, settings)

        if not out.is_file() or out.stat().st_size == 0:
            raise RuntimeError("ffmpeg produced no file")
        probe = await asyncio.to_thread(media.audio_files, folder)
        if out not in probe:
            raise RuntimeError("the converted file isn't readable as audio")

        for f in files:
            f.unlink(missing_ok=True)  # the name only; a hard-linked seeding copy keeps its data
        shutil.rmtree(folder / ".narratarr-convert", ignore_errors=True)

        # Reuse the tag writer the Tidy feature already uses, now that the M4B is the only audio.
        tidy.write_tags(folder, book)
        log.info("Book %s: converted %d MP3s to %s", book.get("id"), len(files), out.name)
        return out
    except Exception as e:  # noqa: BLE001 - a failed conversion must never fail the download
        log.warning("Book %s: MP3 to M4B conversion failed, filing the MP3s: %s", book.get("id"), e)
        out.unlink(missing_ok=True)  # never leave a half-written M4B for the library
        return None


def _safe_title(book: dict) -> str:
    from .worker import safe_name

    return safe_name(book.get("title") or "Untitled")
