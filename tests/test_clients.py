"""NZBGet, qBittorrent and Transmission, each driven through the real download pipeline against a fake."""

import base64
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from narratarr import config, db, worker
from narratarr.worker import Worker, link_into

HASH = "061850ead3eb6f1c5c6d8420211b4bbf2d4ee3e2"
NZB = b'<?xml version="1.0"?>\n<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb"></nzb>'


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(worker, "POLL_SECONDS", 0)


def torrent_book(**extra) -> dict:
    return db.insert({
        "source": "audiobookbay", "source_url": "https://audiobookbay.lu/abss/dune/", "title": "Dune",
        "author": "Frank Herbert", "info_hash": HASH, "status": "queued", **extra,
    })


def finished_download(root: Path) -> Path:
    folder = root / "downloads" / "Dune"
    folder.mkdir(parents=True)
    (folder / "Dune.m4b").write_bytes(b"audio")
    return folder


class FakeNZBGet:
    def __init__(self, dest: Path, history="SUCCESS/UNPACK"):
        self.dest, self.history, self.polls, self.appended, self.calls = dest, history, 0, None, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "prowlarr":
            return httpx.Response(200, content=NZB)
        assert request.headers["authorization"] == "Basic " + base64.b64encode(b"nzbget:secret").decode()
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        self.calls.append(method)
        result = None
        if method == "append":
            self.appended = params
            result = 42
        elif method == "listgroups":
            self.polls += 1
            result = [{"NZBID": 42, "Status": "DOWNLOADING", "FileSizeMB": 100, "RemainingSizeMB": 25}] if self.polls == 1 else []
        elif method == "status":
            result = {"DownloadRate": 5000, "DownloadPaused": False}
        elif method == "history":
            result = [{"NZBID": 42, "Status": self.history, "DestDir": str(self.dest), "FinalDir": ""}]
        elif method == "editqueue":
            result = True
        return httpx.Response(200, json={"result": result})


async def test_nzbget_pipeline(isolated_data):
    config.update({
        "usenet_client": "nzbget", "nzbget_url": "http://nzbget:6789", "nzbget_username": "nzbget",
        "nzbget_password": "secret", "nzbget_category": "Audiobooks",
        "prowlarr_url": "http://prowlarr:9696", "prowlarr_api_key": "K",
    })
    fake = FakeNZBGet(finished_download(isolated_data))
    book = db.insert({
        "source": "prowlarr", "source_url": "https://nzbs.example/1", "title": "Dune", "author": "Frank Herbert",
        "protocol": "usenet", "download_url": "http://prowlarr:9696/1/download?link=x", "status": "queued",
    })

    await Worker(transport=httpx.MockTransport(fake)).process(book["id"])

    done = db.get(book["id"])
    assert done["status"] == "completed", done["error"]
    assert (Path(done["path"]) / "Dune.m4b").read_bytes() == b"audio"
    name, content, category = fake.appended[:3]
    assert name == "Frank Herbert - Dune.nzb" and category == "Audiobooks"
    assert base64.b64decode(content) == NZB
    assert "editqueue" in fake.calls  # cleared from NZBGet's history


async def test_nzbget_failure(isolated_data):
    config.update({"usenet_client": "nzbget", "nzbget_url": "http://nzbget:6789", "nzbget_username": "nzbget",
                   "nzbget_password": "secret", "prowlarr_url": "http://prowlarr:9696", "prowlarr_api_key": "K"})
    book = db.insert({"source": "prowlarr", "source_url": "https://nzbs.example/2", "title": "Dune",
                      "protocol": "usenet", "download_url": "http://prowlarr:9696/x", "status": "queued"})
    await Worker(transport=httpx.MockTransport(FakeNZBGet(isolated_data, history="FAILURE/PAR")))._run(book["id"])
    failed = db.get(book["id"])
    assert failed["status"] == "failed" and "failure/par" in failed["error"]


class FakeQbit:
    def __init__(self, path: Path):
        self.path, self.logged_in, self.added, self.polls, self.deleted = path, False, None, 0, False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v2/auth/login":
            ok = parse_qs(request.content.decode()) == {"username": ["admin"], "password": ["pw"]}
            self.logged_in = ok
            # qBittorrent 5.x: 204 on success, 401 on failure
            return httpx.Response(204 if ok else 401, headers={"set-cookie": "SID=abc; path=/"} if ok else {})
        if not self.logged_in or "SID=abc" not in request.headers.get("cookie", ""):
            return httpx.Response(403)
        if path == "/api/v2/torrents/add":
            self.added = request.content
            return httpx.Response(200, text="Ok.")
        if path == "/api/v2/torrents/info":
            self.polls += 1
            done = self.polls > 2
            t = {"hash": HASH, "state": "uploading" if done else "downloading", "progress": 1 if done else 0.5,
                 "dlspeed": 1000, "num_seeds": 3, "content_path": str(self.path)}
            return httpx.Response(200, json=[t])
        if path == "/api/v2/torrents/delete":
            self.deleted = True
            return httpx.Response(200)
        return httpx.Response(404)


