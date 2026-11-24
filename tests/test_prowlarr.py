from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from narratarr import api, config, db, search, worker
from narratarr.sources import prowlarr
from narratarr.sources.prowlarr import (
    Prowlarr,
    ProwlarrError,
    strip_key,
    tidy_title,
    to_details,
    with_key,
)
from narratarr.worker import Worker, mapped_path

PROWLARR = "http://prowlarr:9696"
SAB = "http://sabnzbd:8080"
HASH = "061850ead3eb6f1c5c6d8420211b4bbf2d4ee3e2"
NZB = b'<?xml version="1.0"?>\n<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb"></nzb>'

USENET = {
    "guid": "https://nzbs.example/details/abc",
    "title": "Frank.Herbert-Dune.(2006).[M4B].64kbps",
    "size": 734003200,
    "indexer": "NZBs Example",
    "indexerId": 4,
    "protocol": "usenet",
    "publishDate": "2024-05-01T10:00:00Z",
    "downloadUrl": f"{PROWLARR}/4/download?apikey=PKEY&link=abc&file=Dune",
}
TORRENT = {
    "guid": "https://tracker.example/t/9",
    "title": "Andy Weir - Project Hail Mary [MP3 128kbps]",
    "size": 512000000,
    "indexer": "Tracker",
    "indexerId": 7,
    "protocol": "torrent",
    "seeders": 31,
    "magnetUrl": f"magnet:?xt=urn:btih:{HASH.upper()}&dn=PHM&tr=udp%3A%2F%2Ftracker.example%3A1337",
}
TORRENT_FILE = {
    **TORRENT,
    "guid": "https://tracker.example/t/10",
    "magnetUrl": None,
    "downloadUrl": f"{PROWLARR}/7/download?apikey=PKEY&link=t10",
}


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    prowlarr._releases.clear()
    prowlarr._search_cache.clear()
    monkeypatch.setattr(worker, "POLL_SECONDS", 0)
    config.update(
        {
            "prowlarr_url": PROWLARR,
            "prowlarr_api_key": "PKEY",
            "sabnzbd_url": SAB,
            "sabnzbd_api_key": "SKEY",
            "sabnzbd_category": "audiobooks",
        }
    )


def test_release_parsing():
    d = to_details(USENET)
    assert d.title == "Frank Herbert - Dune"
    assert (d.format, d.bitrate, d.size, d.posted) == ("M4B", "64 Kbps", "700.0 MB", "2024-05-01")
    assert d.protocol == "usenet" and d.seeders is None and d.indexer == "NZBs Example"
    assert "apikey" not in d.download_url and "link=abc" in d.download_url

    t = to_details(TORRENT)
    assert t.title == "Andy Weir - Project Hail Mary"
    assert t.info_hash == HASH and t.trackers == ["udp://tracker.example:1337"]
    assert t.seeders == 31 and t.download_url.startswith("magnet:")  # trackers travel with it


def test_api_key_only_goes_back_to_prowlarr():
    assert strip_key("http://p/1/download?apikey=X&link=y") == "http://p/1/download?link=y"
    assert "apikey=K" in with_key("http://p:9696/1/download?link=y", "http://p:9696", "K")
    assert (
        with_key("https://elsewhere.example/x", "http://p:9696", "K")
        == "https://elsewhere.example/x"
    )
    assert tidy_title("Some Book (Unabridged)") == "Some Book"


