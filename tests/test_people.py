"""Who wrote it and who reads it: from file tags on import, and corrected by hand."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mutagen.id3 import ID3, TALB, TCOM, TPE1, TPE2, TXXX

from narratarr import api, auth, db
from narratarr.importer import pick_people, read_meta

# Ten silent MPEG-1 Layer III frames (128 kbps, 44.1 kHz): enough for mutagen to call it an MP3.
FRAMES = (b"\xff\xfb\x90\x64" + b"\x00" * 413) * 10


def tagged_mp3(path: Path, **frames) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(FRAMES)
    tags = ID3()
    for frame in frames.values():
        tags.add(frame)
    tags.save(path)
    return path


@pytest.mark.parametrize(
    ("artist", "albumartist", "narrator", "author", "reader"),
    [
        ("Frank Herbert", "Scott Brick", "Scott Brick", "Frank Herbert", "Scott Brick"),  # the reported case
        ("Scott Brick", "Frank Herbert", "Scott Brick", "Frank Herbert", "Scott Brick"),
        ("Frank Herbert, Scott Brick", "", "Scott Brick", "Frank Herbert", "Scott Brick"),
        ("Douglas Preston & Lincoln Child", "", "", "Douglas Preston & Lincoln Child", ""),
        ("Stephen King, read by Frank Muller", "", "", "Stephen King", "Frank Muller"),
        ("Stephen King (Narrated by Will Patton)", "", "", "Stephen King", "Will Patton"),
        ("", "Brandon Sanderson", "Michael Kramer; Kate Reading", "Brandon Sanderson", "Michael Kramer; Kate Reading"),
        ("Trevor Noah", "Trevor Noah", "Trevor Noah", "Trevor Noah", "Trevor Noah"),  # read by its author
    ],
)
def test_pick_people(artist, albumartist, narrator, author, reader):
    assert pick_people(artist, albumartist, narrator) == (author, reader)


def test_album_artist_narrator_is_not_the_author(tmp_path):
    """Artist first, as Audiobookshelf reads it; the narrator in Album artist used to win."""
    book = tmp_path / "in" / "Dune"
    tagged_mp3(book / "01.mp3", album=TALB(text="Dune"), artist=TPE1(text="Frank Herbert"),
               albumartist=TPE2(text="Scott Brick"), composer=TCOM(text="Scott Brick"))

    meta = read_meta(book, tmp_path / "in")

    assert meta["author"] == "Frank Herbert"
    assert meta["narrators"] == ["Scott Brick"]


def test_narrator_tag_is_read(tmp_path):
    """A NARRATOR tag (no composer) still keeps the narrator out of the author."""
    book = tmp_path / "in" / "Project Hail Mary"
    tagged_mp3(book / "01.mp3", album=TALB(text="Project Hail Mary"), artist=TPE1(text="Ray Porter"),
               albumartist=TPE2(text="Andy Weir"), narrator=TXXX(desc="NARRATOR", text="Ray Porter"))

    meta = read_meta(book, tmp_path / "in")

    assert meta["author"] == "Andy Weir"
    assert meta["narrators"] == ["Ray Porter"]


# ---- correcting it by hand ----------------------------------------------------


@pytest.fixture
def client():
    with TestClient(api.app) as c:
        c.app.state.worker.poke = lambda: None
        yield c


def local_book():
    return db.insert({"source": "local", "source_url": "local:///x", "title": "Dune", "author": "Scott Brick",
                      "narrators": [], "status": "completed"})


def test_edit_details(client):
    book = local_book()
    out = client.patch(f"/api/library/{book['id']}", json={
        "title": "  Dune  ", "author": "Frank  Herbert", "narrators": ["Scott Brick", " ", "Simon Vance"]}).json()
    assert (out["title"], out["author"], out["narrators"]) == ("Dune", "Frank Herbert", ["Scott Brick", "Simon Vance"])
    assert db.get(book["id"])["author"] == "Frank Herbert"


def test_edit_needs_a_title_and_a_book(client):
    book = local_book()
    assert client.patch(f"/api/library/{book['id']}", json={"title": "   "}).status_code == 400
    assert client.patch("/api/library/9999", json={"author": "x"}).status_code == 404
    # Only what was sent changes.
    client.patch(f"/api/library/{book['id']}", json={"author": "Frank Herbert"})
    assert db.get(book["id"])["title"] == "Dune"


def test_only_admins_edit():
    assert auth.admin_only("PATCH", "/api/library/3")
    assert auth.admin_only("DELETE", "/api/library/3")
    assert not auth.admin_only("PUT", "/api/library/3/progress")
