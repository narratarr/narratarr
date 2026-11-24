"""Guards against losing files, leaking data, and growing without bound."""

import asyncio
import json
import os
import stat
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from narratarr import api, config, db, importer, search, worker
from narratarr.cache import Cache
from narratarr.qbittorrent import QBittorrent
from narratarr.transmission import Transmission
from narratarr.worker import Worker, readable_error, tmp_dir, unique_destination


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(worker, "POLL_SECONDS", 0)


@pytest.fixture
def client():
    with TestClient(api.app) as c:
        c.app.state.worker.poke = lambda: None
        yield c


# ---- files are never lost -------------------------------------------------------------


async def test_interrupted_move_import_resumes_without_losing_files(isolated_data):
    src = isolated_data / "inbox" / "Frank Herbert - Dune"
    src.mkdir(parents=True)
    (src / "01.mp3").write_bytes(b"one")
    (src / "02.mp3").write_bytes(b"two")
    book = db.insert(importer.as_book(importer.read_meta(src, src.parent), "move", "queued"))
    s = config.load()

    # A previous attempt got half way: one file is already in the temp folder.
    tmp = tmp_dir(book, s)
    tmp.mkdir(parents=True)
    (src / "01.mp3").rename(tmp / "01.mp3")

    await Worker().process(book["id"])

    done = db.get(book["id"])
    assert done["status"] == "completed", done["error"]
    dest = Path(done["path"])
    assert (dest / "01.mp3").read_bytes() == b"one" and (dest / "02.mp3").read_bytes() == b"two"
    assert not src.exists()


async def test_move_import_finishes_even_if_source_is_gone(isolated_data):
    src = isolated_data / "inbox" / "Frank Herbert - Dune"
    src.mkdir(parents=True)
    (src / "Dune.m4b").write_bytes(b"audio")
    book = db.insert(importer.as_book(importer.read_meta(src, src.parent), "move", "queued"))
    tmp = tmp_dir(book, config.load())
    tmp.mkdir(parents=True)
    (src / "Dune.m4b").rename(tmp / "Dune.m4b")  # everything moved, then a crash before organizing
    src.rmdir()

    await Worker().process(book["id"])

    done = db.get(book["id"])
    assert (
        done["status"] == "completed" and (Path(done["path"]) / "Dune.m4b").read_bytes() == b"audio"
    )


def test_two_books_never_share_a_folder(isolated_data):
    s = config.load()
    first = db.insert(
        {
            "source": "local",
            "source_url": "file:///a",
            "title": "Dune",
            "author": "Frank Herbert",
            "status": "completed",
            "path": str(Path(s.library_dir).resolve() / "Frank Herbert" / "Dune"),
        }
    )
    second = db.insert(
        {
            "source": "local",
            "source_url": "file:///b",
            "title": "Dune",
            "author": "Frank Herbert",
            "status": "queued",
        }
    )
    assert unique_destination(second, s).name == "Dune (2)"
    assert unique_destination(first, s).name == "Dune"  # a book keeps its own folder

    s.folder_template = "{author}"  # would put every book by an author in one folder
    assert unique_destination(second, s).name == "Frank Herbert (2)"


def test_remove_never_deletes_a_folder_another_book_lives_in(client, isolated_data):
    root = Path(config.load().library_dir).resolve()
    folder = root / "Frank Herbert" / "Dune"
    folder.mkdir(parents=True)
    (folder / "Dune.m4b").write_bytes(b"audio")
    a = db.insert(
        {
            "source": "local",
            "source_url": "file:///a",
            "title": "Dune",
            "status": "completed",
            "path": str(folder),
        }
    )
    b = db.insert(
        {
            "source": "local",
            "source_url": "file:///b",
            "title": "Dune",
            "status": "completed",
            "path": str(folder),
        }
    )
    parent = db.insert(
        {
            "source": "local",
            "source_url": "file:///c",
            "title": "Herbert",
            "status": "completed",
            "path": str(folder.parent),
        }
    )

    client.delete(f"/api/library/{parent['id']}?delete_files=true")
    assert folder.exists()  # the author folder holds other books
    client.delete(f"/api/library/{a['id']}?delete_files=true")
    assert folder.exists()  # still b's folder
    client.delete(f"/api/library/{b['id']}?delete_files=true")
    assert not folder.exists()  # nobody else's now