async def test_search_and_details():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("x-api-key")
        seen["params"] = parse_qs(request.url.query.decode())
        junk = {"title": "no link", "protocol": "torrent"}
        return httpx.Response(200, json=[USENET, TORRENT, junk])

    p = Prowlarr(
        PROWLARR,
        "PKEY",
        "3030, 3000",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    page = await p.search("dune")
    assert [i.protocol for i in page.items] == ["usenet", "torrent"]
    assert seen["key"] == "PKEY"
    assert seen["params"]["categories"] == ["3030", "3000"] and seen["params"]["query"] == ["dune"]
    assert (await p.search("dune", page=2)).items == []

    url = page.items[0].url
    assert p.owns(url) and not p.owns("https://archive.org/details/x")

    async def apple(q, limit=8, client=None):
        return [
            {
                "title": "Dune: Book 1",
                "author": "Frank Herbert",
                "cover_url": "https://apple.example/dune.jpg",
            }
        ]

    search.suggestions, original = apple, search.suggestions
    try:
        d = await p.details(url)
    finally:
        search.suggestions = original
    assert (d.title, d.author, d.cover_url) == (
        "Dune",
        "Frank Herbert",
        "https://apple.example/dune.jpg",
    )

    prowlarr._releases.clear()
    with pytest.raises(ProwlarrError, match="expired"):
        await p.details(url)


async def test_identify_refuses_loose_matches(monkeypatch):
    async def apple(q, limit=8, client=None):
        return [{"title": "Dune Messiah", "author": "Frank Herbert", "cover_url": ""}]

    monkeypatch.setattr(search, "suggestions", apple)
    assert await search.identify("Brian Herbert - Sandworms of Dune") is None


async def test_bad_key_message():
    p = Prowlarr(
        PROWLARR,
        "nope",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401))),
    )
    with pytest.raises(ProwlarrError, match="API key"):
        await p.search("dune")


class FakeServers:
    """Prowlarr, SABnzbd and Real-Debrid on one fake network."""

    def __init__(self, sab_dir: Path, history_status="Completed", report_path: str | None = None):
        self.sab_dir = sab_dir
        self.history_status = history_status
        self.report_path = report_path
        self.polls = 0
        self.added = None
        self.forgotten = False
        self.prowlarr_keys = []
        self.rd_torrent = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        q = parse_qs(url.query.decode())
        if url.host == "prowlarr":
            self.prowlarr_keys.append(q.get("apikey", [""])[0])
            if "link=t10" in str(url):
                return httpx.Response(200, content=b"d8:announce3:url4:infod4:name3:PHMee")
            return httpx.Response(200, content=NZB, headers={"content-type": "application/x-nzb"})
        if url.host == "sabnzbd":
            assert q["apikey"] == ["SKEY"]
            mode = q["mode"][0]
            if mode == "addfile":
                self.added = {
                    "cat": q.get("cat"),
                    "name": q.get("nzbname"),
                    "body": request.content,
                }
                return httpx.Response(200, json={"status": True, "nzo_ids": ["SABnzbd_nzo_1"]})
            if mode == "queue":
                self.polls += 1
                if self.polls == 1:
                    slot = {
                        "nzo_id": "SABnzbd_nzo_1",
                        "status": "Downloading",
                        "percentage": "40",
                        "timeleft": "0:02:00",
                    }
                    return httpx.Response(
                        200, json={"queue": {"kbpersec": "2048", "slots": [slot]}}
                    )
                return httpx.Response(200, json={"queue": {"slots": []}})
            if mode == "history":
                if "name" in q:
                    self.forgotten = True
                    return httpx.Response(200, json={"status": True})
                job = self.sab_dir / "Frank Herbert - Dune"
                job.mkdir(parents=True, exist_ok=True)
                (job / "Dune.m4b").write_bytes(b"audio")
                slot = {
                    "nzo_id": "SABnzbd_nzo_1",
                    "status": self.history_status,
                    "fail_message": "Out of retention",
                    "storage": self.report_path or str(job),
                }
                return httpx.Response(200, json={"history": {"slots": [slot]}})
        if url.host == "api.real-debrid.com":
            if url.path.endswith("/torrents/addTorrent"):
                self.rd_torrent = request.content
                return httpx.Response(201, json={"id": "TID"})
            if url.path.endswith("/torrents/info/TID"):
                return httpx.Response(200, json={"status": "dead"})
        return httpx.Response(404)


