import shutil
import struct
import subprocess
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from narratarr import api, db, media


def write_wav(path: Path, seconds: float, rate: int = 8000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<h", 0) * int(seconds * rate))


@pytest.fixture
def book_dir(isolated_data):
    d = isolated_data / "library" / "Frank Herbert" / "Dune"
    # Natural sort must put Part 2 before Part 10.
    write_wav(d / "Part 10.wav", 1.0)
    write_wav(d / "Part 2.wav", 2.0)
    write_wav(d / "Part 1.wav", 1.5)
    (d / "cover.jpg").write_bytes(b"\xff\xd8jpeg")
    (d / "desc.txt").write_text("not audio")
    return d


@pytest.fixture
def client():
    with TestClient(api.app) as c:
        yield c


def completed_book(path: Path) -> dict:
    return db.insert(
        {
            "source": "audiobookbay",
            "source_url": "https://audiobookbay.lu/abss/dune/",
            "title": "Dune",
            "status": "completed",
            "path": str(path),
        }
    )


def test_describe_multi_file_book(book_dir):
    info = media.describe(book_dir)
    assert [f["name"] for f in info["files"]] == ["Part 1.wav", "Part 2.wav", "Part 10.wav"]
    assert [round(f["offset"], 2) for f in info["files"]] == [0, 1.5, 3.5]
    assert round(info["duration"], 2) == 4.5
    # One chapter per file when files carry no chapter markers.
    assert [round(c["start"], 2) for c in info["chapters"]] == [0, 1.5, 3.5]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_describe_reads_m4b_chapters(tmp_path):
    meta = tmp_path / "chapters.txt"
    meta.write_text(
        ";FFMETADATA1\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=Opening\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=1000\nEND=2000\ntitle=The Spice\n"
    )
    out = tmp_path / "book" / "Dune.m4b"
    out.parent.mkdir()
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=22050:cl=mono",
            "-i",
            str(meta),
            "-map_metadata",
            "1",
            "-t",
            "2",
            "-c:a",
            "aac",
            "-f",
            "mp4",
            str(out),
        ],
        check=True,
        timeout=60,
    )
    info = media.describe(out.parent)
    assert [c["title"] for c in info["chapters"]] == ["Opening", "The Spice"]
    assert info["files"][0]["type"] == "audio/mp4"
    # ffmpeg writes a QuickTime chapter track too (the format LibriVox uses); read it directly.
    assert [(round(s), title) for s, title in media.qt_chapters(out)] == [
        (0, "Opening"),
        (1, "The Spice"),
    ]


def test_qt_chapters_tolerates_non_mp4_files(tmp_path):
    junk = tmp_path / "x.m4b"
    junk.write_bytes(b"not really an mp4 file at all")
    assert media.qt_chapters(junk) == []


def test_media_and_stream_endpoints(client, book_dir):
    book = completed_book(book_dir)
    info = client.get(f"/api/library/{book['id']}/media").json()
    assert len(info["files"]) == 3

    full = client.get(f"/api/library/{book['id']}/stream/1")
    assert full.status_code == 200
    assert full.headers["content-type"] == "audio/wav"

    ranged = client.get(f"/api/library/{book['id']}/stream/1", headers={"Range": "bytes=0-99"})
    assert ranged.status_code == 206
    assert len(ranged.content) == 100

    assert client.get(f"/api/library/{book['id']}/stream/9").status_code == 404
    assert client.get(f"/api/library/{book['id']}/cover").content == b"\xff\xd8jpeg"


def test_media_requires_downloaded_book(client):
    book = db.insert(
        {
            "source": "audiobookbay",
            "source_url": "https://audiobookbay.lu/abss/x/",
            "title": "X",
            "status": "wanted",
        }
    )
    assert client.get(f"/api/library/{book['id']}/media").status_code == 404


def test_progress_is_saved_and_finishing_resets_position(client, book_dir):
    book = completed_book(book_dir)
    mine = lambda: next(b for b in client.get("/api/library").json() if b["id"] == book["id"])  # noqa: E731
    client.put(f"/api/library/{book['id']}/progress", json={"position": 1234.5, "duration": 5000})
    saved = mine()
    assert saved["listen_position"] == 1234.5
    assert saved["listen_duration"] == 5000
    assert saved["finished"] is False

    client.put(f"/api/library/{book['id']}/progress", json={"position": 4999, "finished": True})
    done = mine()
    assert done["finished"] is True
    assert done["listen_position"] == 0


def test_old_databases_are_migrated(isolated_data):
    with db.connect() as conn:
        conn.execute("DROP TABLE books")
        conn.executescript(db.SCHEMA)  # original schema, without listening columns
    db.init()
    with db.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(books)")}
    assert {"listen_position", "listen_duration", "listened_at", "finished"} <= cols
