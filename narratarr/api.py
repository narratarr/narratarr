"""HTTP API + static web UI."""

from __future__ import annotations

import asyncio
import base64
import hmac
import ipaddress
import os
import platform
import re
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (apiv1, audible, audiobookshelf, auth, brave, config, db, downloaders, importer, media, search, tokens,
               updates, users, wanted)
from .audiobookshelf import Audiobookshelf, AudiobookshelfError
from .downloaders import ClientError
from .realdebrid import RealDebrid, RealDebridError
from .sources import ORDER, Prowlarr, ProwlarrError, all_sources, get_source, source_for_url
from .worker import Worker, has_files, readable_error, tmp_dir

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    auth.adopt_env_password()
    auth.announce_setup()
    worker = Worker()
    app.state.worker = worker
    app.state.tasks = set()
    worker.start()
    yield
    await worker.stop()


# The internal API is the web UI's and changes freely, so it isn't published; apps use /api/v1.
app = FastAPI(title="Narratarr", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.mount("/api/v1", apiv1.v1)


@app.middleware("http")
async def revalidate_static(request, call_next):
    # Make browsers re-check UI files so updates show up without a hard refresh.
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# GETs that cost something (an indexer or Brave query) get the same protection as writes.
COSTLY_GETS = ("/api/discover", "/api/suggest", "/api/authors/search")


def host_allowed(host: str) -> bool:
    """Only answer to names that can't be hijacked through DNS rebinding.

    A malicious website can point its own domain at 127.0.0.1 and then talk to this app
    as if it were same-origin. IP addresses, plain machine names and local-only domains
    can't be used that way; anything else must be listed in NARRATARR_HOSTS.
    """
    name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host  # drop a :port, keep IPv6
    name = name.strip("[]").lower()
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    if "." not in name or name.endswith((".local", ".lan", ".home", ".internal", ".home.arpa", ".ts.net")):
        return True
    extra = {h.strip().lower() for h in os.environ.get("NARRATARR_HOSTS", "").split(",") if h.strip()}
    return name in extra or any(name.endswith("." + h.lstrip("*.")) for h in extra if h.startswith("*."))


def cross_site(request) -> bool:
    """Whether a browser is making this request on behalf of another website."""
    site = request.headers.get("sec-fetch-site", "")
    if site:  # modern browsers say outright
        return site in ("cross-site", "same-site")
    origin = request.headers.get("origin", "")
    if not origin:
        return False  # curl, scripts, old browsers' GETs
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return urlparse(origin).netloc.lower() != host.split(",")[0].strip().lower()


@app.middleware("http")
async def same_site_only(request, call_next):
    """Refuse requests that a *website* in the user's browser sends here, and unknown hostnames.

    Nothing on another site should be able to start downloads, delete books, move folders,
    or spend the user's indexer and Brave quotas on this machine.
    """
    if request.url.path != "/api/health" and not host_allowed(request.headers.get("host", "")):
        return Response(
            "Narratarr doesn't answer to this hostname. Open it by IP address or machine name, "
            "or add the name to NARRATARR_HOSTS.", status_code=421,
        )
    guarded = request.method in ("POST", "PUT", "PATCH", "DELETE") or request.url.path.startswith(COSTLY_GETS)
    if guarded and cross_site(request):
        return Response("Requests from other websites aren't allowed.", status_code=403)
    return await call_next(request)


@app.middleware("http")
async def require_sign_in(request, call_next):
    """Every API call needs a signed-in user (see auth.py for the exceptions); some need an admin."""
    path = request.url.path
    if not path.startswith("/api/") or path in auth.OPEN or path == "/api/v1" or path.startswith("/api/v1/"):
        return await call_next(request)
    user = auth.current_user(request)
    if user is None:
        setup = users.count() == 0
        return JSONResponse({"detail": "Create the admin account first." if setup else "Sign in first.",
                             "setup": setup}, status_code=401)
    if user["role"] != "admin" and auth.admin_only(request.method, path):
        return JSONResponse({"detail": "Only an admin can do that."}, status_code=403)
    request.state.user = user
    token = db.viewer.set(user["id"])
    try:
        return await call_next(request)
    finally:
        db.viewer.reset(token)


def _worker() -> Worker:
    return app.state.worker


def _source_error(e: Exception, label: str = "AudiobookBay") -> HTTPException:
    if isinstance(e, httpx.HTTPStatusError):
        return HTTPException(502, f"{label} returned an error ({e.response.status_code}). It may be down or moved.")
    return HTTPException(
        502, f"Couldn't reach {label}. The site may be down. Try again in a few minutes, or try another source."
    )


def _resolve(url: str, name: str | None = None):
    """The source a book link belongs to (the named one, if given)."""
    try:
        source = get_source(name) if name else source_for_url(url)
    except ValueError:
        raise HTTPException(400, "Unknown source.")
    if source is None or not source.owns(url):
        raise HTTPException(400, "That link isn't from a supported source.")
    return source


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health", include_in_schema=False)
async def health():
    return {"ok": True}


# ---- discover -------------------------------------------------------------


@app.get("/api/sources")
async def sources():
    found = all_sources()
    settings = config.load().public()
    return [
        {
            "name": name,
            "label": found[name].label,
            "description": found[name].description,
            "delivery": found[name].delivery,
            "default_label": found[name].default_label,
            "categories": [{"slug": c.slug, "name": c.name} for c in found[name].categories()],
        }
        for name in ORDER
        if name not in ("prowlarr", "audible") or settings["prowlarr_configured"]
    ]


@app.get("/api/discover")
async def discover(q: str = "", category: str = "", page: int = 1, source: str = "librivox"):
    try:
        src = get_source(source)
    except ValueError:
        raise HTTPException(400, "Unknown source.")
    settings = config.load()
    use_brave = src.name == "audiobookbay" and settings.search_provider == "brave" and settings.brave_api_key
    try:
        if q.strip() and use_brave:
            data = await search.brave_smart_search(
                q.strip(), settings.brave_api_key, settings.abb_base_url, page, settings.smart_search
            )
        elif q.strip() and src.name == "prowlarr":
            data = await search.careful_search(src, q.strip(), settings.smart_search)
        elif q.strip() and src.name == "audible":
            data = {**(await src.search(q.strip(), page)).to_dict(), "did_you_mean": ""}
        elif q.strip():
            data = await search.smart_search(src, q.strip(), page, use_suggestions=settings.smart_search)
        elif src.name == "audiobookbay":
            data = await search.latest_batch(src, page, category or None)  # the site shows only 9 per page
        else:
            data = (await src.latest(page, category or None)).to_dict()
    except (ProwlarrError, audible.AudibleError) as e:
        raise HTTPException(502, str(e))
    except httpx.HTTPError as e:
        raise _source_error(e, src.label)
    except brave.BraveError as e:
        raise HTTPException(502, str(e))
    books = db.list_books()
    owned = {b["release_url"]: b for b in books if b["release_url"]} | {b["source_url"]: b for b in books}
    for item in data["items"]:
        b = owned.get(item["url"])
        item["source"] = src.name
        item["library"] = {"id": b["id"], "status": b["status"]} if b else None
    return data


@app.get("/api/suggest")
async def suggest(q: str):
    if len(q.strip()) < 3 or not config.load().smart_search:
        return []
    return search.relevant(q, await search.suggestions(q.strip(), limit=8))[:6]


@app.get("/api/details")
async def details(url: str, source: str | None = None):
    src = _resolve(url, source)
    try:
        d = await src.details(url)
    except ProwlarrError as e:
        raise HTTPException(410, str(e))
    except audible.AudibleError as e:
        raise HTTPException(502, str(e))
    except httpx.HTTPError as e:
        raise _source_error(e, src.label)
    data = d.to_dict()
    data["source"] = src.name
    b = db.get_by_url(url)
    data["library"] = {"id": b["id"], "status": b["status"]} if b else None
    return data


# ---- library --------------------------------------------------------------


class AddBook(BaseModel):
    url: str
    download: bool = True
    source: str | None = None


@app.get("/api/library")
async def library():
    return db.list_books()


@app.get("/api/stats")
async def stats():
    return db.stats()


@app.post("/api/library")
async def add_book(body: AddBook, request: Request):
    source = _resolve(body.url, body.source)
    me = getattr(request.state, "user", None)
    if source.name == "audible":
        return await _add_wanted(body.url, body.download, me["id"] if me else None)
    existing = db.get_by_url(body.url)
    if existing:
        if body.download and existing["status"] in ("wanted", "failed"):
            db.update(existing["id"], status="queued", error="")
            _worker().poke()
        return db.get(existing["id"])
    try:
        d = await source.details(body.url)
    except ProwlarrError as e:
        raise HTTPException(410, str(e))
    except httpx.HTTPError as e:
        raise _source_error(e, source.label)
    book = db.insert(
        {
            "source": source.name,
            "source_url": d.url,
            "title": d.title,
            "author": d.author,
            "narrators": d.narrators,
            "cover_url": d.cover_url,
            "description": d.description,
            "language": d.language,
            "categories": d.categories,
            "format": d.format,
            "bitrate": d.bitrate,
            "length": d.length,
            "size": d.size,
            "info_hash": d.info_hash,
            "trackers": d.trackers,
            "files": d.files,
            "protocol": d.protocol,
            "indexer": d.indexer,
            "release": d.release,
            "download_url": d.download_url,
            "status": "queued" if body.download else "wanted",
            "requested_by": me["id"] if me else None,
        }
    )
    if body.download:
        _worker().poke()
    return book


async def _add_wanted(url: str, search_now: bool, user_id: int | None = None) -> dict:
    """An Audible book goes to Wanted; Narratarr then looks for a release for it."""
    asin = audible.asin_from_url(url)
    try:
        b = await audible.book(asin)
    except audible.AudibleError as e:
        raise HTTPException(502, str(e))
    if not b:
        raise HTTPException(404, "Audible doesn't have that book any more.")
    added = wanted.add_wanted(b)
    if added and user_id:
        db.update(added["id"], requested_by=user_id)
        added = db.get(added["id"])
    book = added or wanted.in_library(asin, b["title"], b["author"]) or db.get_by_url(b["url"])
    if search_now and book["status"] == "wanted" and book["source"] == "audible":
        _background(_search_in_background(book["id"]))
    return book


def _background(coro) -> None:
    """Run a coroutine in the background, keeping a reference so it can't be garbage-collected mid-way."""
    task = asyncio.create_task(coro)
    app.state.tasks.add(task)
    task.add_done_callback(app.state.tasks.discard)


async def _search_in_background(book_id: int) -> None:
    try:
        if await wanted.search_and_grab(db.get(book_id)):
            _worker().poke()
    except Exception as e:
        db.update(book_id, search_note=readable_error(e))


def _get_or_404(book_id: int) -> dict:
    book = db.get(book_id)
    if not book:
        raise HTTPException(404, "Book not found")
    return book


@app.post("/api/library/{book_id}/download")
async def download(book_id: int):
    book = _get_or_404(book_id)
    if book["status"] in db.ACTIVE:
        return book
    if book["source"] == "audible":  # no release picked yet: look for one now
        try:
            pick = await wanted.search_and_grab(book)
        except ProwlarrError as e:
            raise HTTPException(502, str(e))
        if not pick:
            raise HTTPException(404, f"{db.get(book_id)['search_note']} Narratarr will keep looking.")
        _worker().poke()
        return db.get(book_id)
    changes = {"client_job_id": ""} if book["status"] == "failed" else {}  # a failed client job starts over
    db.update(book_id, status="queued", error="", progress=0, detail="", **changes)
    _worker().poke()
    return db.get(book_id)


async def _drop_from_client(book: dict) -> None:
    """Stopping a download in Narratarr stops it in SABnzbd, NZBGet, qBittorrent, Transmission or Deluge too."""
    if not book["client_job_id"] or book["status"] not in db.ACTIVE:
        return
    s = config.load()
    name = book["download_client"]
    try:
        if name in downloaders.USENET_CLIENTS:
            async with downloaders.usenet_client(s, name=name) as client:
                await client.delete(book["client_job_id"])
        elif name in ("qbittorrent", "transmission", "deluge"):
            async with downloaders.torrent_client(s, name=name) as client:
                await client.delete(book["client_job_id"], files=True, tag=f"narratarr-{book['id']}")
    except ClientError:
        pass
    db.update(book["id"], client_job_id="")


@app.post("/api/library/{book_id}/stop")
async def stop(book_id: int):
    book = _get_or_404(book_id)
    await _worker().cancel_and_wait(book_id)
    await _drop_from_client(book)
    db.update(book_id, status="wanted", speed=0, progress=0, detail="")
    return db.get(book_id)


@app.delete("/api/library/{book_id}")
async def remove(book_id: int, delete_files: bool = False):
    book = _get_or_404(book_id)
    await _worker().cancel_and_wait(book_id)
    await _drop_from_client(book)
    settings = config.load()
    if delete_files and book["path"]:
        root = Path(settings.library_dir).expanduser().resolve()
        path = Path(book["path"]).resolve()
        others = [Path(b["path"]).resolve() for b in db.list_books() if b["path"] and b["id"] != book_id]
        shared = any(o == path or path in o.parents for o in others)
        # Only ever delete inside the library, and never a folder another book also lives in.
        if root in path.parents and path.exists() and not shared:
            shutil.rmtree(path, ignore_errors=True)
    kept = await asyncio.to_thread(_clear_tmp, book, settings)
    db.delete(book_id)
    return {"ok": True, "kept": kept}


def _clear_tmp(book: dict, settings: config.Settings) -> str:
    """Drop a removed book's temp folder, unless it holds files that exist nowhere else.

    A move-style import or a Usenet download that was stopped part-way has the user's only
    copy sitting in there. Those go back where they came from, or to a "recovered" folder.
    """
    tmp = tmp_dir(book, settings)
    came_from_elsewhere = book["import_mode"] == "move" or book["download_client"] in downloaders.USENET_CLIENTS
    if not (has_files(tmp) and came_from_elsewhere):
        shutil.rmtree(tmp, ignore_errors=True)
        return ""
    target = None
    if book["import_mode"] == "move":
        original = importer.path_of(book["source_url"].split("#")[0])
        if not original.exists():
            files = [p for p in tmp.rglob("*") if p.is_file()]
            if original.suffix and len(files) == 1:  # it was a single loose file
                original.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(files[0]), str(original))
                shutil.rmtree(tmp, ignore_errors=True)
                return str(original)
            target = original
    if target is None:
        target = tmp.parent / f"recovered-{book['id']}-{int(time.time())}"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(tmp), str(target))
    return str(target)


