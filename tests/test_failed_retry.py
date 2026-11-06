"""Failed downloads (PR #15, curator8888): a release that fails is remembered and marked, and another
release of the book is tried, the way Sonarr and Radarr do. Plus ratings on Library cards (PR #14)."""

import pytest
from fastapi.testclient import TestClient

from narratarr import api, config, db, ratings, wanted
from narratarr.downloaders import ClientError, Job, bad_release, is_bad_release
from narratarr.worker import Worker

HASH1, HASH2, HASH3 = "a" * 40, "b" * 40, "c" * 40


def rel(n: int, info_hash: str, match: int = 90) -> dict:
    return {"url": f"https://n.example/{n}", "info_hash": info_hash, "usable": True, "language_ok": True,
            "match": match, "seeders": 5, "release": f"Dune {n}", "title": "Dune", "source": "prowlarr",
            "protocol": "torrent"}


@pytest.fixture
def client():
    with TestClient(api.app) as c:
        c.app.state.worker.poke = lambda: None
        yield c


@pytest.fixture
def indexer(monkeypatch):
    """What a search finds, and every release grabbed (wanted.grab without asking the indexer for details)."""
    found, grabbed = [], []

    async def find(book):
        return [dict(r) for r in found]

    async def grab(book_id, url, source, status="queued"):
        grabbed.append(url)
        r = next(r for r in found if r["url"] == url)
        db.update(book_id, release_url=url, info_hash=r["info_hash"], source=source, status=status, error="",
                  client_job_id="", found_by_search=0)
        return db.get(book_id)

    monkeypatch.setattr(wanted, "find_releases", find)
    monkeypatch.setattr(wanted, "grab", grab)
    return found, grabbed


def downloading(**over) -> dict:
    return db.insert({"source": "prowlarr", "source_url": "https://www.audible.com/pd/B0DUNE", "title": "Dune",
                      "author": "Frank Herbert", "status": "queued", "release_url": "https://n.example/1",
                      "info_hash": HASH1, "protocol": "torrent", "client_job_id": "job-1", **over})


def failing_with(monkeypatch, error: Exception):
    async def process(self, book_id):
        raise error
    monkeypatch.setattr(Worker, "process", process)


# ---- picking around failures ---------------------------------------------------------------


def test_automatic_picking_skips_failed_releases_and_the_same_torrent_elsewhere():
    book = {"failed_releases": [{"url": "https://n.example/1", "info_hash": HASH1, "error": "dead"}],
            "skipped_releases": ["https://n.example/2"]}
    found = [rel(1, HASH1), rel(2, HASH2), rel(4, HASH1.upper()), rel(3, HASH3, match=88)]
    assert wanted.best(found, *wanted.avoid(book))["url"] == "https://n.example/3"
    assert wanted.failed_release(book, rel(4, HASH1)) == "dead"  # another indexer, same torrent
    assert wanted.failed_release(book, rel(3, HASH3)) is None


# ---- the worker ------------------------------------------------------------------------------


async def test_a_dead_release_is_remembered_and_the_next_best_one_taken(monkeypatch, indexer):
    found, grabbed = indexer
    found += [rel(1, HASH1), rel(2, HASH2)]
    book = downloading()
    failing_with(monkeypatch, bad_release(ClientError("qBittorrent reports a problem with this torrent.")))
    await Worker()._run(book["id"])

    got = db.get(book["id"])
    assert grabbed == ["https://n.example/2"]
    assert got["status"] == "queued" and got["release_url"] == "https://n.example/2" and got["failed_retries"] == 1
    assert got["failed_releases"][0]["url"] == "https://n.example/1"
    assert "qBittorrent reports a problem" in got["failed_releases"][0]["error"]
    assert "picked another" in got["search_note"]


async def test_with_approval_on_the_next_release_waits_for_it(monkeypatch, indexer):
    found, _ = indexer
    found += [rel(1, HASH1), rel(2, HASH2)]
    config.update({"found_needs_approval": True})
    book = downloading()
    failing_with(monkeypatch, bad_release(ClientError("SABnzbd: repair failed")))
    await Worker()._run(book["id"])
    got = db.get(book["id"])
    assert got["status"] == "requested" and got["found_by_search"] == 1 and got["release_url"] == "https://n.example/2"


async def test_a_setup_problem_is_not_retried(monkeypatch, indexer):
    """The Discord case: the client saved the book where Narratarr can't see it. Another release
    would land in the same place, so nothing is searched and nothing is marked as a bad release."""
    found, grabbed = indexer
    found += [rel(1, HASH1), rel(2, HASH2)]
    book = downloading()
    failing_with(monkeypatch, ValueError("qBittorrent saved this book to /downloads/Dune, but Narratarr can't see that folder."))
    await Worker()._run(book["id"])
    got = db.get(book["id"])
    assert got["status"] == "failed" and grabbed == [] and got["failed_releases"] == []


async def test_nothing_else_good_leaves_it_failed_and_says_so(monkeypatch, indexer):
    found, grabbed = indexer
    found += [rel(1, HASH1), rel(5, HASH2, match=40)]
    book = downloading()
    failing_with(monkeypatch, bad_release(ValueError("The torrent finished, but there are no audio files in it.")))
    await Worker()._run(book["id"])
    got = db.get(book["id"])
    assert got["status"] == "failed" and grabbed == []
    assert "no audio files" in got["error"] and "none was a sure match" in got["error"]
    assert [f["url"] for f in got["failed_releases"]] == ["https://n.example/1"]