def test_remove_never_deletes_outside_the_library(client, isolated_data):
    outside = isolated_data / "elsewhere" / "Dune"
    outside.mkdir(parents=True)
    (outside / "Dune.m4b").write_bytes(b"audio")
    book = db.insert(
        {
            "source": "local",
            "source_url": "file:///x",
            "title": "Dune",
            "status": "completed",
            "path": str(outside),
            "import_mode": "inplace",
        }
    )
    client.delete(f"/api/library/{book['id']}?delete_files=true")
    assert (outside / "Dune.m4b").exists()


async def test_watch_folder_refuses_the_library_itself(isolated_data):
    lib = Path(config.load().library_dir)
    (lib / "Frank Herbert - Dune").mkdir(parents=True)
    (lib / "Frank Herbert - Dune" / "Dune.m4b").write_bytes(b"audio")
    old = time.time() - 600
    for p in (lib / "Frank Herbert - Dune", lib / "Frank Herbert - Dune" / "Dune.m4b"):
        os.utime(p, (old, old))
    for folder in (lib, lib / "sub", lib.parent):
        with pytest.raises(ValueError):  # refused at save time
            config.update({"import_folder": str(folder)})
        config.load().import_folder = str(folder)  # and ignored if it got in anyway
        (lib / "sub").mkdir(exist_ok=True)
        assert await Worker().watch_folder() == 0


# ---- a torrent the user already had is never deleted ---------------------------------------


async def test_qbittorrent_only_deletes_files_it_added():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/torrents/info":
            tag = "narratarr-7" if "ours" in request.url.query.decode() else "mine"
            return httpx.Response(200, json=[{"hash": "x", "tags": tag}])
        if request.url.path == "/api/v2/torrents/delete":
            calls.append(request.content.decode())
            return httpx.Response(200)
        return httpx.Response(404)

    async with QBittorrent("http://q:8080", transport=httpx.MockTransport(handler)) as q:
        await q.delete("ours", files=True)
        await q.delete("theirs", files=True)
    assert "deleteFiles=true" in calls[0] and "deleteFiles=false" in calls[1]


async def test_transmission_only_deletes_files_it_added():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "torrent-get":
            labels = ["narratarr-7"] if body["arguments"]["ids"] == ["ours"] else ["seedbox"]
            return httpx.Response(
                200, json={"result": "success", "arguments": {"torrents": [{"labels": labels}]}}
            )
        calls.append(body["arguments"])
        return httpx.Response(200, json={"result": "success", "arguments": {}})

    async with Transmission("http://t:9091", transport=httpx.MockTransport(handler)) as t:
        await t.delete("ours", files=True)
        await t.delete("theirs", files=True)
    assert calls[0]["delete-local-data"] is True and calls[1]["delete-local-data"] is False


# ---- nothing leaks --------------------------------------------------------------------------


def test_cache_is_bounded_and_expires():
    c = Cache(seconds=0.05, size=2)
    for i in range(3):
        c.set(i, i)
    assert len(c) == 2 and c.get(0) is None and c.get(2) == 2
    time.sleep(0.06)
    assert c.get(2) is None and len(c) == 1


async def test_release_names_stay_home_when_smart_search_is_off(monkeypatch):
    async def boom(*a, **kw):
        raise AssertionError("contacted Apple")

    monkeypatch.setattr(search, "suggestions", boom)
    config.update({"smart_search": False})
    assert await search.identify("Brandon Sanderson - The Final Empire") is None