# ---- player ---------------------------------------------------------------


def _book_dir(book_id: int) -> Path:
    book = _get_or_404(book_id)
    path = Path(book["path"]) if book["path"] else None
    if book["status"] != "completed" or not path or not path.is_dir():
        raise HTTPException(404, "This book hasn't been downloaded yet.")
    return path


@app.get("/api/library/{book_id}/media")
async def book_media(book_id: int):
    info = await asyncio.to_thread(media.describe, _book_dir(book_id))
    if not info["files"]:
        raise HTTPException(404, "No playable audio files were found in this book's folder.")
    return info


@app.get("/api/library/{book_id}/stream/{index}")
async def stream(book_id: int, index: int):
    book_dir = _book_dir(book_id)
    files = await asyncio.to_thread(media.audio_files, book_dir)
    if not 0 <= index < len(files):
        raise HTTPException(404, "No such file")
    path = files[index]
    # FileResponse handles Range requests, so the player can seek instantly.
    return FileResponse(path, media_type=media.AUDIO_TYPES[path.suffix.lower()])


@app.get("/api/library/{book_id}/cover")
async def cover(book_id: int):
    book = _get_or_404(book_id)
    if book["path"] and Path(book["path"]).is_dir():
        for found in sorted(Path(book["path"]).glob("cover.*")):
            return FileResponse(found)
        art = await asyncio.to_thread(importer.embedded_cover, Path(book["path"]))
        if art:
            return Response(art[0], media_type=art[1], headers={"Cache-Control": "max-age=86400"})
    if book["cover_url"].startswith(("http://", "https://")):
        return RedirectResponse(book["cover_url"])
    raise HTTPException(404, "No cover")


