"""From the Discord (aarsenca): tidy a single book, and fill in a download's details before it's filed."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from narratarr import api, config, db, tidy, wanted
from narratarr.worker import Worker

from test_tidy import book, put  # noqa: F401 - helpers


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "library"
    root.mkdir(exist_ok=True)
    config.update({"folder_template": "{author}/{series}/{title}"})
    return root.resolve()


@pytest.fixture
def client():
    with TestClient(api.app) as c:
        c.app.state.worker.poke = lambda: None
        yield c


# ---- Tidy this book --------------------------------------------------------------------------------


def test_one_book_shows_where_it_goes_then_moves_and_can_be_undone(lib, client):
    old = put(lib, "dune rip")
    b = book(old)
    other = book(put(lib, "messy other"), title="Artemis", author="Andy Weir", series=[])

    r = client.get(f"/api/tidy/book/{b['id']}").json()
    assert r["state"] == "move" and r["to"] == "Frank Herbert/Dune Chronicles/Dune"

    moved = client.post(f"/api/tidy/book/{b['id']}").json()
    assert moved["moved"] == 1
    assert db.get(b["id"])["path"] == str(lib / "Frank Herbert/Dune Chronicles/Dune")
    assert (lib / "Frank Herbert/Dune Chronicles/Dune/01.mp3").exists() and not old.exists()
    assert db.get(other["id"])["path"] == str(lib / "messy other"), "nothing else moved"

    assert client.get(f"/api/tidy/book/{b['id']}").json()["state"] == "tidy"
    assert tidy.undo(config.load())["back"] == 1
    assert (old / "01.mp3").exists()


def test_a_book_that_isnt_finished_cant_be_tidied(lib, client):
    b = book(put(lib, "dune rip"), status="downloading")
    assert client.get(f"/api/tidy/book/{b['id']}").status_code == 409
    assert client.get("/api/tidy/book/99999").status_code == 404


def test_one_book_waits_while_tidy_library_is_running(lib, client, monkeypatch):
    b = book(put(lib, "dune rip"))
    monkeypatch.setattr(tidy, "JOB", tidy.Job(running=True))
    assert client.post(f"/api/tidy/book/{b['id']}").status_code == 409
    assert db.get(b["id"])["path"] == str(lib / "dune rip")


def test_a_listener_cant_tidy(lib, monkeypatch):
    from narratarr import auth
    b = book(put(lib, "dune rip"))
    monkeypatch.setattr(auth, "current_user", lambda request: {"id": 2, "username": "l", "role": "listener", "created_at": 0})
    with TestClient(api.app) as c:
        assert c.post(f"/api/tidy/book/{b['id']}").status_code == 403
        assert c.get(f"/api/tidy/book/{b['id']}").status_code == 403


# ---- Fill in missing details before filing a download -----------------------------------------------


def _download(**extra):
    return db.insert({"source": "prowlarr", "source_url": "https://tracker.example/t/1", "title": "Dune",
                      "author": "Frank Herbert", "protocol": "torrent", "status": "downloading", **extra})


MATCH = {"title": "Dune", "author": "Frank Herbert", "asin": "B002V1OF70", "narrators": ["Scott Brick"],
         "released": "2007-01-01", "series": [{"asin": "B00X", "title": "Dune Chronicles", "sequence": "1"}]}


@pytest.fixture
def audible(monkeypatch):
    asked = []

    async def match(b):
        asked.append(b["title"])
        if isinstance(match.result, Exception):
            raise match.result
        return match.result

    match.result = MATCH
    monkeypatch.setattr(wanted, "audible_match", match)
    return asked


async def test_switched_on_a_download_gets_its_details_before_filing(audible):
    config.update({"fill_details_before_filing": True})
    b = await Worker()._fill_before_filing(db.get(_download()["id"]), config.load())
    assert b["series"][0]["title"] == "Dune Chronicles" and b["narrators"] == ["Scott Brick"]
    assert b["released"] == "2007-01-01"


async def test_off_by_default(audible):
    b = db.get(_download()["id"])
    assert await Worker()._fill_before_filing(b, config.load()) == b
    assert audible == []


async def test_imports_and_complete_books_are_left_alone(audible):
    config.update({"fill_details_before_filing": True})
    local = db.get(db.insert({"source": "local", "source_url": "local:x", "title": "Dune", "author": "Frank Herbert",
                              "status": "queued"})["id"])
    whole = db.get(_download(source_url="u2", narrators=["Me"], released="1965",
                             series=[{"asin": "", "title": "Mine", "sequence": "1"}])["id"])
    await Worker()._fill_before_filing(local, config.load())
    await Worker()._fill_before_filing(whole, config.load())
    assert audible == []
    assert db.get(whole["id"])["narrators"] == ["Me"]


async def test_a_failed_lookup_never_fails_the_download(audible):
    config.update({"fill_details_before_filing": True})
    wanted.audible_match.result = RuntimeError("Audible is down")
    b = db.get(_download()["id"])
    assert await Worker()._fill_before_filing(b, config.load()) == b


async def test_the_folder_is_named_with_the_details_found(audible, isolated_data):
    config.update({"fill_details_before_filing": True, "folder_template": "{author}/{series}/{title}"})
    b = db.get(_download()["id"])
    tmp = isolated_data / "tmp-dl"
    tmp.mkdir()
    (tmp / "01.mp3").write_bytes(b"audio")
    await Worker()._finish(b, tmp, config.load())
    done = db.get(b["id"])
    assert done["status"] == "completed"
    assert Path(done["path"]) == Path(config.load().library_dir).resolve() / "Frank Herbert" / "Dune Chronicles" / "Dune"