async def test_qbittorrent_keeps_seeding(isolated_data):
    config.update({"torrent_client": "qbittorrent", "qbittorrent_url": "http://qbit:8080",
                   "qbittorrent_username": "admin", "qbittorrent_password": "pw"})
    original = finished_download(isolated_data)
    fake = FakeQbit(original)
    book = torrent_book()

    await Worker(transport=httpx.MockTransport(fake)).process(book["id"])

    done = db.get(book["id"])
    assert done["status"] == "completed", done["error"]
    assert (Path(done["path"]) / "Dune.m4b").read_bytes() == b"audio"
    assert (original / "Dune.m4b").exists() and not fake.deleted  # still there to seed
    assert HASH.encode() in fake.added and b"narratarr-" in fake.added and b"audiobooks" in fake.added
    assert done["download_client"] == "qbittorrent" and done["client_job_id"] == HASH


async def test_qbittorrent_bad_password(isolated_data):
    config.update({"torrent_client": "qbittorrent", "qbittorrent_url": "http://qbit:8080",
                   "qbittorrent_username": "admin", "qbittorrent_password": "nope"})
    book = torrent_book()
    await Worker(transport=httpx.MockTransport(FakeQbit(isolated_data)))._run(book["id"])
    assert "password" in db.get(book["id"])["error"]


async def test_qbittorrent_old_style_login(isolated_data):
    from narratarr.qbittorrent import QBittorrent

    def handler(request):
        if request.url.path == "/api/v2/auth/login":
            return httpx.Response(200, text="Ok.", headers={"set-cookie": "SID=abc; path=/"})
        return httpx.Response(200, text="v4.6.7")

    async with QBittorrent("http://q:8080", "admin", "pw", transport=httpx.MockTransport(handler)) as q:
        assert await q.version() == "v4.6.7"


class FakeTransmission:
    def __init__(self, folder: Path):
        self.folder, self.polls, self.added = folder, 0, None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-transmission-session-id") != "S1":
            return httpx.Response(409, headers={"X-Transmission-Session-Id": "S1"})
        body = json.loads(request.content)
        if body["method"] == "torrent-add":
            self.added = body["arguments"]
            return httpx.Response(200, json={"result": "success", "arguments": {
                "torrent-added": {"hashString": HASH.upper(), "id": 1, "name": "Dune"}}})
        if body["method"] == "torrent-get":
            self.polls += 1
            done = self.polls > 1
            if body["arguments"].get("fields") == ["labels"]:
                return httpx.Response(200, json={"result": "success", "arguments": {"torrents": [{"labels": getattr(self, "labels", [])}]}})
            t = {"hashString": HASH, "name": self.folder.name, "status": 6 if done else 4,
                 "percentDone": 1 if done else 0.3, "leftUntilDone": 0 if done else 10, "rateDownload": 500,
                 "error": 0, "downloadDir": "/data/torrents", "metadataPercentComplete": 1, "peersSendingToUs": 2}
            return httpx.Response(200, json={"result": "success", "arguments": {"torrents": [t]}})
        if body["method"] == "torrent-set":
            self.labels = body["arguments"].get("labels", [])
            return httpx.Response(200, json={"result": "success", "arguments": {}})
        if body["method"] == "torrent-remove":
            self.removed = body["arguments"]
            return httpx.Response(200, json={"result": "success", "arguments": {}})
        return httpx.Response(200, json={"result": "method not recognized"})


async def test_transmission_with_path_mapping(isolated_data):
    original = finished_download(isolated_data)
    config.update({"torrent_client": "transmission", "transmission_url": "http://transmission:9091",
                   "path_remote": "/data/torrents", "path_local": str(original.parent)})
    fake = FakeTransmission(original)
    book = torrent_book()

    await Worker(transport=httpx.MockTransport(fake)).process(book["id"])

    done = db.get(book["id"])
    assert done["status"] == "completed", done["error"]
    assert fake.added["filename"].startswith(f"magnet:?xt=urn:btih:{HASH}")
    assert (original / "Dune.m4b").exists()