class Progress(BaseModel):
    position: float
    duration: float = 0
    finished: bool | None = None


@app.put("/api/library/{book_id}/progress")
async def save_progress(book_id: int, body: Progress, request: Request):
    _get_or_404(book_id)
    db.save_progress(request.state.user["id"], book_id, max(0.0, body.position), body.duration or None, body.finished)
    return {"ok": True}


# ---- wanted: releases ---------------------------------------------------------


@app.get("/api/library/{book_id}/releases")
async def releases(book_id: int):
    book = _get_or_404(book_id)
    try:
        found = await wanted.find_releases(book)
    except ProwlarrError as e:
        raise HTTPException(502, str(e))
    db.update(book_id, last_searched=time.time())
    return {"items": found[:60], "auto_match": wanted.AUTO_MATCH}


class Grab(BaseModel):
    url: str
    source: str


@app.post("/api/library/{book_id}/grab")
async def grab(book_id: int, body: Grab):
    book = _get_or_404(book_id)
    if book["status"] in db.ACTIVE:
        raise HTTPException(409, "This book is already downloading.")
    _resolve(body.url, body.source)
    try:
        book = await wanted.grab(book_id, body.url, body.source)
    except ProwlarrError as e:
        raise HTTPException(410, str(e))
    except httpx.HTTPError as e:
        raise _source_error(e, body.source)
    _worker().poke()
    return book


