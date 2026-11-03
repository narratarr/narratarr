"""Tidy library: moving the books already in the library into the folder layout from Settings."""

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mutagen.id3 import ID3, TALB, TDRC, TPE1, TXXX

from narratarr import api, auth, config, db, tidy

ABS = "{author}/{series}/Vol {series_number} - {year} - {title} {{narrator}}"
FRAMES = (b"\xff\xfb\x90\x64" + b"\x00" * 413) * 10  # ten silent MP3 frames


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    config.update({"folder_template": ABS})
    return root.resolve()


def put(root: Path, rel: str, files=("01.mp3",), content=b"audio") -> Path:
    folder = root / rel
    folder.mkdir(parents=True, exist_ok=True)
    for f in files:
        (folder / f).write_bytes(content)
    return folder


def book(path: Path, title="Dune", author="Frank Herbert", kind="audiobook", status="completed", **extra) -> dict:
    row = {"source": "local", "source_url": f"file://{path}#{time.time_ns()}", "title": title, "author": author,
           "narrators": ["Scott Brick"], "series": [{"asin": "", "title": "Dune Chronicles", "sequence": "1"}],
           "released": "1965-08-01", "status": status, "path": str(path), "kind": kind, **extra}
    return db.insert(row)


def rows(prefer="library", busy=frozenset()):
    return {r["id"]: r for r in tidy.preview(config.load(), prefer, busy)["books"]}


def test_a_messy_folder_moves_into_the_audiobookshelf_layout_and_back(lib):
    old = put(lib, "Dune (2019 rip) [64kbps]")
    inode = old.stat().st_ino
    b = book(old)
    r = rows()[b["id"]]
    assert r["state"] == "move"
    assert r["from"] == "Dune (2019 rip) [64kbps]"
    assert r["to"] == "Frank Herbert/Dune Chronicles/Vol 1 - 1965 - Dune {Scott Brick}"

    job = tidy.apply(config.load(), [b["id"]])
    new = lib / r["to"]
    assert job.moved == 1 and not job.failed
    assert db.get(b["id"])["path"] == str(new)
    assert (new / "01.mp3").read_bytes() == b"audio" and not old.exists()
    assert new.stat().st_ino == inode  # a rename, not a copy: Audiobookshelf knows it's the same book
    assert rows()[b["id"]]["state"] == "tidy"

    assert tidy.undo(config.load()) == {"back": 1, "failed": []}
    assert db.get(b["id"])["path"] == str(old) and (old / "01.mp3").exists()
    assert not (lib / "Frank Herbert").exists()  # the folders tidying made are gone again
    assert db.last_tidy_batch() == ("", [])


def test_an_ebook_inside_the_folder_moves_with_its_audiobook(lib):
    folder = put(lib, "dune audiobook", files=("01.mp3", "Dune.epub"))
    audio = book(folder)
    ebook = book(folder / "Dune.epub", kind="ebook")
    r = rows()
    assert ebook["id"] not in r  # not a move of its own
    assert r[audio["id"]]["carries"] == ["Dune"]
    tidy.apply(config.load(), [audio["id"]])
    assert db.get(ebook["id"])["path"] == str(lib / r[audio["id"]]["to"] / "Dune.epub")
    tidy.undo(config.load())
    assert db.get(ebook["id"])["path"] == str(folder / "Dune.epub")


def test_an_ebook_in_its_own_folder_moves_with_its_cover(lib):
    folder = put(lib, "misc/dune-epub", files=("Dune.epub", "cover.jpg", ".DS_Store"))
    b = book(folder / "Dune.epub", kind="ebook", series=[])
    config.update({"folder_template": "{author}/{title}"})
    r = rows()[b["id"]]
    assert r["to"] == "Frank Herbert/Dune/Dune.epub"
    tidy.apply(config.load(), [b["id"]])
    assert (lib / "Frank Herbert/Dune/cover.jpg").exists()
    assert not (lib / "misc").exists()  # emptied folders are cleared away, .DS_Store and all


def test_an_ebook_joins_its_audiobooks_new_folder(lib):
    audio = book(put(lib, "Dune audio"))
    ebook = book(put(lib, "Books/Herbert", files=("Dune.epub", "Other.pdf")) / "Dune.epub", kind="ebook")
    r = rows()
    assert r[ebook["id"]]["to"] == r[audio["id"]]["to"] + "/Dune.epub" and r[ebook["id"]]["state"] == "move"
    job = tidy.apply(config.load(), [audio["id"], ebook["id"]])
    assert job.moved == 2 and not job.failed
    assert Path(db.get(ebook["id"])["path"]).parent == Path(db.get(audio["id"])["path"])
    assert (lib / "Books/Herbert/Other.pdf").exists()  # only the book's own file moved

    tidy.undo(config.load())
    assert (lib / "Books/Herbert/Dune.epub").exists() and (lib / "Dune audio/01.mp3").exists()