def test_passwords_are_fully_masked_and_settings_file_is_private(isolated_data):
    pub = config.update({"nzbget_password": "hunter2xyz", "rd_api_key": "ABCDEFGH1234"}).public()
    assert "xyz" not in pub["nzbget_password"] and pub["nzbget_password"].startswith("•")
    assert pub["rd_api_key"].endswith("1234")  # keys keep a tail so you can tell them apart
    assert stat.S_IMODE(config.SETTINGS_FILE.stat().st_mode) == 0o600
    assert not config.SETTINGS_FILE.with_suffix(".tmp").exists()


def test_error_messages_carry_no_urls():
    req = httpx.Request("GET", "https://dl.example/d/SECRETTOKEN/file.m4b")
    err = httpx.HTTPStatusError("boom", request=req, response=httpx.Response(503, request=req))
    msg = readable_error(err)
    assert "SECRETTOKEN" not in msg and "503" in msg
    assert "SECRET" not in readable_error(httpx.ConnectError("failed", request=req))


# ---- other websites can't drive the app --------------------------------------------------------


def test_cross_site_requests_are_refused(client):
    book = db.insert(
        {
            "source": "librivox",
            "source_url": "https://archive.org/details/x",
            "title": "X",
            "status": "wanted",
        }
    )
    url = f"/api/library/{book['id']}/download"
    assert client.post(url, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post(url, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert (
        client.post(
            url, headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}
        ).status_code
        == 200
    )
    assert client.post(url).status_code == 200  # curl and scripts send no Origin
    assert (
        client.get("/api/library", headers={"Origin": "https://evil.example"}).status_code == 200
    )  # reads are fine


# ---- hostnames, proxies and costly reads --------------------------------------------------------


def test_only_local_hostnames_are_answered(client, monkeypatch):
    ok = [
        "localhost:8484",
        "127.0.0.1:8484",
        "[::1]:8484",
        "192.168.1.20:8484",
        "mymac",
        "nas.local",
        "mac.tail1234.ts.net",
    ]
    for host in ok:
        assert client.get("/api/library", headers={"Host": host}).status_code == 200, host
    assert (
        client.get("/api/library", headers={"Host": "evil.example"}).status_code == 421
    )  # DNS rebinding
    assert (
        client.get("/api/health", headers={"Host": "evil.example"}).status_code == 200
    )  # health checks still work
    monkeypatch.setenv("NARRATARR_HOSTS", "books.example.com, *.home.example")
    assert client.get("/api/library", headers={"Host": "books.example.com"}).status_code == 200
    assert client.get("/api/library", headers={"Host": "narratarr.home.example"}).status_code == 200
    assert client.get("/api/library", headers={"Host": "evil.example"}).status_code == 421


def test_reverse_proxy_users_are_not_locked_out(client):
    book = db.insert(
        {
            "source": "librivox",
            "source_url": "https://archive.org/details/y",
            "title": "Y",
            "status": "wanted",
        }
    )
    # nginx forwards Host: 127.0.0.1 while the browser's Origin is the public name: Sec-Fetch-Site decides.
    r = client.post(
        f"/api/library/{book['id']}/download",
        headers={
            "Host": "127.0.0.1:8484",
            "Origin": "https://books.example.com",
            "Sec-Fetch-Site": "same-origin",
        },
    )
    assert r.status_code == 200
    r = client.post(
        f"/api/library/{book['id']}/download",
        headers={
            "Host": "127.0.0.1:8484",
            "Origin": "https://books.example.com",
            "X-Forwarded-Host": "books.example.com",
        },
    )
    assert r.status_code == 200


def test_other_sites_cannot_spend_search_quota(client):
    hdrs = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Dest": "image"}
    assert client.get("/api/discover?q=dune&source=audiobookbay", headers=hdrs).status_code == 403
    assert client.get("/api/suggest?q=dune", headers=hdrs).status_code == 403
    assert client.get("/api/library", headers=hdrs).status_code == 200  # plain reads still fine


# ---- secrets follow their service ----------------------------------------------------------------


def test_repointing_a_service_drops_its_secret(isolated_data):
    config.update({"prowlarr_url": "http://prowlarr:9696", "prowlarr_api_key": "REALKEY123"})
    assert (
        config.update({"prowlarr_url": "http://prowlarr:9696/"}).prowlarr_api_key == "REALKEY123"
    )  # same host
    assert (
        config.update({"prowlarr_url": "http://attacker:9"}).prowlarr_api_key == ""
    )  # new host: key gone
    s = config.update({"prowlarr_url": "http://prowlarr2:9696", "prowlarr_api_key": "NEWKEY456"})
    assert s.prowlarr_api_key == "NEWKEY456"  # new host with a freshly typed key is fine
    config.update({"nzbget_url": "http://nzbget:6789", "nzbget_password": "pw"})
    masked = config.load().public()["nzbget_password"]
    assert (
        config.update(
            {"nzbget_url": "http://other:6789", "nzbget_password": masked}
        ).nzbget_password
        == ""
    )
    with pytest.raises(ValueError):
        config.update({"sabnzbd_url": "ftp://sab:8080"})


async def test_stuck_client_download_gives_up_eventually(isolated_data, monkeypatch):
    from narratarr.downloaders import Job

    class Stuck:
        label = "qBittorrent"

        async def job(self, job_id):
            return Job("downloading", 10, 0, "Waiting for peers…")

    monkeypatch.setattr(worker, "CLIENT_TIMEOUT_SECONDS", 0)
    with pytest.raises(worker.ClientError, match="in a week"):
        await Worker()._wait_for_client(Stuck(), 1, "x", config.load())


def test_fonts_are_bundled_and_pages_send_no_referrer():
    html = Path("narratarr/static/index.html").read_text()
    assert "googleapis" not in html and 'name="referrer" content="no-referrer"' in html
    fonts = Path("narratarr/static/fonts")
    assert (fonts / "fonts.css").exists() and len(list(fonts.glob("*.woff2"))) >= 6
    assert "https://" not in (fonts / "fonts.css").read_text()


# ---- Remove keeps files that exist nowhere else ------------------------------------------------------


def test_remove_returns_a_stopped_move_import_to_its_folder(client, isolated_data):
    src = isolated_data / "inbox" / "Frank Herbert - Dune"
    src.mkdir(parents=True)
    (src / "01.mp3").write_bytes(b"one")
    book = db.insert(
        {
            **importer.as_book(importer.read_meta(src, src.parent), "move", "queued"),
            "status": "wanted",
        }
    )
    tmp = tmp_dir(book, config.load())
    tmp.mkdir(parents=True)
    (src / "01.mp3").rename(tmp / "01.mp3")  # stopped half way: the only copy is in tmp
    src.rmdir()

    out = client.delete(f"/api/library/{book['id']}").json()
    assert out["kept"] == str(src) and (src / "01.mp3").read_bytes() == b"one"
    assert not tmp.exists()


def test_remove_keeps_a_half_imported_usenet_download(client, isolated_data):
    book = db.insert(
        {
            "source": "prowlarr",
            "source_url": "https://n.example/1",
            "title": "Dune",
            "status": "failed",
            "protocol": "usenet",
            "download_client": "sabnzbd",
            "client_job_id": "nzo1",
        }
    )
    tmp = tmp_dir(book, config.load())
    tmp.mkdir(parents=True)
    (tmp / "Dune.m4b").write_bytes(b"audio")
    out = client.delete(f"/api/library/{book['id']}").json()
    kept = Path(out["kept"])
    assert kept.name.startswith("recovered-") and (kept / "Dune.m4b").read_bytes() == b"audio"


def test_remove_drops_tmp_of_a_plain_download(client, isolated_data):
    book = db.insert(
        {
            "source": "librivox",
            "source_url": "https://archive.org/details/z",
            "title": "Z",
            "status": "failed",
        }
    )
    tmp = tmp_dir(book, config.load())
    tmp.mkdir(parents=True)
    (tmp / "part.mp3").write_bytes(b"x")
    assert client.delete(f"/api/library/{book['id']}").json()["kept"] == "" and not tmp.exists()


# ---- downloads never trample what's already on disk -----------------------------------------------


async def test_download_never_overwrites_a_folder_narratarr_did_not_make(isolated_data):
    root = Path(config.load().library_dir)
    mine = root / "Frank Herbert" / "Dune"
    (mine / "CD1").mkdir(parents=True)
    (mine / "CD1" / "01.mp3").write_bytes(b"my rip")
    (mine / "desc.txt").write_text("my notes")
    src = isolated_data / "inbox" / "Frank Herbert - Dune"
    src.mkdir(parents=True)
    (src / "01.mp3").write_bytes(b"new")
    book = db.insert(importer.as_book(importer.read_meta(src, src.parent), "copy", "queued"))

    await Worker().process(book["id"])

    done = db.get(book["id"])
    assert done["status"] == "completed" and Path(done["path"]).name == "Dune (2)"
    assert (mine / "CD1" / "01.mp3").read_bytes() == b"my rip" and (
        mine / "desc.txt"
    ).read_text() == "my notes"


async def test_downloading_again_reuses_the_books_own_folder(isolated_data):
    src = isolated_data / "inbox" / "Frank Herbert - Dune"
    src.mkdir(parents=True)
    (src / "01.mp3").write_bytes(b"v1")
    book = db.insert(importer.as_book(importer.read_meta(src, src.parent), "copy", "queued"))
    await Worker().process(book["id"])
    first = db.get(book["id"])["path"]
    (src / "01.mp3").write_bytes(b"v2")
    db.update(book["id"], status="queued")
    await Worker().process(book["id"])
    assert db.get(book["id"])["path"] == first and (Path(first) / "01.mp3").read_bytes() == b"v2"
    assert not Path(first).with_name("Dune (2)").exists()


async def test_two_books_queued_together_get_separate_folders(isolated_data):
    config.load().folder_template = "{author}"
    books = []
    for i in (1, 2):
        src = isolated_data / "inbox" / f"Jane Austen - Book {i}"
        src.mkdir(parents=True)
        (src / "01.mp3").write_bytes(b"x")
        books.append(
            db.insert(importer.as_book(importer.read_meta(src, src.parent), "copy", "queued"))
        )
    w = Worker()
    await asyncio.gather(w.process(books[0]["id"]), w.process(books[1]["id"]))
    paths = {db.get(b["id"])["path"] for b in books}
    assert len(paths) == 2 and all((Path(p) / "01.mp3").exists() for p in paths)


async def test_real_debrid_files_with_the_same_name_both_survive(isolated_data):
    from tests.test_worker import FakeRD, make_book

    class TwoDiscs(FakeRD):
        def __call__(self, request):
            if request.url.host == "dl.example":
                return httpx.Response(200, content=request.url.path.encode())
            if request.url.path.endswith("/torrents/info/TID"):
                self.info_calls += 1
                if self.info_calls < 3:
                    return httpx.Response(200, json={"status": "downloading", "progress": 50})
                links = [
                    "https://real-debrid.com/d/CD1/01.mp3",
                    "https://real-debrid.com/d/CD2/01.mp3",
                ]
                return httpx.Response(
                    200, json={"status": "downloaded", "progress": 100, "links": links}
                )
            if request.url.path.endswith("/unrestrict/link"):
                link = request.content.decode()
                disc = "CD1" if "CD1" in link else "CD2"
                return httpx.Response(
                    200,
                    json={
                        "filename": "01.mp3",
                        "filesize": 10,
                        "download": f"https://dl.example/{disc}/01.mp3",
                    },
                )
            return super().__call__(request)

    book = make_book()
    await Worker(transport=httpx.MockTransport(TwoDiscs({}))).process(book["id"])
    done = db.get(book["id"])
    assert done["status"] == "completed", done["error"]
    names = sorted(p.name for p in Path(done["path"]).glob("*.mp3"))
    assert names == ["01 (2).mp3", "01.mp3"]


# ---- usenet import survives a crash in the middle -----------------------------------------------------


async def test_usenet_import_resumes_after_a_crash(isolated_data):
    from tests.test_prowlarr import USENET, FakeServers, add_release

    config.update(
        {
            "prowlarr_url": "http://prowlarr:9696",
            "prowlarr_api_key": "PKEY",
            "sabnzbd_url": "http://sabnzbd:8080",
            "sabnzbd_api_key": "SKEY",
        }
    )
    fake = FakeServers(isolated_data / "sab" / "complete")
    book = add_release(USENET, download_client="sabnzbd", client_job_id="SABnzbd_nzo_1")
    # An earlier run already moved the file out of SABnzbd's folder, then crashed before organizing.
    tmp = tmp_dir(book, config.load())
    tmp.mkdir(parents=True)
    (tmp / "Dune.m4b").write_bytes(b"audio")
    fake.polls = 5  # SABnzbd reports the job completed, but its folder won't be there any more

    await Worker(transport=httpx.MockTransport(fake)).process(book["id"])

    done = db.get(book["id"])
    assert done["status"] == "completed", done["error"]
    assert (Path(done["path"]) / "Dune.m4b").read_bytes() == b"audio"


# ---- clients only touch this book's own torrent ---------------------------------------------------------


async def test_stop_leaves_another_books_torrent_alone():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/torrents/info":
            return httpx.Response(
                200,
                json=[
                    {
                        "hash": "h",
                        "tags": "narratarr-1",
                        "save_path": "/dl",
                        "content_path": "/dl",
                        "name": "Dune.m4b",
                        "state": "uploading",
                        "progress": 1,
                    }
                ],
            )
        calls.append(request.url.path)
        return httpx.Response(200)

    async with QBittorrent("http://q:8080", transport=httpx.MockTransport(handler)) as q:
        await q.delete("h", files=True, tag="narratarr-2")  # book 2 adopted book 1's torrent
        assert calls == []
        await q.delete("h", files=True, tag="narratarr-1")
        assert calls == ["/api/v2/torrents/delete"]
        job = await q.job("h")
        assert (
            job.path == "/dl/Dune.m4b"
        )  # a bare single-file torrent names the file, not the whole folder


def test_unfollow_purge_only_touches_that_author(isolated_data):
    f = db.add_follow("author", "A1", "Lee Child", "", [])
    db.insert(
        {
            "source": "audible",
            "source_url": "https://a/1",
            "title": "Killing Floor",
            "author": "Lee Child",
            "status": "wanted",
        }
    )
    db.insert(
        {
            "source": "audible",
            "source_url": "https://a/2",
            "title": "Mockingbird",
            "author": "Harper Lee",
            "status": "wanted",
        }
    )
    db.insert(
        {
            "source": "audible",
            "source_url": "https://a/3",
            "title": "Co-written",
            "author": "Lee Child, Andrew Child",
            "status": "wanted",
        }
    )
    from narratarr import wanted

    counts = wanted.follow_counts(f, db.list_books())
    assert counts["wanted"] == 2


async def test_stop_waits_for_file_work_to_finish(isolated_data):
    import threading

    w = Worker()
    started, release = threading.Event(), threading.Event()

    def slow(_stop):
        started.set()
        release.wait(5)
        return "done"

    w.stops[9] = threading.Event()
    task = asyncio.ensure_future(w._in_thread(9, slow))
    await asyncio.to_thread(started.wait, 5)
    waiter = asyncio.ensure_future(w.cancel_and_wait(9))
    await asyncio.sleep(0.05)
    assert not waiter.done()  # still waiting on the thread
    release.set()
    await asyncio.wait_for(waiter, 5)
    assert task.cancelled() or task.done()