# ---- import ---------------------------------------------------------------------


class ScanFolder(BaseModel):
    folder: str


def _folder(path: str) -> Path:
    folder = Path(path.strip()).expanduser()
    if not path.strip() or not folder.is_dir():
        raise HTTPException(400, f"Narratarr can't find a folder at {path or 'that path'}.")
    return folder.resolve()


@app.post("/api/import/scan")
async def import_scan(body: ScanFolder):
    folder = _folder(body.folder)
    found = await asyncio.to_thread(importer.scan, folder)
    books = db.list_books()
    known_paths = {b["path"] for b in books if b["path"]} | {importer.path_of(b["source_url"]).as_posix()
                                                               for b in books if b["source"] == "local"}
    library = Path(config.load().library_dir).expanduser().resolve()
    for f in found:
        f["in_library"] = f["path"] in known_paths or bool(wanted.in_library("", f["title"], f["author"], books)
                                                           and f["title"] and f["author"])
        f["inside_library"] = library == Path(f["path"]) or library in Path(f["path"]).parents
    return {"folder": str(folder), "books": found}


class ImportItem(BaseModel):
    path: str
    title: str = ""
    author: str = ""


class ImportBooks(BaseModel):
    folder: str
    mode: str = "copy"  # "copy", "move" or "inplace"
    books: list[ImportItem]


