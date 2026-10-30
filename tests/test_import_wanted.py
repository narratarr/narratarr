"""Importing books from disk, Wanted books and following authors."""

import os
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from narratarr import api, audible, config, db, importer, wanted, worker
from narratarr.sources import prowlarr
from narratarr.worker import Worker


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(worker, "POLL_SECONDS", 0)
    prowlarr._releases.clear()
    prowlarr._search_cache.clear()


def make_tree(root: Path) -> None:
    files = [
        "Frank Herbert/Dune/01.mp3",
        "Frank Herbert/Dune/02.mp3",
        "Andy Weir - Project Hail Mary/CD1/01.mp3",
        "Andy Weir - Project Hail Mary/CD2/01.mp3",
        "The Hobbit.m4b",
        ".hidden/Secret/01.mp3",
        "Notes/readme.txt",
    ]
    for f in files:
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_bytes(b"audio" * 10)
    (root / "Frank Herbert/Dune/desc.txt").write_text("Arrakis.")


def test_id_like_tags_lose_to_folder_names():
    assert importer._readable("the_raven") == "" and importer._readable("poe") == ""
    assert importer._readable("The Raven") == "The Raven" and importer._readable("Dune") == "Dune"


def test_find_books_and_guess_names(tmp_path):
    make_tree(tmp_path)
    found = {Path(b["path"]).name: b for b in importer.scan(tmp_path)}
    assert set(found) == {"Dune", "Andy Weir - Project Hail Mary", "The Hobbit.m4b"}
    assert (found["Dune"]["title"], found["Dune"]["author"], found["Dune"]["files"]) == ("Dune", "Frank Herbert", 2)
    assert found["Dune"]["description"] == "Arrakis."
    phm = found["Andy Weir - Project Hail Mary"]
    assert (phm["title"], phm["author"], phm["files"]) == ("Project Hail Mary", "Andy Weir", 2)
    assert found["The Hobbit.m4b"]["single_file"] and found["The Hobbit.m4b"]["title"] == "The Hobbit"


@pytest.fixture
def client():
    with TestClient(api.app) as c:
        yield c


def test_import_in_place_copy_and_move(client, isolated_data):
    src = isolated_data / "old-library"
    make_tree(src)
    scan = client.post("/api/import/scan", json={"folder": str(src)}).json()
    paths = {Path(b["path"]).name: b["path"] for b in scan["books"]}
    assert len(paths) == 3 and not any(b["in_library"] for b in scan["books"])

    # In place: nothing moves, the book is ready right away.
    r = client.post("/api/import", json={"folder": str(src), "mode": "inplace", "books": [{"path": paths["Dune"]}]})
    assert r.json()["added"] == 1
    dune = next(b for b in db.list_books() if b["title"] == "Dune")
    assert dune["status"] == "completed" and dune["path"] == paths["Dune"]
    assert client.get(f"/api/library/{dune['id']}/media").status_code in (200, 404)  # fake audio, but reachable

    # Scanning again knows it's there.
    again = client.post("/api/import/scan", json={"folder": str(src)}).json()["books"]
    assert next(b for b in again if b["path"] == paths["Dune"])["in_library"]

    # Copy and move go through the worker.
    client.post("/api/import", json={"folder": str(src), "mode": "copy", "books": [
        {"path": paths["Andy Weir - Project Hail Mary"]}]})
    client.post("/api/import", json={"folder": str(src), "mode": "inplace", "books": [
        {"path": paths["The Hobbit.m4b"], "author": "J.R.R. Tolkien"}]})  # a loose file can't stay in place: copied
    for _ in range(100):  # the app's own worker picks them up
        if all(b["status"] == "completed" for b in db.list_books()):
            break
        time.sleep(0.1)
    lib = Path(config.load().library_dir)
    assert (lib / "Andy Weir" / "Project Hail Mary" / "CD2" / "01.mp3").exists()
    assert (src / "Andy Weir - Project Hail Mary").exists()  # copied, not moved
    assert (lib / "J.R.R. Tolkien" / "The Hobbit" / "The Hobbit.m4b").exists()

    assert client.post("/api/import/scan", json={"folder": "/no/such/folder"}).status_code == 400