def test_an_ebook_without_its_audiobook_stays_put(lib):
    audio = book(put(lib, "Dune audio"))
    ebook = book(put(lib, "Books", files=("Dune.epub", "x.pdf")) / "Dune.epub", kind="ebook")
    job = tidy.apply(config.load(), [ebook["id"]])
    assert job.moved == 0 and "audiobook" in job.failed[0]["error"]
    assert (lib / "Books/Dune.epub").exists() and db.get(audio["id"])["path"] == str(lib / "Dune audio")


def test_books_that_cant_move_safely_are_held_back(lib, monkeypatch):
    twin_a = book(put(lib, "a"), title="Twin")
    twin_b = book(put(lib, "b"), title="Twin")
    taken = book(put(lib, "c"), title="Taken", series=[])
    put(lib, rows()[taken["id"]]["to"], files=("someone-elses.mp3",))
    outer = book(put(lib, "Shelf"), title="Outer", author="Someone", series=[])
    inner = book(put(lib, "Shelf/Inner"), title="Inner", author="Someone", series=[])
    nobody = book(put(lib, "d"), title="Mystery", author="")
    busy = book(put(lib, "e"), title="Busy")
    gone = book(lib / "nope", title="Gone")
    pending = book(put(lib, "f"), title="Pending", status="downloading")
    r = rows(busy={busy["id"]})

    assert r[twin_a["id"]]["state"] == r[twin_b["id"]]["state"] == "held"
    assert "same folder" in r[twin_a["id"]]["reason"]
    assert r[taken["id"]]["state"] == "held" and "already there" in r[taken["id"]]["reason"]
    assert r[outer["id"]]["reason"] == "Another book is inside its folder"
    assert r[inner["id"]]["state"] == "move"  # out of another book's folder is fine; into one isn't
    assert r[nobody["id"]]["state"] == "look" and r[nobody["id"]]["reason"] == "It has no author"
    assert r[busy["id"]]["reason"] == "Narratarr is working on it right now"
    assert r[gone["id"]]["reason"] == "Its files aren't there any more"
    assert pending["id"] not in r

    # A book only another disk could take is held back: copying would cost its listening progress.
    lone = book(put(lib, "g"), title="Lone", series=[])
    monkeypatch.setattr(tidy, "_device", lambda p: 1 if "g" in p.parts else 2)
    assert "another disk" in rows()[lone["id"]]["reason"]

    # And the plan is worked out again when applying: asking for held books moves nothing.
    job = tidy.apply(config.load(), [twin_a["id"], outer["id"], nobody["id"]])
    assert job.total == 0 and (lib / "a/01.mp3").exists()
    assert {f["error"] for f in job.failed} == {"Another book would go to the same folder",
                                                 "It has no author", "Another book is inside its folder"}


def test_a_name_that_only_changes_case(lib):
    config.update({"folder_template": "{author}/{title}"})
    folder = put(lib, "frank herbert/dune")
    b = book(folder)
    assert rows()[b["id"]]["to"] == "Frank Herbert/Dune"
    job = tidy.apply(config.load(), [b["id"]])
    assert not job.failed
    assert [p.name for p in lib.iterdir()] == ["Frank Herbert"]
    assert [p.name for p in (lib / "Frank Herbert").iterdir()] == ["Dune"]
    assert db.get(b["id"])["path"] == str(lib / "Frank Herbert/Dune")


def test_a_book_straight_in_an_author_folder_moves_into_its_own(lib):
    config.update({"folder_template": "{author}/{title}"})
    b = book(put(lib, "Frank Herbert", files=("01.mp3", "02.mp3")))
    job = tidy.apply(config.load(), [b["id"]])
    assert not job.failed
    assert sorted(p.name for p in (lib / "Frank Herbert/Dune").iterdir()) == ["01.mp3", "02.mp3"]
    tidy.undo(config.load())
    assert sorted(p.name for p in (lib / "Frank Herbert").iterdir()) == ["01.mp3", "02.mp3"]


def test_a_file_already_in_the_way_stops_only_that_book(lib):
    config.update({"folder_template": "{author}/{title}"})
    a = book(put(lib, "x"), title="Alpha")
    b = book(put(lib, "y"), title="Beta")
    plan = rows()
    put(lib, "Frank Herbert/Beta", files=("intruder.txt",))  # appears after the preview
    job = tidy.apply(config.load(), [a["id"], b["id"]])
    assert job.moved == 1 and job.failed[0]["id"] == b["id"]
    assert db.get(b["id"])["path"] == str(lib / "y") and (lib / plan[a["id"]]["to"]).is_dir()


def tagged(path: Path, **frames) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(FRAMES)
    tags = ID3()
    for f in frames.values():
        tags.add(f)
    tags.save(path)
    return path