@app.post("/api/import")
async def import_books(body: ImportBooks):
    folder = _folder(body.folder)
    if body.mode not in ("copy", "move", "inplace"):
        raise HTTPException(400, "Unknown import mode.")
    found = {f["path"]: f for f in await asyncio.to_thread(importer.scan, folder)}
    added = []
    for item in body.books:
        f = found.get(item.path)
        if not f:
            continue  # only books that are really in that folder
        f = {**f, "title": item.title.strip() or f["title"], "author": item.author.strip() or f["author"]}
        mode = "copy" if body.mode == "inplace" and f["single_file"] else body.mode
        if db.get_by_url(importer.url_for(Path(f["path"]))) or db.get_by_path(f["path"]):
            continue
        added.append(db.insert(importer.as_book(f, mode, "completed" if mode == "inplace" else "queued")))
    if any(b["status"] == "queued" for b in added):
        _worker().poke()
    return {"added": len(added)}


@app.post("/api/import/check-folder")
async def import_check_folder():
    """Check the watched import folder right away instead of waiting a minute."""
    return {"queued": await _worker().watch_folder()}


# ---- authors and series ----------------------------------------------------------


@app.get("/api/authors/search")
async def author_search(q: str):
    if len(q.strip()) < 2:
        return []
    try:
        return await audible.search_authors(q.strip())
    except audible.AudibleError as e:
        raise HTTPException(502, str(e))


@app.get("/api/follows")
async def follows():
    books = db.list_books()
    return [{**f, "known": len(f["known"]), **wanted.follow_counts(f, books)} for f in db.list_follows()]


class Follow(BaseModel):
    kind: str
    asin: str
    name: str = ""
    add_existing: bool = False


@app.post("/api/follows")
async def add_follow(body: Follow):
    if body.kind not in ("author", "series"):
        raise HTTPException(400, "You can follow an author or a series.")
    try:
        f, added = await wanted.follow(body.kind, body.asin, body.name, body.add_existing)
    except audible.AudibleError as e:
        raise HTTPException(502, str(e))
    if added:
        _background(_auto_search_now())
    return {**f, "known": len(f["known"]), "added": added}


async def _auto_search_now() -> None:
    try:
        if await wanted.auto_search():
            _worker().poke()
    except Exception:
        pass


def _follow_or_404(follow_id: int) -> dict:
    f = db.get_follow(follow_id)
    if not f:
        raise HTTPException(404, "You're not following that any more.")
    return f


@app.get("/api/follows/{follow_id}")
async def follow_detail(follow_id: int):
    f = _follow_or_404(follow_id)
    try:
        if f["kind"] == "author":
            info = await audible.author(f["asin"])
            found = await audible.author_books(f["asin"], f["name"])
        else:
            info = {"description": ""}
            _, found = await audible.series_books(f["asin"])
    except audible.AudibleError as e:
        raise HTTPException(502, str(e))
    books = db.list_books()
    for b in found:
        lib = wanted.in_library(b["asin"], b["title"], b["author"], books)
        b["library"] = {"id": lib["id"], "status": lib["status"]} if lib else None
    return {**f, "known": len(f["known"]), "description": info.get("description", ""), "books": found,
            **wanted.follow_counts(f, books)}


@app.post("/api/follows/{follow_id}/want-all")
async def want_all(follow_id: int):
    f = _follow_or_404(follow_id)
    detail = await follow_detail(follow_id)
    added = sum(1 for b in detail["books"] if not b["library"] and wanted.add_wanted(b))
    if added:
        _background(_auto_search_now())
    return {"added": added, "name": f["name"]}


