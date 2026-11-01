"""Listener requests wait for an admin (issue #6), and listeners can take back their own requests (issue #7)."""

import pytest
from fastapi.testclient import TestClient

from narratarr import api, auth, config, db, notify, users, wanted

DUNE = {"url": "https://www.audible.com/pd/B002V1OF70", "asin": "B002V1OF70", "title": "Dune", "author": "Frank Herbert",
        "narrators": ["Scott Brick"], "cover_url": "", "description": "", "language": "English", "series": [],
        "released": "1965-08-01", "length": "21 hrs"}


@pytest.fixture
def people():
    return users.create("boss", "a-long-password", "admin"), users.create("kid", "listen-only", "listener")


@pytest.fixture
def setup(monkeypatch, people):
    """A client whose signed-in person can be switched, with searching and announcing recorded, not done."""
    admin, kid = people
    who = {"user": kid}
    monkeypatch.setattr(auth, "current_user", lambda request: who["user"])
    searched, events = [], []

    async def fake_search(book_id):
        searched.append(book_id)

    async def fake_catalog(url, source):
        return DUNE

    monkeypatch.setattr(api, "_search_in_background", fake_search)
    monkeypatch.setattr(api, "_catalog_book", fake_catalog)
    monkeypatch.setattr(notify, "emit", lambda event, book, username="", error="": events.append(
        (event, book["status"], username or "")))
    with TestClient(api.app) as c:
        poked = []
        c.app.state.worker.poke = lambda: poked.append(1)
        yield {"c": c, "who": who, "admin": admin, "kid": kid, "searched": searched, "events": events, "poked": poked}


def test_a_listeners_request_waits_for_an_admin(setup):
    c, s = setup["c"], setup
    book = c.post("/api/library", json={"url": DUNE["url"], "source": "audible", "download": True}).json()
    assert book["status"] == "requested" and book["requested_by"] == s["kid"]["id"]
    assert s["searched"] == [] and s["poked"] == []  # nothing looked for, nothing downloaded
    assert s["events"] == [("wanted", "requested", "")]  # "kid wants Dune… waiting for your approval"
    assert "waiting for your approval" in notify.context("wanted", db.get(book["id"]))["message"]

    # Asking again doesn't skip the queue, and auto search leaves it alone.
    assert c.post(f"/api/library/{book['id']}/download").json()["status"] == "requested"
    assert book["id"] not in [b["id"] for b in wanted.due_for_search()]

    # Only an admin approves; then Narratarr starts looking.
    assert c.post(f"/api/library/{book['id']}/approve").status_code == 403
    s["who"]["user"] = s["admin"]
    assert c.post(f"/api/library/{book['id']}/approve").json()["status"] == "wanted"
    assert s["searched"] == [book["id"]]


def test_a_listeners_picked_release_is_kept_for_approval(setup, monkeypatch):
    c, s = setup["c"], setup
    row = db.insert({"source": "audible", "source_url": DUNE["url"], "title": "Dune", "author": "Frank Herbert",
                     "status": "wanted"})

    async def fake_grab(book_id, url, source, status="queued"):
        db.update(book_id, source="prowlarr", release_url=url, status=status)
        return db.get(book_id)

    monkeypatch.setattr(wanted, "grab", fake_grab)
    monkeypatch.setattr(api, "_resolve", lambda url, source: None)
    got = c.post(f"/api/library/{row['id']}/grab", json={"url": "https://prowlarr.example/r/1", "source": "prowlarr"}).json()
    assert got["status"] == "requested" and got["release_url"] == "https://prowlarr.example/r/1"
    assert s["poked"] == [] and s["events"] == [("wanted", "requested", "kid")]

    s["who"]["user"] = s["admin"]
    assert c.post(f"/api/library/{row['id']}/approve").json()["status"] == "queued"  # exactly that release
    assert s["poked"] == [1]


def test_admins_can_turn_approval_off(setup):
    c, s = setup["c"], setup
    config.update({"listener_approval": False})
    book = c.post("/api/library", json={"url": DUNE["url"], "source": "audible", "download": True}).json()
    assert book["status"] == "wanted" and s["searched"] == [book["id"]]


def test_admin_requests_never_wait(setup):
    c, s = setup["c"], setup
    s["who"]["user"] = s["admin"]
    book = c.post("/api/library", json={"url": DUNE["url"], "source": "audible", "download": True}).json()
    assert book["status"] == "wanted" and s["searched"] == [book["id"]]


def test_following_is_for_admins_while_requests_need_approval(setup):
    c = setup["c"]
    r = c.post("/api/follows", json={"kind": "author", "asin": "B000APZOQA", "name": "Frank Herbert"})
    assert r.status_code == 403 and "admin" in r.json()["detail"]


def test_listeners_remove_only_their_own_requests(setup):
    c, s = setup["c"], setup
    mine = db.insert({"source": "audible", "source_url": "https://www.audible.com/pd/A", "title": "A", "status": "requested",
                      "requested_by": s["kid"]["id"]})
    theirs = db.insert({"source": "audible", "source_url": "https://www.audible.com/pd/B", "title": "B", "status": "wanted",
                        "requested_by": s["admin"]["id"]})
    done = db.insert({"source": "audible", "source_url": "https://www.audible.com/pd/C", "title": "C",
                      "status": "completed", "requested_by": s["kid"]["id"]})
    assert c.delete(f"/api/library/{theirs['id']}").status_code == 403
    assert c.delete(f"/api/library/{done['id']}").status_code == 403  # it's in the library now: an admin's call
    assert c.delete(f"/api/library/{mine['id']}").status_code == 200 and db.get(mine["id"]) is None

    s["who"]["user"] = s["admin"]
    assert c.delete(f"/api/library/{theirs['id']}").status_code == 200


def test_listeners_see_whether_approval_is_on(setup):
    assert setup["c"].get("/api/settings").json()["listener_approval"] is True
