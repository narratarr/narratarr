import functools
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from narratarr import alldebrid, api, config, db, worker
from narratarr.alldebrid import AllDebrid, AllDebridError, file_names, flatten
from narratarr.worker import Worker

HASH = "061850ead3eb6f1c5c6d8420211b4bbf2d4ee3e2"


def ok(data):
    return httpx.Response(200, json={"status": "success", "data": data})


def err(code, message="nope", status=200):
    return httpx.Response(status, json={"status": "error", "error": {"code": code, "message": message}})


def form(request: httpx.Request) -> dict:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


class FakeAD:
    """Simulates AllDebrid plus the host its direct links point at."""

    def __init__(self, files: dict[str, bytes], final_code=4, upload_error="", delayed=False):
        self.files = files
        self.final_code = final_code
        self.upload_error = upload_error
        self.delayed = delayed
        self.status_calls = 0
        self.magnet = ""
        self.deleted = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, host = request.url.path, request.url.host
        if host == "covers.example":
            return httpx.Response(200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"})
        if host == "dl.example":
            return httpx.Response(200, content=self.files[path.rsplit("/", 1)[-1]])
        assert request.headers["authorization"] == "Bearer ad-key"
        if path == "/v4/magnet/upload":
            if self.upload_error:
                return ok({"magnets": [{"magnet": "x", "error": {"code": self.upload_error, "message": "no"}}]})
            self.magnet = form(request)["magnets[]"]
            return ok({"magnets": [{"magnet": self.magnet, "hash": HASH, "id": 77, "ready": False}]})
        if path == "/v4.1/magnet/status":
            assert form(request)["id"] == "77"
            self.status_calls += 1
            if self.status_calls == 1:
                return ok({"magnets": {"id": 77, "statusCode": 1, "size": 200, "downloaded": 50, "downloadSpeed": 900, "seeders": 3}})
            return ok({"magnets": [{"id": 77, "statusCode": self.final_code, "status": "Ready"}]})
        if path == "/v4/magnet/files":
            tree = [{"n": "Dune", "e": [{"n": name, "s": len(data), "l": f"https://alldebrid.com/f/{name}"} for name, data in self.files.items()]}]
            return ok({"magnets": [{"id": "77", "files": tree}]})
        if path == "/v4/link/unlock":
            name = form(request)["link"].rsplit("/", 1)[-1]
            if self.delayed:
                return ok({"delayed": 5, "filename": name})
            return ok({"link": f"https://dl.example/{name}", "filename": name})
        if path == "/v4/link/delayed":
            name = next(iter(self.files))
            return ok({"status": 2, "time_left": 0, "link": f"https://dl.example/{name}"})
        if path == "/v4/magnet/delete":
            self.deleted = True
            return ok({"message": "Magnet was successfully deleted"})
        return err("404", status=404)


def make_book(**overrides):
    book = {
        "source": "audiobookbay",
        "source_url": "https://audiobookbay.lu/abss/dune-frank-herbert-2/",
        "title": "Dune",
        "author": "Frank Herbert",
        "cover_url": "https://covers.example/dune.jpg",
        "info_hash": HASH,
        "trackers": [],
        "status": "queued",
    }
    return db.insert({**book, **overrides})


@pytest.fixture(autouse=True)
def use_alldebrid(monkeypatch):
    monkeypatch.setattr(worker, "POLL_SECONDS", 0)
    monkeypatch.setattr(alldebrid, "DELAYED_POLL_SECONDS", 0)
    config.update({"torrent_client": "alldebrid", "ad_api_key": "ad-key"})


async def test_full_pipeline_through_alldebrid(isolated_data):
    fake = FakeAD({"01.mp3": b"one" * 100, "02.mp3": b"two" * 100})
    book = make_book()

    await Worker(transport=httpx.MockTransport(fake)).process(book["id"])

    done = db.get(book["id"])
    assert done["status"] == "completed", done["error"]
    dest = Path(done["path"])
    assert (dest / "01.mp3").read_bytes() == b"one" * 100  # the one folder everything sat in is dropped
    assert (dest / "02.mp3").read_bytes() == b"two" * 100
    assert HASH in fake.magnet and fake.deleted
    assert done["rd_torrent_id"] == ""


async def test_kept_job_is_tagged_and_resumed_only_on_alldebrid(isolated_data):
    config.update({"delete_from_ad_after": False})
    fake = FakeAD({"Dune.m4b": b"audio"})
    book = make_book()
    await Worker(transport=httpx.MockTransport(fake)).process(book["id"])
    assert db.get(book["id"])["rd_torrent_id"] == "ad:77"
    assert not fake.deleted

    # A Real-Debrid id left from before the switch is never sent to AllDebrid.
    db.update(book["id"], rd_torrent_id="RDTORRENT", status="queued")
    fake2 = FakeAD({"Dune.m4b": b"audio"})
    await Worker(transport=httpx.MockTransport(fake2)).process(book["id"])
    assert fake2.magnet  # added afresh


async def test_failed_magnet_says_why(isolated_data):
    book = make_book()
    await Worker(transport=httpx.MockTransport(FakeAD({"x.mp3": b""}, final_code=15)))._run(book["id"])
    failed = db.get(book["id"])
    assert failed["status"] == "failed"
    assert "no one is sharing it" in failed["error"]


@pytest.mark.parametrize(
    ("code", "words"),
    [("MAGNET_NO_SERVER", "data centre or VPN"), ("MAGNET_TOO_MANY_ACTIVE", "30 active"), ("MAGNET_MUST_BE_PREMIUM", "premium")],
)
async def test_upload_errors_are_plain_words(isolated_data, code, words):
    book = make_book()
    await Worker(transport=httpx.MockTransport(FakeAD({}, upload_error=code)))._run(book["id"])
    assert words in db.get(book["id"])["error"]


async def test_delayed_links_are_waited_for():
    fake = FakeAD({"Dune.m4b": b"audio"}, delayed=True)
    async with AllDebrid("ad-key", transport=httpx.MockTransport(fake)) as ad:
        files = await ad.direct_files("77")
    assert files == [{"filename": "Dune.m4b", "filesize": 5, "download": "https://dl.example/Dune.m4b"}]


async def test_new_location_and_bad_key_messages():
    for code, words in (("AUTH_BLOCKED", "emailed you a link"), ("AUTH_BAD_APIKEY", "rejected the API key")):
        transport = httpx.MockTransport(lambda r, code=code: err(code))
        async with AllDebrid("ad-key", transport=transport) as ad:
            with pytest.raises(AllDebridError, match=words):
                await ad.user()
    async with AllDebrid("ad-key", transport=httpx.MockTransport(lambda r: httpx.Response(429, text="slow down"))) as ad:
        with pytest.raises(AllDebridError, match="busy"):
            await ad.user()


async def test_missing_key_fails_with_helpful_message(isolated_data):
    # AllDebrid has to be the only candidate, or a configured entry wins the fall-through and this
    # stops testing what it says it tests. The list is set directly for that reason: the flat mirror
    # leaves earlier entries in place, so a stale Real-Debrid could otherwise take the grab.
    config.update({"clients": [{"id": "alldebrid", "type": "alldebrid", "api_key": ""}],
                   "default_client": "alldebrid"})
    book = make_book()
    await Worker(transport=httpx.MockTransport(FakeAD({})))._run(book["id"])
    # An unconfigured client is never handed the release any more (issue #12), so the error comes
    # from the router and names the client that isn't finished rather than from its constructor.
    error = db.get(book["id"])["error"]
    assert "AllDebrid" in error and "set up" in error


def test_file_names_keep_sub_folders():
    tree = [{"n": "Book", "e": [
        {"n": "CD1", "e": [{"n": "01.mp3", "s": 1, "l": "https://alldebrid.com/f/a"}]},
        {"n": "CD2", "e": [{"n": "01.mp3", "s": 2, "l": "https://alldebrid.com/f/b"}]},
        {"n": "empty", "e": []},
    ]}]
    entries = flatten(tree)
    assert [size for _, size, _ in entries] == [1, 2]
    assert file_names([p for p, _, _ in entries]) == ["CD1 - 01.mp3", "CD2 - 01.mp3"]
    assert file_names([("Book.m4b",)]) == ["Book.m4b"]


def test_settings_know_alldebrid(isolated_data):
    public = config.load().public()
    assert public["ad_configured"] and public["torrent_configured"]
    assert public["ad_api_key"].startswith("•") and public["ad_api_key"].endswith("-key")
    assert config.update({"torrent_client": "alldebrid"}).torrent_client == "alldebrid"


# ---- connecting with a PIN ----------------------------------------------------


class FakePin:
    def __init__(self, user_url="https://alldebrid.com/pin/?pin=K7Q2"):
        self.user_url = user_url
        self.checks = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v4.1/pin/get":
            return ok({"pin": "K7Q2", "check": "secret-check", "expires_in": 600, "user_url": self.user_url,
                       "base_url": "https://alldebrid.com/pin/"})
        if request.url.path == "/v4/pin/check":
            assert form(request) == {"pin": "K7Q2", "check": "secret-check"}
            self.checks += 1
            if self.checks == 1:
                return ok({"activated": False, "expires_in": 590})
            return ok({"activated": True, "apikey": "fresh-key", "expires_in": 580})
        if request.url.path == "/v4/user":
            assert request.headers["authorization"] == "Bearer fresh-key"
            return ok({"user": {"username": "desmond", "isPremium": True, "premiumUntil": 1893456000}})
        return err("404", status=404)


@pytest.fixture
def pin_client(monkeypatch):
    fake = FakePin()
    transport = httpx.MockTransport(fake)
    monkeypatch.setattr(alldebrid, "pin_start", functools.partial(alldebrid.pin_start, transport=transport))
    monkeypatch.setattr(alldebrid, "pin_check", functools.partial(alldebrid.pin_check, transport=transport))
    monkeypatch.setattr(api, "AllDebrid", functools.partial(AllDebrid, transport=transport))
    monkeypatch.setattr(api, "_ad_pin", {})
    config.update({"ad_api_key": "", "torrent_client": "realdebrid"})
    with TestClient(api.app) as c:
        c.app.state.worker.poke = lambda: None
        yield c, fake


def test_pin_connects_and_chooses_alldebrid(pin_client):
    client, _ = pin_client
    start = client.post("/api/settings/alldebrid/pin").json()
    assert start == {"pin": "K7Q2", "user_url": "https://alldebrid.com/pin/?pin=K7Q2", "expires_in": 600}
    assert "secret-check" not in str(start)  # the private half stays on the server

    assert client.post("/api/settings/alldebrid/pin/check", json={"pin": "K7Q2"}).json() == {"connected": False}
    done = client.post("/api/settings/alldebrid/pin/check", json={"pin": "K7Q2"}).json()
    assert done["connected"] and done["username"] == "desmond" and done["premium"]
    assert done["settings"]["ad_api_key"].startswith("•") and "fresh-key" not in str(done)

    s = config.load()
    assert s.ad_api_key == "fresh-key" and s.torrent_client == "alldebrid"
    # The code is used up.
    assert client.post("/api/settings/alldebrid/pin/check", json={"pin": "K7Q2"}).status_code == 400


def test_pin_check_needs_the_current_code(pin_client):
    client, fake = pin_client
    assert client.post("/api/settings/alldebrid/pin/check", json={"pin": "K7Q2"}).status_code == 400
    client.post("/api/settings/alldebrid/pin")
    assert client.post("/api/settings/alldebrid/pin/check", json={"pin": "XXXX"}).status_code == 400
    assert fake.checks == 0


async def test_pin_link_only_ever_points_at_alldebrid():
    transport = httpx.MockTransport(FakePin(user_url="https://evil.example/pin"))
    assert (await alldebrid.pin_start(transport=transport))["user_url"] == "https://alldebrid.com/pin/"