@app.delete("/api/follows/{follow_id}")
async def unfollow(follow_id: int, remove_wanted: bool = False):
    f = _follow_or_404(follow_id)
    if remove_wanted:
        books = db.list_books()
        mine = {b["id"] for b in books if b["status"] == "wanted" and b["source"] == "audible"}
        for b in books:
            if b["id"] in mine and wanted.follow_counts(f, [b])["wanted"]:
                db.delete(b["id"])
    db.delete_follow(follow_id)
    return {"ok": True}


# ---- updates ------------------------------------------------------------------------


@app.get("/api/updates")
async def update_status(force: bool = False):
    return await updates.status(force)


@app.get("/api/changelog")
async def get_changelog():
    return {"markdown": updates.changelog(), "current": updates.current_version()}


class Update(BaseModel):
    version: str


@app.post("/api/updates/apply")
async def apply_update(body: Update):
    if not re.fullmatch(r"\d+\.\d+\.\d+", body.version):
        raise HTTPException(400, "That doesn't look like a version number.")
    try:
        return await updates.apply(body.version)
    except RuntimeError as e:
        raise HTTPException(400, str(e))


# ---- settings -------------------------------------------------------------


# What listeners see of the settings: enough for the app to know what's set up, no addresses or keys.
LISTENER_SETTINGS = (
    "rd_configured", "brave_configured", "prowlarr_configured", "usenet_configured", "torrent_configured",
    "usenet_client", "torrent_client", "smart_search", "search_provider", "audible_region", "check_updates",
    "auto_search", "auto_search_hours",
)


@app.get("/api/settings")
async def get_settings(request: Request):
    public = config.load().public()
    if request.state.user["role"] == "admin":
        return public
    return {k: v for k, v in public.items() if k in LISTENER_SETTINGS}


@app.put("/api/settings")
async def put_settings(changes: dict):
    try:
        return config.update(changes).public()
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/settings/test-brave")
async def test_brave():
    s = config.load()
    try:
        res = await brave.search("dune", s.brave_api_key, s.abb_base_url)
    except brave.BraveError as e:
        raise HTTPException(400, str(e))
    return {"results": len(res["items"])}


@app.post("/api/settings/test-prowlarr")
async def test_prowlarr():
    s = config.load()
    try:
        return await Prowlarr(s.prowlarr_url, s.prowlarr_api_key).status()
    except ProwlarrError as e:
        raise HTTPException(400, str(e))


@app.post("/api/settings/test-client/{name}")
async def test_client(name: str):
    s = config.load()
    try:
        if name in downloaders.USENET_CLIENTS:
            factory = downloaders.usenet_client
        elif name in ("qbittorrent", "transmission", "deluge"):
            factory = downloaders.torrent_client
        else:
            raise HTTPException(404, "Unknown download client.")
        async with factory(s, name=name) as client:
            return {"version": await client.version(), "label": client.label}
    except ClientError as e:
        raise HTTPException(400, str(e))


@app.post("/api/settings/test-rd")
async def test_rd():
    try:
        async with RealDebrid(config.load().rd_api_key) as rd:
            user = await rd.user()
    except RealDebridError as e:
        raise HTTPException(400, str(e))
    except httpx.HTTPError:
        raise HTTPException(502, "Couldn't reach Real-Debrid.")
    return {
        "username": user.get("username"),
        "type": user.get("type"),
        "expiration": user.get("expiration"),
        "points": user.get("points"),
    }


# ---- Audiobookshelf -----------------------------------------------------------


class FindServer(BaseModel):
    address: str = ""  # empty: look on this machine, in Docker, then across the home network
    sweep: bool = True


@app.post("/api/audiobookshelf/find")
async def abs_find(body: FindServer):
    try:
        if body.address.strip():
            servers = await audiobookshelf.find_at(body.address)
        else:
            servers = await audiobookshelf.find_nearby(body.sweep)
    except AudiobookshelfError as e:
        raise HTTPException(400, str(e))
    for server in servers:
        server["public"] = await asyncio.to_thread(audiobookshelf.is_public, server["url"])
        server["insecure"] = server["public"] and server["url"].startswith("http://")
    return {"servers": servers}


class SignIn(BaseModel):
    url: str
    username: str
    password: str
    allow_insecure: bool = False