async def test_retries_stop_after_two_and_when_switched_off(monkeypatch, indexer):
    found, grabbed = indexer
    found += [rel(1, HASH1), rel(2, HASH2)]
    failing_with(monkeypatch, bad_release(ClientError("dead")))
    tired = downloading(failed_retries=wanted.MAX_FAILED_RETRIES)
    await Worker()._run(tired["id"])
    assert db.get(tired["id"])["status"] == "failed" and grabbed == []
    assert db.get(tired["id"])["failed_releases"]  # still remembered, still marked

    config.update({"retry_failed_downloads": False})
    off = downloading(source_url="https://www.audible.com/pd/B0OTHER")
    await Worker()._run(off["id"])
    assert db.get(off["id"])["status"] == "failed" and grabbed == []


async def test_a_direct_source_has_no_other_release(monkeypatch, indexer):
    found, grabbed = indexer
    found += [rel(2, HASH2)]
    book = downloading(source="librivox", release_url="https://librivox.org/dune/", info_hash="")
    failing_with(monkeypatch, bad_release(ValueError("no audio")))
    await Worker()._run(book["id"])
    assert db.get(book["id"])["status"] == "failed" and grabbed == []


async def test_a_client_saying_failed_is_a_bad_release_but_a_vanished_job_is_not():
    class Client:
        label = "qBittorrent"

        def __init__(self, job):
            self._job = job

        async def job(self, job_id):
            return self._job

    book = downloading()
    w = Worker()
    with pytest.raises(ClientError) as failed:
        await w._wait_for_client(Client(Job(state="failed", error="Missing articles")), book["id"], "j", config.load())
    assert is_bad_release(failed.value)
    with pytest.raises(ClientError) as gone:  # someone removed it in the client: their call, not a bad torrent
        await w._wait_for_client(Client(None), book["id"], "j", config.load())
    assert not is_bad_release(gone.value)


# ---- Find releases ---------------------------------------------------------------------------


def test_find_releases_marks_what_failed_and_what_was_turned_down(client, indexer):
    found, _ = indexer
    found += [rel(1, HASH1), rel(2, HASH2), rel(3, HASH3)]
    book = downloading(status="failed", skipped_releases=["https://n.example/3"],
                       failed_releases=[{"url": "https://n.example/1", "info_hash": HASH1, "error": "Dead torrent"}])
    items = client.get(f"/api/library/{book['id']}/releases").json()["items"]
    assert [(i["failed"], i["turned_down"]) for i in items] == [("Dead torrent", False), ("", False), ("", True)]


# ---- switches save as switches ---------------------------------------------------------------


def test_a_switch_can_be_turned_off_and_old_on_text_reads_as_on(client):
    assert client.put("/api/settings", json={"found_needs_approval": True}).json()["found_needs_approval"] is True
    assert client.put("/api/settings", json={"found_needs_approval": False}).json()["found_needs_approval"] is False
    # What older pages sent for a ticked box, and what got saved:
    assert client.put("/api/settings", json={"retry_failed_downloads": "on"}).json()["retry_failed_downloads"] is True
    assert config.as_bool("off") is False and config.as_bool("") is False and config.as_bool(1) is True


def test_a_settings_file_with_on_text_loads_as_true(isolated_data, monkeypatch):
    import json
    config.SETTINGS_FILE.write_text(json.dumps({"ratings_enabled": "on", "found_needs_approval": "false"}))
    monkeypatch.setattr(config, "_settings", None)
    s = config.load()
    assert s.ratings_enabled is True and s.found_needs_approval is False


# ---- ratings on Library cards (PR #14) -------------------------------------------------------


class Shelf:
    """A ratings provider whose cache holds one rated book."""
    enabled = True

    def cached(self, title, author, asin=""):
        return ratings.make_rating(4.6, None, "https://www.audible.com/pd/B0DUNE", "audnex") if asin == "B0DUNE" else None

    def state(self, title, author, asin=""):
        return "none"


def test_library_ratings_come_from_the_cache_and_only_asin_books_are_queued(client, monkeypatch):
    queued = []
    monkeypatch.setattr(ratings, "provider", lambda settings=None: Shelf())
    monkeypatch.setattr(ratings, "ensure_filler", lambda: None)
    monkeypatch.setattr(ratings, "_enqueue", lambda t, a, asin="": queued.append(asin))
    rated = db.insert({"source": "local", "source_url": "file:///b/dune", "title": "Dune", "author": "Frank Herbert",
                       "status": "completed", "asin": "B0DUNE"})
    db.insert({"source": "local", "source_url": "file:///b/x", "title": "Unknown", "author": "Someone", "status": "completed"})
    db.insert({"source": "local", "source_url": "file:///b/y", "title": "Hyperion", "author": "Dan Simmons",
               "status": "completed", "asin": "B0HYPER"})
    out = client.get("/api/library/ratings").json()
    assert out["off"] is False and list(out["ratings"]) == [str(rated["id"])]
    assert out["ratings"][str(rated["id"])]["score"] == 4.6
    assert queued == ["B0HYPER"]  # the unrated book with an ASIN; never a search for the one without


def test_library_ratings_switched_off(client):
    config.update({"ratings_enabled": False})
    assert client.get("/api/library/ratings").json() == {"off": True, "ratings": {}}


def test_asking_twice_queues_once():
    ratings.reset_for_tests()
    before = ratings._queue.qsize()
    ratings._enqueue("Dune", "Frank Herbert", "B0DUNE")
    ratings._enqueue("Dune", "Frank Herbert", "B0DUNE")
    assert ratings._queue.qsize() == before + 1
