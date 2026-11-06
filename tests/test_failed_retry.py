"""Failed downloads: annotation, filtering, and auto-retry."""
import httpx
import pytest
from narratarr import config, db, wanted, worker
from narratarr.downloaders import ClientError
from narratarr.worker import Worker

HASH1 = "a" * 40
HASH2 = "b" * 40

PROWLARR = "http://prowlarr:9696"

def make_failed_book(**overrides):
    base = {
        "source": "prowlarr",
        "source_url": "https://n.example/book",
        "title": "Dune",
        "author": "Frank Herbert",
        "status": "failed",
        "release_url": "https://n.example/1",
        "info_hash": HASH1,
        "error": "qBittorrent reports a problem",
        "format": "M4B",
    }
    return db.insert({**base, **overrides})

def fake_find_releases(monkeypatch, releases):
    async def _find(book):
        return releases
    monkeypatch.setattr(wanted, "find_releases", _find)

def test_best_skips_failed_and_excluded():
    found = [
        {"url": "https://n.example/1", "info_hash": HASH1, "usable": True, "language_ok": True, "match": 90, "seeders": 5},
        {"url": "https://n.example/2", "info_hash": HASH2, "usable": True, "language_ok": True, "match": 88, "seeders": 3},
    ]
    assert wanted.best(found)["url"] == "https://n.example/1"
    # failed flag
    found[0]["failed"] = True
    assert wanted.best(found)["url"] == "https://n.example/2"
    found[0]["failed"] = False
    # exclude via url
    assert wanted.best(found, exclude={"release_url": "https://n.example/1"})["url"] == "https://n.example/2"
    # exclude via info_hash case-insensitive
    assert wanted.best(found, exclude={"info_hash": HASH1.upper()})["url"] == "https://n.example/2"

def test_api_annotates_failed_release(isolated_data, monkeypatch):
    from fastapi.testclient import TestClient
    from narratarr import api
    from narratarr.sources.prowlarr import Prowlarr

    # fake Prowlarr releases
    releases = [
        {"guid": "https://n.example/1", "title": "Dune M4B", "infoHash": HASH1, "protocol": "torrent", "indexer": "I", "downloadUrl": f"{PROWLARR}/dl/1", "size": 100},
        {"guid": "https://n.example/2", "title": "Dune M4B alt", "infoHash": HASH2, "protocol": "torrent", "indexer": "I", "downloadUrl": f"{PROWLARR}/dl/2", "size": 100},
    ]
    # also need to mock find_releases directly to avoid real HTTP
    fake_releases = [
        {"url": "https://n.example/1", "info_hash": HASH1, "usable": True, "language_ok": True, "match": 90, "seeders": 5, "release": "Dune", "title": "Dune", "source": "prowlarr", "protocol": "torrent", "format": "M4B", "size": "1GB", "language": "English", "indexer": "I", "posted": ""},
        {"url": "https://n.example/2", "info_hash": HASH2, "usable": True, "language_ok": True, "match": 88, "seeders": 3, "release": "Dune alt", "title": "Dune", "source": "prowlarr", "protocol": "torrent", "format": "M4B", "size": "1GB", "language": "English", "indexer": "I", "posted": ""},
    ]
    fake_find_releases(monkeypatch, fake_releases)
    config.update({"prowlarr_url": PROWLARR, "prowlarr_api_key": "K", "qbittorrent_url": "http://qb:8080"})

    book = make_failed_book()
    client = TestClient(api.app, client=("192.168.1.20", 50000))
    client.__enter__()
    # bypass auth
    from narratarr import auth
    monkeypatch.setattr(auth, "current_user", lambda r: {"id": 1, "username": "admin", "role": "admin"})
    # need to re-enter after monkeypatch? Use direct call via api.releases
    import asyncio
    async def run():
        from narratarr.api import releases
        # mock _get_or_404 via db
        res = await releases(book["id"])
        assert res["items"][0]["failed"] is True
        assert res["items"][0]["failed_reason"] == "qBittorrent reports a problem"
        assert res["items"][1]["failed"] is False
        # info_hash match also
        # second release with same info_hash as failed should also be marked
        fake_releases[1]["info_hash"] = HASH1
        res2 = await releases(book["id"])
        assert res2["items"][1]["failed"] is True
    asyncio.run(run())
    client.__exit__(None, None, None)