@app.post("/api/audiobookshelf/sign-in")
async def abs_sign_in(body: SignIn):
    """Trade the user's ABS login for an API key, once. The password is used for this request only."""
    url = body.url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "The Audiobookshelf address must start with http:// or https://")
    if not body.allow_insecure and await asyncio.to_thread(audiobookshelf.insecure, url):
        raise HTTPException(400, "This address is on the internet without https, so your password could be read on the way.")
    try:
        async with Audiobookshelf(url) as server:
            token = await server.sign_in(body.username, body.password)
        async with Audiobookshelf(url, token) as server:
            libraries = await server.libraries()
    except AudiobookshelfError as e:
        raise HTTPException(400, str(e))
    s = config.load()
    library_id = s.audiobookshelf_library_id
    if url != s.audiobookshelf_url or not any(lib["id"] == library_id for lib in libraries):
        library_id = audiobookshelf.pick_library(libraries, str(Path(s.library_dir).expanduser()))
    s = config.update({"audiobookshelf_url": url, "audiobookshelf_token": token, "audiobookshelf_library_id": library_id})
    return {"libraries": libraries, "library_id": library_id, "settings": s.public()}


@app.get("/api/audiobookshelf/libraries")
async def abs_libraries():
    s = config.load()
    try:
        async with Audiobookshelf(s.audiobookshelf_url, s.audiobookshelf_token) as server:
            return {"libraries": await server.libraries(), "library_id": s.audiobookshelf_library_id}
    except AudiobookshelfError as e:
        raise HTTPException(400, str(e))


@app.post("/api/settings/test-audiobookshelf")
async def test_audiobookshelf():
    s = config.load()
    try:
        async with Audiobookshelf(s.audiobookshelf_url, s.audiobookshelf_token) as server:
            status = await server.status()
            libraries = await server.libraries()
    except AudiobookshelfError as e:
        raise HTTPException(400, str(e))
    library = next((lib for lib in libraries if lib["id"] == s.audiobookshelf_library_id), None)
    if not library:
        raise HTTPException(400, "Connected, but no library is chosen. Pick the one Narratarr's books go into.")
    return {"version": status["version"], "library": library["name"]}


@app.get("/api/audiobookshelf/guide")
async def abs_guide():
    """What the setup guide needs to fill in this machine's own paths and guess the setup."""
    s = config.load()
    info = {
        "install": updates.install_kind(), "os": platform.system().lower(),
        "library_dir": str(Path(s.library_dir).expanduser()),
        "connected": bool(s.audiobookshelf_url and s.audiobookshelf_token), "url": s.audiobookshelf_url,
        "where": "", "nearby": False, "libraries": [], "library_id": s.audiobookshelf_library_id, "error": "",
    }
    if s.audiobookshelf_url:
        info["where"] = await asyncio.to_thread(audiobookshelf.where, s.audiobookshelf_url)
    if info["where"] not in ("here", "docker"):
        # Reached through a public or LAN address, but also answering on this machine: the same server.
        info["nearby"] = bool(await audiobookshelf.find_nearby(sweep=False))
    if info["connected"]:
        try:
            async with Audiobookshelf(s.audiobookshelf_url, s.audiobookshelf_token) as server:
                info["libraries"] = await server.libraries()
        except AudiobookshelfError as e:
            info["error"] = str(e)
    return info


class Check(BaseModel):
    scan: bool = True


@app.post("/api/audiobookshelf/check")
async def abs_check(body: Check):
    """Whether Audiobookshelf can see the books Narratarr has downloaded."""
    s = config.load()
    if not (s.audiobookshelf_url and s.audiobookshelf_token and s.audiobookshelf_library_id):
        raise HTTPException(400, "Connect Audiobookshelf and pick a library first.")
    books = [b for b in db.list_books() if b["status"] == "completed" and b["path"]]
    library_dir = str(Path(s.library_dir).expanduser().resolve())
    try:
        async with Audiobookshelf(s.audiobookshelf_url, s.audiobookshelf_token) as server:
            library = next((lib for lib in await server.libraries() if lib["id"] == s.audiobookshelf_library_id), None)
            if not library:
                raise AudiobookshelfError("Audiobookshelf doesn't know that library any more. Pick it again.")
            if body.scan and books:
                await server.scan(library["id"])
            # A scan runs in the background, so look a few times while it finds things.
            for attempt in range(6 if body.scan and books else 1):
                if attempt:
                    await asyncio.sleep(1.5)
                result = audiobookshelf.compare(books, library_dir, await server.items(library["id"]))
                if result["seen"] == result["books"]:
                    break
    except AudiobookshelfError as e:
        raise HTTPException(400, str(e))
    return {**result, "library": library["name"], "folders": library["folders"]}


# ---- accounts ---------------------------------------------------------------------