async def test_watch_folder_moves_finished_books(isolated_data):
    inbox = isolated_data / "inbox"
    (inbox / "Frank Herbert - Dune").mkdir(parents=True)
    (inbox / "Frank Herbert - Dune" / "Dune.m4b").write_bytes(b"audio")
    config.update({"import_folder": str(inbox)})
    w = Worker()

    assert await w.watch_folder() == 0  # just arrived: might still be copying
    old = time.time() - 600
    for p in [inbox / "Frank Herbert - Dune", inbox / "Frank Herbert - Dune" / "Dune.m4b"]:
        os.utime(p, (old, old))
    assert await w.watch_folder() == 1
    assert await w.watch_folder() == 0  # not twice

    book = db.list_books()[0]
    await w.process(book["id"])
    done = db.get(book["id"])
    assert done["status"] == "completed", done["error"]
    assert not (inbox / "Frank Herbert - Dune").exists()
    assert (Path(done["path"]) / "Dune.m4b").exists()
    assert "#imported-" in done["source_url"]


# ---- wanted ------------------------------------------------------------------------

PROWLARR = "http://prowlarr:9696"
AUDIBLE_BOOK = {
    "asin": "B002V0QCYU", "url": "https://www.audible.com/pd/B002V0QCYU", "title": "The Final Empire",
    "subtitle": "Mistborn, Book 1", "author": "Brandon Sanderson", "author_asins": ["B001IGFHW6"],
    "narrators": ["Michael Kramer"], "cover_url": "https://img.example/fe.jpg", "description": "A heist.",
    "released": "2008-12-28", "length": "24 hours and 39 minutes", "language": "English",
    "series": [{"asin": "B006K1P698", "title": "The Mistborn Saga", "sequence": "1"}],
    "kind": "SinglePartBook", "abridged": False,
}


def test_match_scores():
    book = {"title": "The Final Empire", "author": "Brandon Sanderson"}
    assert wanted.match(book, {"release": "Brandon Sanderson - Mistborn 01 - The Final Empire (2008) [M4B]"}) >= 90
    assert wanted.match(book, {"release": "Brandon Sanderson - The Well of Ascension [M4B]"}) < 60
    assert wanted.match(book, {"release": "The Final Empire by Some Other Person"}) < wanted.AUTO_MATCH
    assert wanted.match(book, {"title": "The Final Empire", "author": "Brandon Sanderson"}) == 100


def fake_prowlarr(releases):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "prowlarr":
            return httpx.Response(200, json=releases)
        if "archive.org" in request.url.host:
            return httpx.Response(200, json={"response": {"numFound": 0, "docs": []}})
        return httpx.Response(404)
    return handler


@pytest.fixture
def prowlarr_net(monkeypatch):
    """Route Prowlarr and archive.org searches to fakes."""
    releases = []
    transport = httpx.MockTransport(fake_prowlarr(releases))
    real = httpx.AsyncClient

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real(*a, **{**kw, "transport": transport}))
    config.update({"prowlarr_url": PROWLARR, "prowlarr_api_key": "K", "sabnzbd_url": "http://sab", "sabnzbd_api_key": "S"})
    return releases