async def test_transmission_saves_to_its_folder_with_the_label(isolated_data):
    original = finished_download(isolated_data)
    config.update({"torrent_client": "transmission", "transmission_url": "http://transmission:9091",
                   "transmission_folder": "/data/torrents/books", "transmission_label": "books, audio",
                   "path_remote": "/data/torrents", "path_local": str(original.parent)})
    fake = FakeTransmission(original)
    book = torrent_book()

    await Worker(transport=httpx.MockTransport(fake)).process(book["id"])

    assert db.get(book["id"])["status"] == "completed"
    assert fake.added["download-dir"] == "/data/torrents/books"
    assert fake.labels == [f"narratarr-{book['id']}", "books  audio"]  # no commas: Transmission refuses them


async def test_transmission_defaults_leave_the_folder_alone(isolated_data):
    original = finished_download(isolated_data)
    config.update({"torrent_client": "transmission", "transmission_url": "http://transmission:9091",
                   "path_remote": "/data/torrents", "path_local": str(original.parent)})
    fake = FakeTransmission(original)
    book = torrent_book()

    await Worker(transport=httpx.MockTransport(fake)).process(book["id"])

    assert "download-dir" not in fake.added
    assert fake.labels == [f"narratarr-{book['id']}", "audiobooks"]


async def test_qbittorrent_folder_overrides_automatic_management(isolated_data):
    config.update({"torrent_client": "qbittorrent", "qbittorrent_url": "http://qbit:8080",
                   "qbittorrent_username": "admin", "qbittorrent_password": "pw",
                   "qbittorrent_folder": "/data/torrents/books"})
    original = finished_download(isolated_data)
    fake = FakeQbit(original)
    book = torrent_book()

    await Worker(transport=httpx.MockTransport(fake)).process(book["id"])

    sent = fake.added.decode()
    assert 'name="savepath"' in sent and "/data/torrents/books" in sent
    assert 'name="autoTMM"' in sent


class FakeDeluge:
    """Deluge 2.x, faithful where it matters.

    It rejects methods it does not recognise with an error object, the way a real server does, and
    labels only appear after `label.set_torrent` — `options["label"]` is accepted and silently
    dropped, exactly as on a real daemon. Both of those were real bugs that a friendlier fake hid.
    """

    def __init__(self, folder: Path, plugins=("Label",)):
        self.folder, self.plugins = folder, list(plugins)
        self.added, self.options, self.removed = None, None, None
        self.torrents: dict[str, dict] = {}
        self.labels: set[str] = set()
        self.calls: list[str] = []

    @staticmethod
    def _ok(result, body):
        return httpx.Response(200, json={"result": result, "error": None, "id": body["id"]})

    @staticmethod
    def _fail(message, body, code=2):
        return httpx.Response(200, json={"result": None, "error": {"message": message, "code": code}, "id": body["id"]})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        self.calls.append(method)
        if method == "auth.login":
            return self._ok(params == ["pw"], body)
        if method == "web.connected":
            return self._ok(True, body)
        if method == "core.get_enabled_plugins":
            return self._ok(self.plugins, body)
        if method == "daemon.get_version":
            return self._ok("2.2.0", body)
        if method == "core.add_torrent_magnet":
            self.added, self.options = params[0], params[1]
            self.torrents[HASH] = {"label": "", "polls": 0}
            return self._ok(HASH.upper(), body)  # upper case, as the daemon does
        if method == "label.add":
            if params[0] in self.labels:
                return self._fail("Label already exists", body, 4)
            self.labels.add(params[0])
            return self._ok(None, body)
        if method == "label.set_torrent":
            torrent = self.torrents.get(params[0])
            if torrent is None:
                return self._fail("Unknown torrent", body)
            torrent["label"] = params[1]
            return self._ok(None, body)
        if method == "core.get_torrent_status":
            torrent = self.torrents.get(params[0])
            if torrent is None:
                return self._ok({}, body)
            if params[1] == ["label"]:
                return self._ok({"label": torrent["label"]}, body)
            torrent["polls"] += 1
            done = torrent["polls"] > 1
            return self._ok({"name": self.folder.name, "download_location": "/data/torrents",
                             "state": "Seeding" if done else "Downloading", "progress": 100 if done else 30,
                             "is_finished": done, "download_payload_rate": 500, "message": ""}, body)
        if method == "core.remove_torrent":
            self.removed = params
            return self._ok(self.torrents.pop(params[0], None) is not None, body)
        return self._fail("Unknown method", body)


async def test_deluge_with_path_mapping(isolated_data):
    original = finished_download(isolated_data)
    config.update({"torrent_client": "deluge", "deluge_url": "http://deluge:8112", "deluge_password": "pw",
                   "path_remote": "/data/torrents", "path_local": str(original.parent)})
    fake = FakeDeluge(original)
    book = torrent_book()

    await Worker(transport=httpx.MockTransport(fake)).process(book["id"])

    done = db.get(book["id"])
    assert done["status"] == "completed", done["error"]
    assert fake.added.startswith(f"magnet:?xt=urn:btih:{HASH}")
    assert "label.set_torrent" in fake.calls  # labels are a plugin call, not an add option
    assert fake.torrents[HASH]["label"] == f"narratarr-{book['id']}"
    assert done["client_job_id"] == HASH  # Deluge's upper-case hash normalised
    assert (original / "Dune.m4b").exists()
    assert "download_location" not in fake.options