class Credentials(BaseModel):
    username: str
    password: str
    code: str = ""  # the setup code, when creating the first admin from outside the home network


def _signed_in(request: Request, user: dict) -> JSONResponse:
    resp = JSONResponse({"user": user})
    resp.set_cookie(value=users.start_session(user["id"]), max_age=users.SESSION_SECONDS, **auth.cookie_args(request))
    return resp


@app.get("/api/auth/status")
async def auth_status(request: Request):
    setup = users.count() == 0
    return {
        "setup": setup,
        "setup_code_needed": setup and not auth.is_local(request),
        "user": None if setup else auth.current_user(request),
    }


@app.post("/api/auth/setup")
async def auth_setup(body: Credentials, request: Request):
    """Create the first admin. Only possible while there are no accounts at all."""
    if users.count():
        raise HTTPException(409, "Narratarr already has an admin. Sign in instead.")
    if auth.too_many_tries(request):
        raise HTTPException(429, "Too many tries. Wait ten minutes and try again.")
    if not auth.is_local(request) and not hmac.compare_digest(body.code.strip(), auth.setup_code()):
        auth.failed_try(request)
        raise HTTPException(403, "That setup code isn't right. It's in Narratarr's log (docker logs narratarr).")
    try:
        user = users.create(body.username, body.password, "admin")
    except users.UserError as e:
        raise HTTPException(400, str(e))
    return _signed_in(request, user)


@app.post("/api/auth/login")
async def auth_login(body: Credentials, request: Request):
    ticket = auth.start_try(request, body.username)
    if ticket is None:
        raise HTTPException(429, "Too many tries. Wait ten minutes and try again.")
    user = await asyncio.to_thread(users.authenticate, body.username, body.password)
    if not user:
        raise HTTPException(401, "That username and password don't match.")
    auth.forgive(ticket)
    return _signed_in(request, user)


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    users.end_session(request.cookies.get(auth.COOKIE, ""))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


def _signed_in_with_password(request: Request) -> dict:
    """Managing apps needs a real sign-in: the home-network shortcut would let anyone on the
    network see and sign out the owner's apps."""
    me = users.session_user(request.cookies.get(auth.COOKIE, ""))
    if me is None:
        raise HTTPException(403, "Sign in with your password to manage apps.")
    return me


@app.get("/api/auth/apps")
async def my_apps(request: Request):
    """Apps signed in to this account through the API (see tokens.py)."""
    return tokens.grants(_signed_in_with_password(request)["id"])


@app.delete("/api/auth/apps/{grant_id}")
async def revoke_app(grant_id: int, request: Request):
    if not tokens.revoke(grant_id, _signed_in_with_password(request)["id"]):
        raise HTTPException(404, "That app isn't signed in to your account.")
    return {"ok": True}


class NewPassword(BaseModel):
    current: str
    password: str


@app.put("/api/auth/password")
async def change_own_password(body: NewPassword, request: Request):
    me = request.state.user
    ticket = auth.start_try(request, me["username"])  # a stolen session mustn't mean unlimited guesses
    if ticket is None:
        raise HTTPException(429, "Too many tries. Wait ten minutes and try again.")
    if not await asyncio.to_thread(users.authenticate, me["username"], body.current):
        raise HTTPException(400, "Your current password isn't right.")
    auth.forgive(ticket)
    try:
        users.set_password(me["id"], body.password)  # signs out everywhere, so sign this browser back in
    except users.UserError as e:
        raise HTTPException(400, str(e))
    return _signed_in(request, me)


class NewUser(BaseModel):
    username: str
    password: str
    role: str = "listener"


class UserChange(BaseModel):
    role: str | None = None
    password: str | None = None


@app.get("/api/users")
async def list_users():
    return users.all_users()


@app.post("/api/users")
async def add_user(body: NewUser):
    try:
        return users.create(body.username, body.password, body.role)
    except users.UserError as e:
        raise HTTPException(400, str(e))


@app.patch("/api/users/{user_id}")
async def change_user(user_id: int, body: UserChange):
    if not users.get(user_id):
        raise HTTPException(404, "No such user.")
    try:
        if body.role:
            users.set_role(user_id, body.role)
        if body.password:
            users.set_password(user_id, body.password)
    except users.UserError as e:
        raise HTTPException(400, str(e))
    return users.get(user_id)


@app.delete("/api/users/{user_id}")
async def remove_user(user_id: int):
    if not users.get(user_id):
        raise HTTPException(404, "No such user.")
    try:
        users.delete(user_id)
    except users.UserError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}
