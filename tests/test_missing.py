"""Missing books: entries whose files went away, and what to do about them."""

import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from narratarr import api, auth, config, db, importer, wanted


@pytest.fixture
def client():
    with TestClient(api.app) as c:
        yield c


def add_book(root: Path, name: str, title: str | None = None, author: str = "") -> dict:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "01.mp3").write_bytes(b"audio" * 10)
    return db.insert({
        "source": "local", "source_url": importer.url_for(folder), "title": title or name,
        "author": author, "path": str(folder), "status": "completed", "progress": 100,
        "import_mode": "inplace", "added_at": time.time(), "updated_at": time.time(),
    })


def test_missing_scan_finds_gone_folders(client, isolated_data):
    lib = Path(config.load().library_dir)
    gone = add_book(lib, "Dune", "Dune", "Frank Herbert")
    kept = add_book(lib, "The Hobbit")
    db.insert({"source": "audible", "source_url": "https://x/y", "title": "Wanted Book",
               "status": "wanted", "added_at": time.time(), "updated_at": time.time()})

    shutil.rmtree(lib / "Dune")
    missing = client.get("/api/library/missing").json()["books"]
    ids = [b["id"] for b in missing]
    assert gone["id"] in ids and kept["id"] not in ids
    assert all(b["status"] != "wanted" for b in missing)


def test_missing_scan_finds_emptied_folders(client, isolated_data):
    lib = Path(config.load().library_dir)
    book = add_book(lib, "Dune")
    (lib / "Dune" / "01.mp3").unlink()
    (lib / "Dune" / "cover.jpg").write_bytes(b"img")
    assert book["id"] in [b["id"] for b in client.get("/api/library/missing").json()["books"]]


def test_mark_missing_and_wanted(client, isolated_data):
    lib = Path(config.load().library_dir)
    book = add_book(lib, "Dune", "Dune", "Frank Herbert")
    shutil.rmtree(lib / "Dune")

    r = client.post(f"/api/library/{book['id']}/mark-missing")
    assert r.status_code == 200 and r.json()["status"] == "missing"
    assert db.get(book["id"])["path"] == str(lib / "Dune")  # where it was stays on record

    # A missing book no longer counts as owned, and drops out of later scans.
    assert wanted.in_library("", "Dune", "Frank Herbert") is None
    assert client.get("/api/library/missing").json()["books"] == []

    r = client.post(f"/api/library/{book['id']}/mark-wanted")
    assert r.status_code == 200 and r.json()["status"] == "wanted"
    assert db.get(book["id"])["path"] == ""


def test_import_brings_back_a_missing_book(client, isolated_data):
    src = isolated_data / "shelf"
    book = add_book(src, "Dune")
    client.post(f"/api/library/{book['id']}/mark-missing")

    r = client.post("/api/import", json={"folder": str(src), "mode": "inplace",
                                         "books": [{"path": str(src / "Dune")}]})
    assert r.json()["added"] == 1
    again = db.get(book["id"])
    assert again["status"] == "completed" and again["progress"] == 100
    assert len(db.list_books()) == 1  # the same entry came back; no duplicate was made


def test_listeners_cant_reconcile(client, monkeypatch):
    listener = {"id": 2, "username": "lis", "role": "listener", "created_at": 0}
    monkeypatch.setattr(auth, "current_user", lambda request: listener)
    assert client.get("/api/library/missing").status_code == 403
    assert client.post("/api/library/1/mark-missing").status_code == 403
    assert client.post("/api/library/1/mark-wanted").status_code == 403