async def test_deluge_saves_to_its_folder(isolated_data):
    original = finished_download(isolated_data)
    config.update({"torrent_client": "deluge", "deluge_url": "http://deluge:8112", "deluge_password": "pw",
                   "deluge_folder": "/data/torrents/books",
                   "path_remote": "/data/torrents", "path_local": str(original.parent)})
    fake = FakeDeluge(original)
    book = torrent_book()

    await Worker(transport=httpx.MockTransport(fake)).process(book["id"])

    assert db.get(book["id"])["status"] == "completed"
    assert fake.options["download_location"] == "/data/torrents/books"


async def test_deluge_reports_an_rpc_error(isolated_data):
    """A failed call is a 200 with an error object, not an HTTP status."""

    def handler(request):
        body = json.loads(request.content)
        if body["method"] == "auth.login":
            return httpx.Response(200, json={"result": False, "error": {"message": "Bad login", "code": 1}, "id": 1})
        return httpx.Response(200, json={"result": None, "error": None, "id": 1})

    from narratarr.deluge import Deluge, DelugeError

    with pytest.raises(DelugeError):
        async with Deluge("http://deluge:8112", "wrong", transport=httpx.MockTransport(handler)) as d:
            await d.version()


async def test_deluge_removes_only_its_own_torrents(isolated_data):
    """The label is the proof of ownership: ours is removable, the user's is not."""
    from narratarr.deluge import Deluge

    theirs = "b" * 40
    fake = FakeDeluge(Path("/tmp"))
    fake.torrents = {HASH: {"label": "narratarr-7", "polls": 0}, theirs: {"label": "", "polls": 0}}

    async with Deluge("http://deluge:8112", "pw", transport=httpx.MockTransport(fake)) as d:
        await d.delete(HASH, tag="narratarr-7")
        assert HASH not in fake.torrents, "a torrent carrying our label should be removed"
        assert fake.removed == [HASH, True], "its files go too, because the label is ours"
        await d.delete(theirs, tag="narratarr-7")
        assert theirs in fake.torrents, "a torrent without our label must be left alone"
        assert fake.removed == [HASH, True], "and no remove call should have been made"


async def test_deluge_without_the_label_plugin_still_adds(isolated_data):
    """No Label plugin: no label calls, and delete() then refuses to remove anything."""
    from narratarr.deluge import Deluge

    fake = FakeDeluge(Path("/tmp"), plugins=())
    async with Deluge("http://deluge:8112", "pw", transport=httpx.MockTransport(fake)) as d:
        h = await d.add("magnet", f"magnet:?xt=urn:btih:{HASH}", tag="narratarr-9")
        assert h == HASH
        assert "label.add" not in fake.calls and "label.set_torrent" not in fake.calls
        await d.delete(h, tag="narratarr-9")
        assert HASH in fake.torrents, "with no label there is no proof of ownership, so nothing is removed"


def test_link_into_single_file_and_folder(tmp_path):
    src = tmp_path / "t"
    (src / "CD1").mkdir(parents=True)
    (src / "CD1" / "01.mp3").write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()
    link_into(src, out)
    link_into(src / "CD1" / "01.mp3", out)
    assert (out / "CD1" / "01.mp3").read_bytes() == b"x" and (out / "01.mp3").exists()
    assert (src / "CD1" / "01.mp3").exists()


def test_settings_carry_over_old_sabnzbd_path_mapping(isolated_data, monkeypatch):
    raw = json.loads(config.SETTINGS_FILE.read_text())
    raw.pop("path_remote"), raw.pop("path_local")
    raw.update({"sabnzbd_remote_path": "/downloads", "sabnzbd_local_path": "/mnt/usenet"})
    config.SETTINGS_FILE.write_text(json.dumps(raw))
    monkeypatch.setattr(config, "_settings", None)
    s = config.load()
    assert (s.path_remote, s.path_local) == ("/downloads", "/mnt/usenet")
    assert config.update({"torrent_client": "bogus"}).torrent_client == "realdebrid"
    pub = config.update({"torrent_client": "qbittorrent", "qbittorrent_url": "http://q", "qbittorrent_password": "secret1"}).public()
    assert pub["torrent_configured"] and "secret" not in pub["qbittorrent_password"]