def test_the_files_own_tags_can_name_the_folder(lib):
    folder = lib / "messy"
    tagged(folder / "01.mp3", album=TALB(encoding=3, text=["Children of Dune"]), artist=TPE1(encoding=3, text=["Frank Herbert"]),
           series=TXXX(encoding=3, desc="SERIES", text=["Dune Chronicles"]), part=TXXX(encoding=3, desc="SERIES-PART", text=["3"]))
    b = book(folder, title="childrenofdune", author="", narrators=[], series=[], released="")
    assert rows()[b["id"]]["state"] == "look"
    r = rows("tags")[b["id"]]
    assert r["state"] == "move" and r["origin"] == "tags"
    assert r["to"] == "Frank Herbert/Dune Chronicles/Vol 3 - Children of Dune"


def test_writing_the_details_into_the_tags(lib):
    folder = lib / "book"
    f = tagged(folder / "01.mp3", artist=TPE1(encoding=3, text=["Scott Brick"]), year=TDRC(encoding=3, text=["1965-08-01"]))
    d = {"title": "Dune", "author": "Frank Herbert", "narrators": ["Scott Brick"],
         "series": [{"title": "Dune Chronicles", "sequence": "1"}], "released": "2019-01-01"}
    assert tidy.write_tags(folder, d) == 1
    tags = ID3(f)
    assert str(tags["TALB"]) == "Dune" and str(tags["TPE1"]) == "Frank Herbert" and str(tags["TPE2"]) == "Frank Herbert"
    assert str(tags["TCOM"]) == "Scott Brick" and str(tags["TXXX:SERIES"]) == "Dune Chronicles"
    assert str(tags["TXXX:SERIES-PART"]) == "1"
    assert str(tags["TDRC"]) == "1965-08-01"  # a date already there is left alone
    assert tidy.write_tags(folder, d) == 0  # nothing left to change, so the file isn't rewritten
    assert tidy.file_tags(folder)["series"][0]["title"] == "Dune Chronicles"  # and Narratarr reads them back


def test_side_files_are_added_only_where_missing(lib):
    folder = put(lib, "Dune", files=("01.mp3", "desc.txt"))
    (folder / "desc.txt").write_text("mine")
    tidy.side_files(folder, {"title": "Dune", "description": "theirs", "narrators": ["Scott Brick"], "cover_url": ""})
    assert (folder / "desc.txt").read_text() == "mine"
    assert (folder / "reader.txt").read_text() == "Scott Brick"


def test_prune_keeps_folders_with_anything_real_in_them(lib):
    keep = put(lib, "a/keep", files=("notes.txt",))
    empty = put(lib, "b/c", files=(".DS_Store", "Thumbs.db"))
    tidy.prune(keep, lib)
    tidy.prune(empty, lib)
    assert keep.exists() and not (lib / "b").exists()
    tidy.prune(lib, lib)
    assert lib.exists()


def test_moving_never_copies_across_disks(lib, monkeypatch):
    src = put(lib, "src")

    def cross(a, b):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(tidy.os, "rename", cross)
    with pytest.raises(tidy.TidyError, match="another disk"):
        tidy.rename(src, lib / "dst", lib)
    assert (src / "01.mp3").exists()


def test_the_api_previews_applies_and_undoes(lib):
    b = book(put(lib, "Dune [64k]"))
    with TestClient(api.app) as c:
        p = c.post("/api/tidy/preview", json={}).json()
        assert p["counts"]["move"] == 1 and p["template"] == ABS and p["undo"] == 0
        assert c.post("/api/tidy/apply", json={"books": []}).status_code == 400
        c.post("/api/tidy/apply", json={"books": [b["id"]], "sidecars": True})
        for _ in range(100):
            status = c.get("/api/tidy/status").json()
            if not status["running"]:
                break
            time.sleep(0.02)
        assert status["moved"] == 1 and status["undo"] == 1
        assert (lib / p["books"][0]["to"] / "reader.txt").exists()
        assert c.post("/api/tidy/undo").json()["back"] == 1
        assert db.get(b["id"])["path"] == str(lib / "Dune [64k]")


def test_only_admins_can_tidy():
    for method, path in [("POST", "/api/tidy/preview"), ("POST", "/api/tidy/apply"), ("GET", "/api/tidy/status"),
                         ("POST", "/api/tidy/undo")]:
        assert auth.admin_only(method, path)


@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="ffmpeg not installed")
@pytest.mark.parametrize("ext", ["m4b", "flac", "opus"])
def test_writing_tags_into_m4b_flac_and_opus(lib, ext):
    import subprocess

    folder = lib / ext
    folder.mkdir()
    f = folder / f"01.{ext}"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "1",
                    *(["-c:a", "aac", "-f", "mp4"] if ext == "m4b" else []), str(f)], check=True)
    d = {"title": "Dune", "author": "Frank Herbert", "narrators": ["Scott Brick"],
         "series": [{"title": "Dune Chronicles", "sequence": "1"}], "released": "1965"}
    assert tidy.write_tags(folder, d) == 1
    found = tidy.file_tags(folder)
    assert found["title"] == "Dune" and found["author"] == "Frank Herbert" and found["narrators"] == ["Scott Brick"]
    assert found["series"] == [{"asin": "", "title": "Dune Chronicles", "sequence": "1"}] and found["released"] == "1965"
    assert tidy.write_tags(folder, d) == 0