async def test_auto_search_grabs_the_right_release(prowlarr_net):
    prowlarr_net += [
        {"guid": "https://n.example/1", "title": "Brandon Sanderson - The Well of Ascension [M4B]", "protocol": "usenet",
         "indexer": "N", "downloadUrl": f"{PROWLARR}/1/download?apikey=K&link=1", "size": 1},
        {"guid": "https://n.example/2", "title": "Brandon.Sanderson-Mistborn.01.The.Final.Empire.2008.M4B",
         "protocol": "usenet", "indexer": "N", "downloadUrl": f"{PROWLARR}/1/download?apikey=K&link=2", "size": 1},
    ]
    book = wanted.add_wanted(AUDIBLE_BOOK)
    assert wanted.add_wanted(AUDIBLE_BOOK) is None  # no duplicates

    assert await wanted.auto_search() == 1
    got = db.get(book["id"])
    assert got["status"] == "queued" and got["source"] == "prowlarr" and got["protocol"] == "usenet"
    assert "link=2" in got["download_url"] and "apikey" not in got["download_url"]
    assert got["title"] == "The Final Empire" and got["cover_url"] == "https://img.example/fe.jpg"  # our metadata kept
    assert got["release_url"] == "https://n.example/2"


async def test_auto_search_waits_politely(prowlarr_net):
    book = wanted.add_wanted(AUDIBLE_BOOK)
    assert await wanted.auto_search() == 0
    after = db.get(book["id"])
    assert after["status"] == "wanted" and after["search_note"] == "Nothing found yet."
    assert wanted.due_for_search() == []  # not searched again until the interval passes

    future = {**AUDIBLE_BOOK, "asin": "B0FUTURE01", "url": "https://www.audible.com/pd/B0FUTURE01",
              "title": "Wind and Truth", "released": "2999-01-01"}
    wanted.add_wanted(future)
    assert all(b["title"] != "Wind and Truth" for b in wanted.due_for_search())  # not out yet


async def test_releases_need_a_client(prowlarr_net):
    config.update({"sabnzbd_api_key": ""})
    prowlarr_net.append({"guid": "https://n.example/2", "title": "Brandon Sanderson - The Final Empire [M4B]",
                         "protocol": "usenet", "indexer": "N", "downloadUrl": f"{PROWLARR}/1/download?link=2"})
    book = wanted.add_wanted(AUDIBLE_BOOK)
    assert await wanted.search_and_grab(book) is None
    assert "download clients" in db.get(book["id"])["search_note"]


async def test_follow_author_adds_only_upcoming_books(monkeypatch):
    old = AUDIBLE_BOOK
    upcoming = {**AUDIBLE_BOOK, "asin": "B0NEW00001", "url": "https://www.audible.com/pd/B0NEW00001",
                "title": "Isles of the Emberdark", "released": "2999-03-01", "series": []}
    catalog = [old, upcoming]

    async def author(asin, client=None):
        return {"asin": asin, "name": "Brandon Sanderson", "image": "https://img.example/bs.jpg", "description": ""}

    async def author_books(asin, name, client=None, pages=4):
        return list(catalog)

    monkeypatch.setattr(audible, "author", author)
    monkeypatch.setattr(audible, "author_books", author_books)

    f, added = await wanted.follow("author", "B001IGFHW6")
    assert added == 1 and [b["title"] for b in db.list_books()] == ["Isles of the Emberdark"]

    newer = {**upcoming, "asin": "B0NEW00002", "url": "https://www.audible.com/pd/B0NEW00002", "title": "Mistborn 4"}
    catalog.append(newer)
    assert await wanted.refresh(db.get_follow(f["id"])) == 1
    assert await wanted.refresh(db.get_follow(f["id"])) == 0
    counts = wanted.follow_counts(db.get_follow(f["id"]), db.list_books())
    assert counts == {"in_library": 0, "wanted": 2}


async def test_audible_pages_count_from_zero():
    """Audible's first page is page 0. Asking for page 1 first skipped the best 40 matches."""
    import httpx

    seen = []

    def handler(request):
        seen.append((request.url.params.get("page"), request.url.params.get("keywords") or request.url.params.get("author")))
        return httpx.Response(200, json={"products": [], "total_results": 0})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await audible.search_books("zero-based paging check", 1, client)
        await audible.search_books("zero-based paging check", 2, client)
        await audible.author_books("B000AQ0842", "Zero Based Author", client, pages=1)
    assert seen == [("0", "zero-based paging check"), ("1", "zero-based paging check"), ("0", "Zero Based Author")]