@pytest.mark.asyncio
async def test_worker_retries_with_next_best(isolated_data, monkeypatch):
    """Worker retries failed download with next best release, single retry."""
    from narratarr.downloaders import ClientError

    # Two releases
    r1 = {"url": "https://n.example/1", "info_hash": HASH1, "usable": True, "language_ok": True, "match": 90, "seeders": 5, "release": "Dune 1", "title": "Dune", "source": "prowlarr", "protocol": "torrent", "format": "M4B"}
    r2 = {"url": "https://n.example/2", "info_hash": HASH2, "usable": True, "language_ok": True, "match": 88, "seeders": 3, "release": "Dune 2", "title": "Dune", "source": "prowlarr", "protocol": "torrent", "format": "M4B"}

    # Book initially grabbed r1, now queued
    book = db.insert({"source": "prowlarr", "source_url": "https://n.example/book", "title": "Dune", "author": "Frank Herbert", "status": "queued", "release_url": r1["url"], "info_hash": HASH1, "download_url": f"{PROWLARR}/dl/1", "protocol": "torrent", "indexer": "I"})

    # Mock find_releases to return both, with r1 marked failed after we annotate
    async def fake_find(book_dict):
        # return copies
        return [dict(r1), dict(r2)]
    monkeypatch.setattr(wanted, "find_releases", fake_find)

    # Mock wanted.grab to update db
    orig_grab = wanted.grab
    async def fake_grab(book_id, url, source, status="queued"):
        # simulate grab by updating release_url/info_hash
        db.update(book_id, release_url=url, info_hash=HASH2 if url==r2["url"] else HASH1, status=status)
        return db.get(book_id)
    monkeypatch.setattr(wanted, "grab", fake_grab)

    # Make Worker.process fail first time with ClientError (simulating dead torrent), succeed second time
    calls = []
    orig_process = Worker.process
    async def fake_process(self, book_id):
        calls.append(book_id)
        if len(calls) == 1:
            raise ClientError("qBittorrent reports a problem with this torrent (error).")
        # second call succeeds (simulate completing)
        db.update(book_id, status="completed", path="/tmp/done")
    monkeypatch.setattr(Worker, "process", fake_process)

    w = Worker()
    await w._run(book["id"])

    done = db.get(book["id"])
    assert done["status"] == "completed", done
    assert done["release_url"] == r2["url"], "should have retried with next best"
    assert len(calls) == 2

@pytest.mark.asyncio
async def test_worker_no_retry_when_no_alternative(isolated_data, monkeypatch):
    book = db.insert({"source": "prowlarr", "source_url": "https://n.example/book", "title": "Dune", "author": "Frank", "status": "queued", "release_url": "https://n.example/1", "info_hash": HASH1, "protocol": "torrent"})
    async def fake_find(b):
        return [{"url": "https://n.example/1", "info_hash": HASH1, "usable": True, "language_ok": True, "match": 90, "seeders": 5, "release": "Dune"}]
    monkeypatch.setattr(wanted, "find_releases", fake_find)
    async def fake_process(self, book_id):
        raise ValueError("no seeds")
    monkeypatch.setattr(Worker, "process", fake_process)
    w = Worker()
    await w._run(book["id"])
    assert db.get(book["id"])["status"] == "failed"
    assert "no seeds" in db.get(book["id"])["error"]

@pytest.mark.asyncio
async def test_worker_retry_avoids_loop(isolated_data, monkeypatch):
    """Second failure should not retry again."""
    r1 = {"url": "https://n.example/1", "info_hash": HASH1, "usable": True, "language_ok": True, "match": 90, "seeders": 5, "release": "R1", "title": "Dune", "source": "prowlarr", "protocol": "torrent"}
    r2 = {"url": "https://n.example/2", "info_hash": HASH2, "usable": True, "language_ok": True, "match": 88, "seeders": 3, "release": "R2", "title": "Dune", "source": "prowlarr", "protocol": "torrent"}
    book = db.insert({"source": "prowlarr", "source_url": "https://n.example/book", "title": "Dune", "author": "Frank", "status": "queued", "release_url": r1["url"], "info_hash": HASH1, "protocol": "torrent"})
    async def fake_find(b):
        return [dict(r1), dict(r2)]
    monkeypatch.setattr(wanted, "find_releases", fake_find)
    async def fake_grab(book_id, url, source, status="queued"):
        db.update(book_id, release_url=url, info_hash=HASH2 if url==r2["url"] else HASH1, status=status)
        return db.get(book_id)
    monkeypatch.setattr(wanted, "grab", fake_grab)
    async def fake_process(self, book_id):
        raise ClientError("failed")
    monkeypatch.setattr(Worker, "process", fake_process)
    w = Worker()
    await w._run(book["id"])
    failed = db.get(book["id"])
    assert failed["status"] == "failed"
    # should contain both errors
    assert "retried with" in failed["error"]
    # Should have retried only once, not infinitely