def add_release(release: dict, **extra) -> dict:
    d = to_details(release)
    return db.insert(
        {
            "source": "prowlarr",
            "source_url": d.url,
            "title": "Dune",
            "author": "Frank Herbert",
            "protocol": d.protocol,
            "download_url": d.download_url,
            "info_hash": d.info_hash,
            "status": "queued",
            **extra,
        }
    )


async def test_usenet_pipeline(isolated_data):
    fake = FakeServers(isolated_data / "sab" / "complete")
    book = add_release(USENET)

    await Worker(transport=httpx.MockTransport(fake)).process(book["id"])

    done = db.get(book["id"])
    assert done["status"] == "completed", done["error"]
    assert (Path(done["path"]) / "Dune.m4b").read_bytes() == b"audio"
    assert fake.added["cat"] == ["audiobooks"] and fake.added["name"] == ["Frank Herbert - Dune"]
    assert NZB in fake.added["body"]
    assert fake.prowlarr_keys == ["PKEY"]
    assert fake.forgotten and done["client_job_id"] == ""
    assert not (isolated_data / "sab" / "complete" / "Frank Herbert - Dune").exists()


async def test_usenet_path_mapping(isolated_data):
    config.update({"path_remote": "/downloads", "path_local": str(isolated_data / "sab")})
    fake = FakeServers(
        isolated_data / "sab" / "complete", report_path="/downloads/complete/Frank Herbert - Dune"
    )
    book = add_release(USENET)

    await Worker(transport=httpx.MockTransport(fake)).process(book["id"])

    assert db.get(book["id"])["status"] == "completed"


async def test_usenet_unreachable_folder_explains_mapping(isolated_data):
    fake = FakeServers(isolated_data / "sab" / "complete", report_path="/somewhere/else")
    book = add_release(USENET)

    await Worker(transport=httpx.MockTransport(fake))._run(book["id"])

    failed = db.get(book["id"])
    assert failed["status"] == "failed" and "path mapping" in failed["error"]


async def test_usenet_failure_reason_is_shown(isolated_data):
    fake = FakeServers(isolated_data / "sab" / "complete", history_status="Failed")
    book = add_release(USENET)

    await Worker(transport=httpx.MockTransport(fake))._run(book["id"])

    failed = db.get(book["id"])
    assert failed["status"] == "failed" and "Out of retention" in failed["error"]


async def test_prowlarr_torrent_file_goes_to_real_debrid(isolated_data):
    fake = FakeServers(isolated_data / "sab")
    book = add_release(TORRENT_FILE)
    assert book["download_url"] and not book["info_hash"]

    await Worker(transport=httpx.MockTransport(fake))._run(book["id"])

    assert fake.rd_torrent.startswith(b"d8:announce")
    assert (
        "dead" in db.get(book["id"])["error"]
    )  # our fake Real-Debrid gives up; the hand-off is what matters


def test_mapped_path_mapping():
    s = config.load()
    s.path_remote, s.path_local = "/downloads/", "/mnt/usenet"
    assert mapped_path("/downloads/complete/Dune", s) == Path("/mnt/usenet/complete/Dune")
    assert mapped_path("/downloads-other/Dune", s) == Path("/downloads-other/Dune")
    s.path_remote, s.path_local = "D:\\Usenet", "/mnt/usenet"
    assert mapped_path("D:\\Usenet\\complete\\Dune", s) == Path("/mnt/usenet/complete/Dune")


def test_prowlarr_tab_appears_once_configured():
    with TestClient(api.app) as c:
        c.app.state.worker.poke = lambda: None
        assert "prowlarr" in [s["name"] for s in c.get("/api/sources").json()]
        s = c.put(
            "/api/settings", json={"prowlarr_api_key": "", "sabnzbd_api_key": "ABCDEFGH9876"}
        ).json()
        assert "prowlarr" not in [x["name"] for x in c.get("/api/sources").json()]
        assert s["sabnzbd_api_key"].endswith("9876") and "ABCD" not in s["sabnzbd_api_key"]
        assert s["sabnzbd_configured"] and not s["prowlarr_configured"]
