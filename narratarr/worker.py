"""Background download pipeline.

For each queued book:
  resolving   - make sure we have the info hash (scrape the book page if needed)
  caching     - hand the magnet to Real-Debrid and wait for it to finish on their side
  downloading - unrestrict each link and stream the files to a temp folder
  organizing  - unzip, move into the library, add cover/description, notify Audiobookshelf

Releases can also go to the user's own download clients instead: Usenet NZBs to
SABnzbd or NZBGet, torrents to qBittorrent, Transmission or Deluge. Narratarr waits for
the client to finish, then brings the files into the library: moved for Usenet,
hard-linked or copied for torrents so they can keep seeding.

Local imports (books the user already has) are copied or moved in from a folder.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
import threading
import time
import zipfile
from pathlib import Path

import httpx

from . import audiobookshelf, config, db, downloaders, importer, media, wanted
from .downloaders import ClientError
from .realdebrid import DEAD, RealDebrid, RealDebridError
from .sources import Details, ProwlarrError, get_source
from .sources.prowlarr import fetch
from .sources.torznab import fetch as torznab_fetch

log = logging.getLogger("narratarr.worker")

POLL_SECONDS = 4
AUTOMATION_SECONDS = 60  # how often to check the import folder
AUTO_SEARCH_SECONDS = 10 * 60  # how often to look for a few Wanted books
RD_TIMEOUT_SECONDS = 6 * 60 * 60
CLIENT_TIMEOUT_SECONDS = 7 * 24 * 60 * 60  # a torrent with no seeds shouldn't hold a download slot forever
MAX_COVER_BYTES = 20 << 20
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(part: str, fallback: str = "Unknown") -> str:
    part = _UNSAFE.sub("", part).strip().strip(".")
    return " ".join(part.split())[:150] or fallback


def destination(book: dict, settings: config.Settings) -> Path:
    root = Path(settings.library_dir).expanduser().resolve()
    values = {
        "author": safe_name(book["author"], "Unknown Author"),
        "title": safe_name(book["title"], "Untitled"),
        "narrator": safe_name(", ".join(book["narrators"][:2]), "Unknown Narrator"),
    }
    template = settings.folder_template or "{author}/{title}"
    try:
        parts = [safe_name(p.format(**values)) for p in template.split("/") if p.strip()]
    except (KeyError, IndexError, ValueError):
        parts = [values["author"], values["title"]]
    dest = root.joinpath(*parts).resolve()
    if root not in dest.parents:
        raise ValueError("Folder template points outside the library folder")
    return dest


def mapped_path(storage: str, settings: config.Settings) -> Path:
    """Translate a folder as a download client sees it into one Narratarr can open (e.g. across containers)."""
    remote, local = settings.path_remote.rstrip("/\\"), settings.path_local.rstrip("/\\")
    if remote and local and storage.startswith(remote) and storage[len(remote):][:1] in ("", "/", "\\"):
        storage = local + storage[len(remote):].replace("\\", "/")
    return Path(storage)


def take(src: Path, into: Path, stop=None) -> None:
    """Move a finished download (a folder or a single file) into our temp folder. Resumable."""
    importer.bring_in(src, into, True, None, stop)


def has_files(folder: Path) -> bool:
    return folder.is_dir() and any(p.is_file() for p in folder.rglob("*"))


def free_name(target: Path) -> Path:
    """'Dune.m4b' -> 'Dune (2).m4b' if the first is taken, and so on."""
    n = 2
    while target.exists():
        target = target.with_name(f"{target.stem} ({n}){target.suffix}")
        n += 1
    return target


async def read_up_to(resp: httpx.Response, limit: int) -> bytes | None:
    """The response body, or None if it's bigger than we're willing to hold in memory."""
    chunks, size = [], 0
    async for chunk in resp.aiter_bytes(1 << 16):
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def tmp_dir(book: dict, settings: config.Settings) -> Path:
    # Keyed by id *and* origin, so a leftover from a reset database can't be mistaken for this book's.
    tag = hashlib.sha1(book["source_url"].encode()).hexdigest()[:8]
    return Path(settings.library_dir).expanduser() / ".narratarr-tmp" / f"{book['id']}-{tag}"


def unique_destination(book: dict, settings: config.Settings) -> Path:
    """The book's folder: the one it already lives in, or a fresh one nobody else uses.

    Two books can resolve to the same folder (a template like "{author}", or two editions
    with the same title), and the library may hold folders Narratarr never made. Sharing
    one would mix files and let "delete files" on one book wipe another's.
    """
    root = Path(settings.library_dir).expanduser().resolve()
    own = Path(book["path"]).resolve() if book.get("path") else None
    if own and root in own.parents and own.is_dir():
        return own  # downloading again goes back where the book already lives
    dest = destination(book, settings)
    taken = {Path(b["path"]).resolve() for b in db.list_books() if b["path"] and b["id"] != book["id"]}
    base, n = dest, 2
    while dest in taken or any(dest in t.parents for t in taken) or has_files(dest):
        dest = base.with_name(f"{base.name} ({n})")
        n += 1
    return dest


def readable_error(e: Exception) -> str:
    """An error message fit for the UI: no request URLs (they can carry tokens)."""
    if isinstance(e, httpx.HTTPStatusError):
        return f"The download server answered with an error ({e.response.status_code})."
    if isinstance(e, httpx.TimeoutException):
        return "The download timed out. Check your connection and try again."
    if isinstance(e, httpx.HTTPError):
        return "Couldn't reach the download server. Check your connection and try again."
    return str(e) or type(e).__name__


def link_into(src: Path, into: Path, stop=None) -> None:
    """Bring a finished torrent into our temp folder without disturbing it, so it can keep seeding.

    Hard links cost no extra space when the library is on the same drive; otherwise we copy.
    """
    files = [src] if src.is_file() else [p for p in src.rglob("*") if p.is_file()]
    for f in files:
        if stop is not None and stop.is_set():
            return
        target = into / (f.name if src.is_file() else f.relative_to(src))
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.hardlink_to(f)
        except OSError:
            shutil.copy2(f, target)


def extract_zip(archive: Path, into: Path) -> None:
    into = into.resolve()
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            target = (into / member.filename).resolve()
            if into != target and into not in target.parents:
                raise ValueError(f"Unsafe path in archive: {member.filename}")
        zf.extractall(into)
    archive.unlink()


class Worker:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        # transport is injectable so tests can fake Real-Debrid and file hosts.
        self.running: dict[int, asyncio.Task] = {}
        self.threads: dict[int, asyncio.Future] = {}  # file work in progress, which a cancel can't interrupt
        self.stops: dict[int, threading.Event] = {}
        self._transport = transport
        self._loop_task: asyncio.Task | None = None
        self._auto_task: asyncio.Task | None = None
        self._wake = asyncio.Event()

    # ---- scheduling -------------------------------------------------------

    def start(self) -> None:
        self._loop_task = asyncio.create_task(self._loop())
        self._auto_task = asyncio.create_task(self._automation())

    async def stop(self) -> None:
        tasks = [t for t in [self._loop_task, self._auto_task, *self.running.values()] if t]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def poke(self) -> None:
        self._wake.set()

    def cancel(self, book_id: int) -> bool:
        if book_id in self.stops:
            self.stops[book_id].set()
        task = self.running.get(book_id)
        if task:
            task.cancel()
            return True
        return False

    async def cancel_and_wait(self, book_id: int) -> None:
        """Cancel a book's job and wait until it has really stopped touching files."""
        self.cancel(book_id)
        pending = [t for t in (self.running.get(book_id), self.threads.get(book_id)) if t]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _in_thread(self, book_id: int, fn, *args):
        """Run file work in a thread. It keeps going if the task is cancelled, so track it."""
        fut = asyncio.ensure_future(asyncio.to_thread(fn, *args, self.stops.get(book_id)))
        self.threads[book_id] = fut
        fut.add_done_callback(lambda _: self.threads.pop(book_id, None))
        return await asyncio.shield(fut)

    async def _loop(self) -> None:
        while True:
            try:
                limit = config.load().max_concurrent
                while len(self.running) < limit:
                    book = db.next_queued(set(self.running))
                    if not book:
                        break
                    self._spawn(book)
            except Exception:
                log.exception("Scheduler error")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=3)
            except TimeoutError:
                pass

    async def _automation(self) -> None:
        """Background chores: the import folder, automatic searches for Wanted books, followed authors."""
        last_search = last_follow = 0.0
        while True:
            try:
                await self.watch_folder()
                now = time.monotonic()
                if now - last_search >= AUTO_SEARCH_SECONDS:
                    last_search = now
                    if await wanted.auto_search():
                        self.poke()
                if now - last_follow >= 30 * 60:
                    last_follow = now
                    if await wanted.refresh_due():
                        if await wanted.auto_search():
                            self.poke()
            except Exception:
                log.exception("Automation error")
            await asyncio.sleep(AUTOMATION_SECONDS)

    async def watch_folder(self) -> int:
        """Queue any finished book that has appeared in the import folder."""
        s = config.load()
        root = Path(s.import_folder).expanduser() if s.import_folder else None
        if not root or not root.is_dir():
            return 0
        library = Path(s.library_dir).expanduser().resolve()
        if root.resolve() == library or library in root.resolve().parents or root.resolve() in library.parents:
            log.warning("The import folder and the library folder overlap; not watching it.")
            return 0
        known = {b["path"] for b in db.list_books() if b["path"]}
        queued = 0
        for found in await asyncio.to_thread(importer.scan, root):
            path = Path(found["path"])
            url = importer.url_for(path)
            if str(path) in known or db.get_by_url(url) or not importer.quiet_for(path, 120):
                continue  # already a library book, already handled, or still being copied in
            db.insert(importer.as_book(found, "move", "queued"))
            queued += 1
        if queued:
            self.poke()
        return queued

    def _spawn(self, book: dict) -> None:
        book_id = book["id"]
        db.update(book_id, status="resolving", error="", detail="Starting…", progress=0)
        self.stops[book_id] = threading.Event()
        task = asyncio.create_task(self._run(book_id))
        self.running[book_id] = task
        task.add_done_callback(lambda _: (self.running.pop(book_id, None), self.stops.pop(book_id, None), self.poke()))

    async def _run(self, book_id: int) -> None:
        try:
            await self.process(book_id)
        except asyncio.CancelledError:
            raise
        except (
            RealDebridError, ClientError, ProwlarrError, httpx.HTTPError, ValueError, OSError, zipfile.BadZipFile
        ) as e:
            log.warning("Book %s failed: %s", book_id, readable_error(e))
            db.update(book_id, status="failed", error=readable_error(e), speed=0, detail="")
        except Exception as e:
            log.exception("Book %s crashed", book_id)
            db.update(book_id, status="failed", error=f"Unexpected error: {e}", speed=0, detail="")

    # ---- pipeline ---------------------------------------------------------

    async def process(self, book_id: int) -> None:
        book = db.get(book_id)
        if book is None:
            return
        if book["source"] == "local":
            return await self._process_import(book)
        source = get_source(book["source"])
        if source.delivery == "direct":
            await self._process_direct(book, source)
        elif source.delivery == "wanted":
            raise ValueError("Pick a release for this book first: open it and choose Find releases.")
        elif book["protocol"] == "usenet":
            await self._process_usenet(book)
        elif config.load().torrent_client != "realdebrid":
            await self._process_torrent_client(book, source)
        else:
            await self._process_torrent(book, source)

    def _fresh_tmp(self, book: dict, settings: config.Settings) -> Path:
        tmp = tmp_dir(book, settings)
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        return tmp

    async def _finish(self, book: dict, tmp: Path, settings: config.Settings, **extra) -> None:
        db.update(book["id"], status="organizing", progress=100, speed=0, detail="Organizing files…")
        dest = await self._organize(book, tmp, settings)
        db.update(
            book["id"], status="completed", path=str(dest), progress=100, speed=0, detail="",
            completed_at=time.time(), **extra,
        )
        await self._notify_audiobookshelf(settings)

    async def _process_direct(self, book: dict, source) -> None:
        """Free sources (LibriVox, Old-Time Radio): plain HTTPS downloads, no Real-Debrid."""
        settings = config.load()
        book_id = book["id"]
        if not book["files"]:
            db.update(book_id, detail="Reading book page…")
            details = await source.details(book["source_url"])
            if not details.files:
                raise ValueError("This item has no downloadable audio files.")
            db.update(book_id, files=details.files)
            book = db.get(book_id)

        db.update(book_id, status="downloading", progress=0, detail="Starting download…")
        files = [
            # Keep sub-folder names in the file name so same-named files don't collide.
            {"filename": f["name"].replace("/", " - "), "filesize": f.get("size", 0), "download": f["url"]}
            for f in book["files"]
        ]
        tmp = self._fresh_tmp(book, settings)
        await self._download(book_id, files, tmp)
        await self._finish(book, tmp, settings)

    async def _process_torrent(self, book: dict, source) -> None:
        settings = config.load()
        book_id = book["id"]
        async with RealDebrid(settings.rd_api_key, transport=self._transport) as rd:
            torrent_id = book["rd_torrent_id"]
            if torrent_id:
                try:
                    await rd.torrent_info(torrent_id)
                except RealDebridError:
                    torrent_id = ""
            if not torrent_id:
                torrent_id = await self._add_to_rd(rd, book, source, settings)
                db.update(book_id, rd_torrent_id=torrent_id)

            db.update(book_id, status="caching", detail="Sent to Real-Debrid")
            info = await self._wait_for_rd(rd, book_id, torrent_id)

            links = info.get("links") or []
            if not links:
                raise RealDebridError("Real-Debrid finished but returned no files.")

            db.update(book_id, status="downloading", progress=0, detail="Preparing download…")
            direct = [await rd.unrestrict(link) for link in links]
            tmp = self._fresh_tmp(book, settings)
            await self._download(book_id, direct, tmp)

            if settings.delete_from_rd_after:
                try:
                    await rd.delete_torrent(torrent_id)
                except (RealDebridError, httpx.HTTPError):
                    log.info("Couldn't remove torrent %s from Real-Debrid", torrent_id)

        await self._finish(book, tmp, settings, rd_torrent_id="" if settings.delete_from_rd_after else torrent_id)

    async def _torrent_payload(self, book: dict, source, settings: config.Settings) -> tuple[str, str | bytes]:
        """What to hand a torrent client: ("magnet", uri) or ("torrent", .torrent bytes)."""
        book_id = book["id"]
        if not book["info_hash"] and book["download_url"]:
            # A release without a published info hash: fetch the .torrent (or magnet) from the
            # indexer that listed it. Torznab needs its own address and key, and its own key param.
            if book.get("source") == "torznab":
                db.update(book_id, detail="Fetching the torrent from your indexer…")
                kind, payload = await torznab_fetch(
                    book["download_url"], settings.torznab_url, settings.torznab_api_key, self._transport
                )
            else:
                db.update(book_id, detail="Fetching the torrent from Prowlarr…")
                kind, payload = await fetch(
                    book["download_url"], settings.prowlarr_url, settings.prowlarr_api_key, self._transport
                )
            if kind == "nzb":
                raise ProwlarrError("This is a Usenet release, not a torrent.")
            return kind, payload

        if not book["info_hash"]:
            db.update(book_id, detail="Reading book page…")
            details = await source.details(book["source_url"])
            if not details.info_hash:
                raise ValueError("This listing doesn't show an info hash.")
            db.update(book_id, info_hash=details.info_hash, trackers=details.trackers)
            book = db.get(book_id)

        magnet = Details(
            url=book["source_url"], title=book["title"], info_hash=book["info_hash"], trackers=book["trackers"]
        ).magnet()
        return "magnet", magnet

    async def _add_to_rd(self, rd: RealDebrid, book: dict, source, settings: config.Settings) -> str:
        kind, payload = await self._torrent_payload(book, source, settings)
        return await (rd.add_torrent(payload) if kind == "torrent" else rd.add_magnet(payload))

    async def _resume_job(self, client, book: dict, name: str) -> str:
        """The client's id for this book's job, if the client still has it (e.g. after a restart)."""
        job_id = book["client_job_id"]
        if job_id and book["download_client"] == name and await client.job(job_id) is not None:
            return job_id
        return ""

    async def _process_usenet(self, book: dict) -> None:
        """Usenet releases: hand the NZB to SABnzbd or NZBGet, wait, then move in what it unpacked."""
        settings = config.load()
        book_id = book["id"]
        name = settings.usenet_client
        tmp = tmp_dir(book, settings)
        leftover = bool(book["client_job_id"]) and has_files(tmp)  # an interrupted run got this far
        async with downloaders.usenet_client(settings, self._transport) as client:
            job_id = await self._resume_job(client, book, name)
            if not job_id and leftover:
                job_id = ""
                return await self._finish(book, tmp, settings, client_job_id="")
            if not job_id:
                if not book["download_url"]:
                    raise ValueError("This release has no NZB link. Search for it again.")
                db.update(book_id, detail="Fetching the NZB from Prowlarr…")
                kind, nzb = await fetch(
                    book["download_url"], settings.prowlarr_url, settings.prowlarr_api_key, self._transport
                )
                if kind != "nzb":
                    raise ProwlarrError("The indexer sent a torrent for this Usenet release.")
                label = safe_name(f"{book['author']} - {book['title']}" if book["author"] else book["title"])
                job_id = await client.add_nzb(nzb, f"{label}.nzb", label, downloaders.category_for(settings, name))
                db.update(book_id, download_client=name, client_job_id=job_id)

            db.update(book_id, status="downloading", progress=0, detail=f"Sent to {client.label}")
            try:
                folder = await self._wait_for_client(client, book_id, job_id, settings)
            except ValueError:
                if not leftover:
                    raise
                folder = None  # an earlier run already moved everything out of the client's folder
            if leftover:
                tmp.mkdir(parents=True, exist_ok=True)
            else:
                tmp = self._fresh_tmp(book, settings)
            if folder is not None:
                await self._in_thread(book_id, take, folder, tmp)

        await self._finish(book, tmp, settings, client_job_id="")
        try:
            async with downloaders.usenet_client(settings, self._transport) as client:
                await client.forget(job_id)
        except ClientError:
            log.info("Couldn't clear job %s from the Usenet client's history", job_id)

    async def _process_torrent_client(self, book: dict, source) -> None:
        """Torrents through the user's own qBittorrent, Transmission or Deluge. The torrent keeps seeding afterwards."""
        settings = config.load()
        book_id = book["id"]
        name = settings.torrent_client
        async with downloaders.torrent_client(settings, self._transport) as client:
            job_id = await self._resume_job(client, book, name)
            if not job_id:
                kind, payload = await self._torrent_payload(book, source, settings)
                tag = f"narratarr-{book_id}"
                job_id = await client.add(kind, payload, tag, downloaders.category_for(settings, name))
                db.update(book_id, download_client=name, client_job_id=job_id, info_hash=book["info_hash"] or job_id)

            db.update(book_id, status="downloading", progress=0, detail=f"Sent to {client.label}")
            folder = await self._wait_for_client(client, book_id, job_id, settings)
            if not (folder.is_file() and folder.suffix.lower() in media.AUDIO_TYPES) and not media.audio_files(folder):
                raise ValueError("The torrent finished, but there are no audio files in it.")
            tmp = self._fresh_tmp(book, settings)
            db.update(book_id, detail="Copying files from the torrent…")
            await self._in_thread(book_id, link_into, folder, tmp)

        await self._finish(book, tmp, settings)

    async def _wait_for_client(self, client, book_id: int, job_id: str, settings: config.Settings) -> Path:
        """Poll a download client until the job is done; returns the finished files as Narratarr sees them."""
        started = time.monotonic()
        while True:
            if time.monotonic() - started > CLIENT_TIMEOUT_SECONDS:
                raise ClientError(f"{client.label} hasn't finished this download in a week. It may have no seeds.")
            job = await client.job(job_id)
            if job is None:
                raise ClientError(f"{client.label} no longer has this download. It may have been removed there.")
            if job.state == "failed":
                raise ClientError(job.error or f"{client.label} couldn't finish this download.")
            if job.state == "completed":
                folder = mapped_path(job.path, settings)
                if not job.path or not folder.exists():
                    raise ValueError(
                        f"{client.label} saved this book to {job.path or 'a folder it did not name'}, but Narratarr "
                        "can't see that folder. If they run in separate containers, give both the same downloads "
                        "folder, or set the path mapping in Settings."
                    )
                return folder
            db.update(book_id, progress=job.progress, speed=job.speed, detail=job.detail)
            await asyncio.sleep(POLL_SECONDS)

    async def _process_import(self, book: dict) -> None:
        """Books the user already has: copy or move them in from a folder on this computer."""
        settings = config.load()
        book_id = book["id"]
        src = importer.path_of(book["source_url"])
        moving = book["import_mode"] == "move"
        # A move that was interrupted has files in the temp folder already; never wipe those.
        tmp = tmp_dir(book, settings)
        resumed = moving and has_files(tmp)
        if not resumed:
            tmp = self._fresh_tmp(book, settings)
        if not src.exists():
            if resumed:
                return await self._finish(book, tmp, settings, source_url=f"{book['source_url']}#imported-{int(time.time())}")
            raise ValueError(f"{src} isn't there any more.")
        db.update(book_id, status="downloading", progress=0, detail="Moving files in…" if moving else "Copying files…")

        def report(done: int, total: int, name: str) -> None:
            db.update(book_id, progress=done * 100 / max(total, 1), detail=f"{'Moving' if moving else 'Copying'} {name}")

        await self._in_thread(book_id, importer.bring_in, src, tmp, moving, report)
        # Once moved, the original path is free again for a different book dropped there later.
        extra = {"source_url": f"{book['source_url']}#imported-{int(time.time())}"} if moving else {}
        await self._finish(book, tmp, settings, **extra)

    async def _wait_for_rd(self, rd: RealDebrid, book_id: int, torrent_id: str) -> dict:
        started = time.monotonic()
        while True:
            info = await rd.torrent_info(torrent_id)
            status = info.get("status", "")
            if status == "downloaded":
                return info
            if status in DEAD:
                raise RealDebridError(f"Real-Debrid couldn't fetch this torrent ({status}).")
            if status == "waiting_files_selection":
                await rd.select_all_files(torrent_id)
            label = {
                "magnet_conversion": "Real-Debrid is reading the magnet…",
                "waiting_files_selection": "Selecting files…",
                "queued": "Queued on Real-Debrid",
                "downloading": f"Real-Debrid is caching · {info.get('seeders') or 0} seeders",
                "compressing": "Real-Debrid is packaging files…",
                "uploading": "Real-Debrid is finishing up…",
            }.get(status, f"Real-Debrid: {status}")
            db.update(book_id, progress=float(info.get("progress") or 0), speed=int(info.get("speed") or 0), detail=label)
            if time.monotonic() - started > RD_TIMEOUT_SECONDS:
                raise RealDebridError("Timed out waiting for Real-Debrid. The torrent may have no seeders.")
            await asyncio.sleep(POLL_SECONDS)

    async def _download(self, book_id: int, files: list[dict], tmp: Path) -> None:
        total = sum(int(f.get("filesize") or 0) for f in files) or 1
        done = 0
        last_report, window_start, window_bytes = 0.0, time.monotonic(), 0
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=httpx.Timeout(60, read=120), transport=self._transport
        ) as client:
            for i, f in enumerate(files, 1):
                name = safe_name(f.get("filename") or f"part-{i}")
                async with client.stream("GET", f["download"]) as resp:
                    resp.raise_for_status()
                    with open(free_name(tmp / name), "wb") as out:
                        async for chunk in resp.aiter_bytes(1 << 16):
                            out.write(chunk)
                            done += len(chunk)
                            window_bytes += len(chunk)
                            now = time.monotonic()
                            if now - last_report >= 0.75:
                                speed = int(window_bytes / max(now - window_start, 1e-6))
                                window_start, window_bytes, last_report = now, 0, now
                                db.update(
                                    book_id,
                                    progress=min(100.0, done * 100 / total),
                                    speed=speed,
                                    detail=f"Downloading {i} of {len(files)} · {name}",
                                )

    async def _organize(self, book: dict, tmp: Path, settings: config.Settings) -> Path:
        for archive in list(tmp.glob("*.zip")):
            await asyncio.to_thread(extract_zip, archive, tmp)

        dest = unique_destination(book, settings)
        replacing = bool(book.get("path")) and Path(book["path"]).resolve() == dest  # downloading again
        dest.mkdir(parents=True, exist_ok=True)
        db.update(book["id"], path=str(dest))  # claim it, so a parallel download can't pick the same folder
        for item in sorted(tmp.iterdir()):
            target = dest / item.name
            if target.exists():
                if replacing:
                    shutil.rmtree(target) if target.is_dir() else target.unlink()
                else:
                    target = free_name(target)
            await self._in_thread(book["id"], lambda a, b, _stop: shutil.move(a, b), str(item), str(target))
        shutil.rmtree(tmp, ignore_errors=True)

        # Audiobookshelf picks these up automatically.
        if book["description"]:
            (dest / "desc.txt").write_text(book["description"])
        if book["narrators"]:
            (dest / "reader.txt").write_text(", ".join(book["narrators"]))
        if book["cover_url"].startswith(("http://", "https://")) and not any(dest.glob("cover.*")):
            try:
                async with httpx.AsyncClient(follow_redirects=True, timeout=20, transport=self._transport) as client:
                    async with client.stream("GET", book["cover_url"]) as resp:
                        kind = resp.headers.get("content-type", "").split(";")[0]
                        if resp.is_success and kind.startswith("image/"):
                            data = await read_up_to(resp, MAX_COVER_BYTES)
                            if data:
                                ext = {"image/png": "png", "image/webp": "webp"}.get(kind, "jpg")
                                (dest / f"cover.{ext}").write_bytes(data)
            except httpx.HTTPError:
                pass
        await asyncio.to_thread(importer.extract_cover, dest)
        return dest

    async def _notify_audiobookshelf(self, settings: config.Settings) -> None:
        if not (settings.audiobookshelf_url and settings.audiobookshelf_token and settings.audiobookshelf_library_id):
            return
        try:
            async with audiobookshelf.Audiobookshelf(settings.audiobookshelf_url, settings.audiobookshelf_token) as server:
                await server.scan(settings.audiobookshelf_library_id)
        except audiobookshelf.AudiobookshelfError as e:
            log.warning("Audiobookshelf didn't rescan after this download: %s", e)
