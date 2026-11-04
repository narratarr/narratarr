import io
import zipfile
from pathlib import Path

import httpx
import pytest

from narratarr import config, db, worker
from narratarr.worker import Worker, destination, extract_zip, safe_name

HASH = "061850ead3eb6f1c5c6d8420211b4bbf2d4ee3e2"


def make_book(**overrides):
    book = {
        "source": "audiobookbay",
        "source_url": "https://audiobookbay.lu/abss/dune-frank-herbert-2/",
        "title": "Dune",
        "author": "Frank Herbert",
        "narrators": ["Scott Brick"],
        "cover_url": "https://covers.example/dune.jpg",
        "description": "Set on the desert planet Arrakis.",
        "info_hash": HASH,
        "trackers": ["udp://tracker.opentrackr.org:1337/announce"],
        "status": "queued",
    }
    return db.insert({**book, **overrides})


class FakeRD:
    """Simulates Real-Debrid plus the file host it hands out links to."""

    def __init__(self, files: dict[str, bytes], final_status="downloaded"):
        self.files = files
        self.final_status = final_status
        self.info_calls = 0
        self.selected = False
        self.deleted = False
        self.magnet = ""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, host = request.url.path, request.url.host
        if host == "covers.example":
            return httpx.Response(200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"})
        if host == "dl.example":
            return httpx.Response(200, content=self.files[path.strip("/")])
        assert request.headers["authorization"] == "Bearer test-key"
        if path.endswith("/torrents/addMagnet"):
            self.magnet = request.content.decode()
            return httpx.Response(201, json={"id": "TID", "uri": "x"})
        if path.endswith("/torrents/info/TID"):
            self.info_calls += 1
            if self.info_calls == 1:
                return httpx.Response(200, json={"status": "waiting_files_selection", "progress": 0})
            if self.info_calls == 2:
                return httpx.Response(200, json={"status": "downloading", "progress": 50, "speed": 1000, "seeders": 4})
            links = [f"https://real-debrid.com/d/{name}" for name in self.files]
            return httpx.Response(200, json={"status": self.final_status, "progress": 100, "links": links})
        if path.endswith("/torrents/selectFiles/TID"):
            self.selected = True
            return httpx.Response(204)
        if path.endswith("/unrestrict/link"):
            name = request.content.decode().rsplit("%2F", 1)[-1]
            return httpx.Response(
                200, json={"filename": name, "filesize": len(self.files[name]), "download": f"https://dl.example/{name}"}
            )
        if path.endswith("/torrents/delete/TID"):
            self.deleted = True
            return httpx.Response(204)
        return httpx.Response(404, json={"error": "unknown"})


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    monkeypatch.setattr(worker, "POLL_SECONDS", 0)


async def test_full_pipeline_downloads_and_organizes(isolated_data):
    fake = FakeRD({"Dune.m4b": b"audio" * 1000})
    book = make_book()

    await Worker(transport=httpx.MockTransport(fake)).process(book["id"])

    done = db.get(book["id"])
    assert done["status"] == "completed", done["error"]
    dest = Path(done["path"])
    assert dest == isolated_data / "library" / "Frank Herbert" / "Dune"
    assert (dest / "Dune.m4b").read_bytes() == b"audio" * 1000
    assert (dest / "desc.txt").read_text() == "Set on the desert planet Arrakis."
    assert (dest / "reader.txt").read_text() == "Scott Brick"
    assert (dest / "cover.jpg").exists()
    assert fake.selected and fake.deleted
    assert HASH in fake.magnet
    scratch = isolated_data / "library" / ".narratarr-tmp"
    assert not scratch.exists() or not any(scratch.iterdir())


async def test_zip_from_real_debrid_is_extracted(isolated_data):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Dune/01.mp3", b"one")
        zf.writestr("Dune/02.mp3", b"two")
    book = make_book()

    await Worker(transport=httpx.MockTransport(FakeRD({"Dune.zip": buf.getvalue()}))).process(book["id"])

    dest = Path(db.get(book["id"])["path"])
    assert (dest / "Dune" / "01.mp3").read_bytes() == b"one"
    assert not (dest / "Dune.zip").exists()


async def test_dead_torrent_fails_cleanly(isolated_data):
    book = make_book()
    w = Worker(transport=httpx.MockTransport(FakeRD({"x.m4b": b""}, final_status="dead")))

    await w._run(book["id"])

    failed = db.get(book["id"])
    assert failed["status"] == "failed"
    assert "dead" in failed["error"]


async def test_missing_api_key_fails_with_helpful_message(isolated_data):
    config.update({"rd_api_key": ""})
    book = make_book()

    await Worker(transport=httpx.MockTransport(FakeRD({})))._run(book["id"])

    # The router refuses to hand a release to a client with no key, and says which one to finish.
    error = db.get(book["id"])["error"]
    assert "Real-Debrid" in error and "set up" in error


def test_destination_template_and_sanitizing(isolated_data):
    s = config.load()
    book = {"author": 'AC/DC: "Live"', "title": "../../etc", "narrators": ["Jim Dale"]}
    dest = destination(book, s)
    root = Path(s.library_dir).resolve()
    assert root in dest.parents
    assert dest.parent.name == "ACDC Live"

    s.folder_template = "{author}/{narrator}/{title}"
    assert destination({**book, "title": "Dune"}, s).parent.name == "Jim Dale"

    s.folder_template = "{nope}"
    assert destination({**book, "title": "Dune"}, s).name == "Dune"


def test_zip_slip_is_rejected(tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escape.txt", b"nope")
    with pytest.raises(ValueError):
        extract_zip(archive, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def test_safe_name():
    assert safe_name('a<b>c:"d"/e\\f|g?h*') == "abcdefgh"
    assert safe_name("   ") == "Unknown"
