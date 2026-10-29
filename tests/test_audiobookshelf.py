"""Audiobookshelf: finding the server, signing in once for an API key, and rescans."""

import ipaddress
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from narratarr import api, audiobookshelf, config, db
from narratarr.audiobookshelf import Audiobookshelf, AudiobookshelfError
from narratarr.worker import Worker

KEY = "abs-api-key-for-narratarr"


class FakeABS:
    """Audiobookshelf 2.26+ on one or more addresses. Set `api_keys=False` to be an older server."""

    def __init__(self, at=("http://abs:13378",), user_type="admin", api_keys=True, libraries=None):
        self.at, self.user_type, self.api_keys = set(at), user_type, api_keys
        self.libraries = libraries if libraries is not None else [
            {"id": "lib-books", "name": "Audiobooks", "mediaType": "book", "folders": [{"fullPath": "/audiobooks"}]},
            {"id": "lib-pods", "name": "Podcasts", "mediaType": "podcast", "folders": [{"fullPath": "/podcasts"}]},
        ]
        self.key_requests, self.scans, self.logins = [], [], []
        self.items = {}  # library id -> [(path, relPath)], as Audiobookshelf found them

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        base = next((a for a in self.at if url.startswith(a + "/")), None)
        if base is None:
            raise httpx.ConnectError("nothing listening", request=request)
        path = url[len(base):]
        auth = request.headers.get("authorization", "")
        if path == "/status":
            return httpx.Response(200, json={"app": "audiobookshelf", "serverVersion": "2.30.0", "isInit": True,
                                             "authMethods": ["local"]})
        if path == "/login":
            body = json.loads(request.content)
            self.logins.append(body)
            if body != {"username": "root", "password": "hunter2"}:
                return httpx.Response(401, text="Unauthorized")
            return httpx.Response(200, json={"user": {"id": "u1", "type": self.user_type, "accessToken": "short-lived",
                                                      "token": "legacy-token"}})
        if path == "/api/api-keys" and request.method == "POST":
            if not self.api_keys:
                return httpx.Response(404, text="Not Found")
            assert auth == "Bearer short-lived"
            self.key_requests.append(json.loads(request.content))
            return httpx.Response(200, json={"apiKey": {"apiKey": KEY, "id": "k1", "name": "Narratarr"}})
        if auth not in (f"Bearer {KEY}", "Bearer legacy-token"):
            return httpx.Response(401, text="Unauthorized")
        if path == "/api/libraries":
            return httpx.Response(200, json={"libraries": self.libraries})
        if path.startswith("/api/libraries/") and path.endswith("/scan"):
            library_id = path.split("/")[3]
            if library_id not in {lib["id"] for lib in self.libraries}:
                return httpx.Response(404, text="Not Found")
            self.scans.append(library_id)
            return httpx.Response(200, text="OK")
        if request.url.path.startswith("/api/libraries/") and request.url.path.endswith("/items"):
            found = self.items.get(request.url.path.split("/")[3], [])
            limit, page = int(request.url.params["limit"]), int(request.url.params["page"])
            chunk = found[page * limit:(page + 1) * limit]
            return httpx.Response(200, json={"total": len(found), "limit": limit, "page": page, "results": [
                {"id": f"i{n}", "path": path, "relPath": rel, "isMissing": False, "media": {}} for n, (path, rel) in enumerate(chunk)]})
        return httpx.Response(404, text="Not Found")


@pytest.fixture
def fake(monkeypatch):
    server = FakeABS()
    monkeypatch.setattr(audiobookshelf, "transport", httpx.MockTransport(server))
    return server


@pytest.fixture
def client(fake):
    with TestClient(api.app) as c:
        c.app.state.worker.poke = lambda: None
        yield c


# ---- finding ---------------------------------------------------------------------


def test_addresses_for_a_bare_host_tries_https_first_then_ports_and_a_proxy_path():
    urls = audiobookshelf.addresses_for("abs.example.com")
    assert urls[0] == "https://abs.example.com"
    assert urls.index("https://abs.example.com:13378") < urls.index("http://abs.example.com")
    assert "https://abs.example.com/audiobookshelf" in urls


def test_addresses_for_keeps_what_was_spelled_out():
    assert audiobookshelf.addresses_for("http://203.0.113.7:8080/abs/") == ["http://203.0.113.7:8080/abs"]
    ipv6 = audiobookshelf.addresses_for("[2001:db8::1]:13378")
    assert ipv6[0] == "https://[2001:db8::1]:13378" and "http://[2001:db8::1]:13378/audiobookshelf" in ipv6
    assert not any(":13378:" in u or u.count(":13378") > 1 for u in ipv6)  # the port given is the only one tried
    assert audiobookshelf.addresses_for("  ") == []


@pytest.mark.parametrize("url, public", [
    ("http://192.168.1.20:13378", False),
    ("http://localhost:13378", False),
    ("http://audiobookshelf", False),
    ("http://nas.local:13378", False),
    ("http://203.0.113.7:13378", False),  # documentation range, not routable
    ("http://8.8.8.8:13378", True),
])
def test_is_public(url, public):
    assert audiobookshelf.is_public(url) is public


async def test_find_at_a_public_domain_behind_a_reverse_proxy(monkeypatch):
    server = FakeABS(at=("https://abs.example.com/audiobookshelf",))
    monkeypatch.setattr(audiobookshelf, "transport", httpx.MockTransport(server))
    found = await audiobookshelf.find_at("abs.example.com")
    assert [s["url"] for s in found] == ["https://abs.example.com/audiobookshelf"]
    assert found[0]["version"] == "2.30.0" and found[0]["password_login"]


async def test_find_at_ignores_things_that_are_not_audiobookshelf(monkeypatch):
    monkeypatch.setattr(audiobookshelf, "transport", httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": 1})))
    assert await audiobookshelf.find_at("203.0.113.7") == []


async def test_find_nearby_sweeps_the_home_network_when_nothing_is_local(monkeypatch):
    server = FakeABS(at=("http://192.168.1.42:13378",))
    monkeypatch.setattr(audiobookshelf, "transport", httpx.MockTransport(server))
    monkeypatch.setattr(audiobookshelf, "_local_network", lambda: ipaddress.ip_network("192.168.1.0/24"))
    found = await audiobookshelf.find_nearby()
    assert [s["url"] for s in found] == ["http://192.168.1.42:13378"]


async def test_find_nearby_prefers_this_machine(monkeypatch):
    server = FakeABS(at=("http://localhost:13378", "http://192.168.1.42:13378"))
    monkeypatch.setattr(audiobookshelf, "transport", httpx.MockTransport(server))
    monkeypatch.setattr(audiobookshelf, "_local_network", lambda: pytest.fail("no sweep when one is local"))
    assert [s["url"] for s in await audiobookshelf.find_nearby()] == ["http://localhost:13378"]


# ---- signing in --------------------------------------------------------------------


async def test_sign_in_creates_a_narratarr_api_key(fake):
    async with Audiobookshelf("http://abs:13378") as server:
        assert await server.sign_in("root", "hunter2") == KEY
    assert fake.key_requests == [{"name": "Narratarr", "userId": "u1", "isActive": True}]


async def test_sign_in_on_an_older_server_uses_the_account_token(fake):
    fake.api_keys = False
    async with Audiobookshelf("http://abs:13378") as server:
        assert await server.sign_in("root", "hunter2") == "legacy-token"


async def test_sign_in_needs_an_admin(fake):
    fake.user_type = "user"
    async with Audiobookshelf("http://abs:13378") as server:
        with pytest.raises(AudiobookshelfError, match="admin"):
            await server.sign_in("root", "hunter2")
    assert fake.key_requests == []


async def test_wrong_password(fake):
    async with Audiobookshelf("http://abs:13378") as server:
        with pytest.raises(AudiobookshelfError, match="username and password"):
            await server.sign_in("root", "nope")


def test_pick_library():
    libs = [{"id": "a", "name": "A", "folders": ["/x"]}, {"id": "b", "name": "B", "folders": ["/books/"]}]
    assert audiobookshelf.pick_library(libs, "/books") == "b"
    assert audiobookshelf.pick_library(libs, "/elsewhere") == ""
    assert audiobookshelf.pick_library(libs[:1], "/elsewhere") == "a"


# ---- the API -------------------------------------------------------------------------


def test_sign_in_endpoint_saves_the_key_not_the_password(client, fake):
    r = client.post("/api/audiobookshelf/sign-in", json={"url": "http://abs:13378/", "username": "root", "password": "hunter2"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [lib["id"] for lib in body["libraries"]] == ["lib-books"]  # podcasts left out
    assert body["library_id"] == "lib-books"  # the only audiobook library
    s = config.load()
    assert (s.audiobookshelf_url, s.audiobookshelf_token, s.audiobookshelf_library_id) == ("http://abs:13378", KEY, "lib-books")
    assert "hunter2" not in config.SETTINGS_FILE.read_text()
    assert KEY not in json.dumps(body["settings"])  # masked like every other secret

    test = client.post("/api/settings/test-audiobookshelf").json()
    assert test == {"version": "2.30.0", "library": "Audiobooks"}


def test_sign_in_endpoint_refuses_plain_http_to_the_internet_unless_confirmed(client, fake):
    fake.at = {"http://8.8.8.8:13378"}
    payload = {"url": "http://8.8.8.8:13378", "username": "root", "password": "hunter2"}
    r = client.post("/api/audiobookshelf/sign-in", json=payload)
    assert r.status_code == 400 and "https" in r.json()["detail"]
    assert fake.logins == []  # the password never left
    assert client.post("/api/audiobookshelf/sign-in", json={**payload, "allow_insecure": True}).status_code == 200


def test_find_endpoint_flags_insecure_public_servers(client, fake):
    fake.at = {"http://8.8.8.8:13378"}
    servers = client.post("/api/audiobookshelf/find", json={"address": "http://8.8.8.8:13378"}).json()["servers"]
    assert servers[0]["public"] and servers[0]["insecure"]


# ---- rescans ---------------------------------------------------------------------------


async def test_rescan_after_a_download(fake):
    config.update({"audiobookshelf_url": "http://abs:13378", "audiobookshelf_token": KEY,
                   "audiobookshelf_library_id": "lib-books"})
    await Worker()._notify_audiobookshelf(config.load())
    assert fake.scans == ["lib-books"]


async def test_a_rejected_rescan_is_logged_not_swallowed(fake, caplog):
    config.update({"audiobookshelf_url": "http://abs:13378", "audiobookshelf_token": "revoked",
                   "audiobookshelf_library_id": "lib-books"})
    with caplog.at_level(logging.WARNING):
        await Worker()._notify_audiobookshelf(config.load())
    assert fake.scans == []
    assert "didn't accept Narratarr's key" in caplog.text


# ---- the setup guide -------------------------------------------------------------------


def test_compare_sees_books_through_a_docker_mount():
    books = [{"title": "Mistborn", "path": "/root/Audiobooks/Brandon Sanderson/Mistborn"},
             {"title": "Dune", "path": "/root/Audiobooks/Frank Herbert/Dune"}]
    items = [{"path": "/narratarr/Brandon Sanderson/Mistborn", "rel": "Brandon Sanderson/Mistborn", "missing": False},
             {"path": "/narratarr/Frank Herbert/Dune", "rel": "Frank Herbert/Dune", "missing": True}]  # deleted since
    assert audiobookshelf.compare(books, "/root/Audiobooks", items) == {
        "books": 2, "seen": 1, "missing": ["Dune"], "seen_at": "/narratarr"}


def test_compare_when_audiobookshelf_watches_a_folder_above_the_library():
    books = [{"title": "Mistborn", "path": "/srv/media/narratarr/Brandon Sanderson/Mistborn"}]
    items = [{"path": "/data/narratarr/Brandon Sanderson/Mistborn", "rel": "narratarr/Brandon Sanderson/Mistborn", "missing": False}]
    assert audiobookshelf.compare(books, "/srv/media/narratarr", items)["seen_at"] == "/data/narratarr"


def test_compare_finds_books_imported_in_place_by_author_and_title():
    books = [{"title": "Earthsea", "path": "/mnt/old/Ursula K. Le Guin/Earthsea"}]  # outside the library folder
    items = [{"path": "/old/Ursula K. Le Guin/Earthsea", "rel": "Ursula K. Le Guin/Earthsea", "missing": False}]
    assert audiobookshelf.compare(books, "/root/Audiobooks", items)["seen"] == 1
    other = [{"path": "/old/Someone Else/Earthsea", "rel": "Someone Else/Earthsea", "missing": False}]
    assert audiobookshelf.compare(books, "/root/Audiobooks", other)["seen"] == 0  # same title, different author


def test_where_audiobookshelf_runs(monkeypatch):
    monkeypatch.setattr(audiobookshelf, "_own_addresses", lambda: {ipaddress.ip_address("8.8.4.4")})
    assert audiobookshelf.where("http://localhost:13378") == "here"
    assert audiobookshelf.where("http://8.8.4.4:13378") == "here"  # this server's own public address, as on a VPS
    assert audiobookshelf.where("http://audiobookshelf:80") == "docker"
    assert audiobookshelf.where("http://192.168.1.40:13378") == "network"
    assert audiobookshelf.where("http://8.8.8.8:13378") == "internet"


def _downloaded(*names):
    root = config.load().library_dir
    for author, title in names:
        db.insert({"source": "local", "source_url": f"local:{title}", "title": title, "author": author,
                   "status": "completed", "path": f"{root}/{author}/{title}"})


def test_check_endpoint_rescans_and_reports_what_audiobookshelf_sees(client, fake, monkeypatch):
    config.update({"audiobookshelf_url": "http://abs:13378", "audiobookshelf_token": KEY, "audiobookshelf_library_id": "lib-books"})
    _downloaded(("Brandon Sanderson", "Mistborn"), ("Frank Herbert", "Dune"))
    fake.items["lib-books"] = [("/audiobooks/Brandon Sanderson/Mistborn", "Brandon Sanderson/Mistborn")]
    real_sleep = api.asyncio.sleep
    monkeypatch.setattr(api.asyncio, "sleep", lambda s: real_sleep(0))
    r = client.post("/api/audiobookshelf/check", json={}).json()
    assert r == {"books": 2, "seen": 1, "missing": ["Dune"], "seen_at": "/audiobooks",
                 "library": "Audiobooks", "folders": ["/audiobooks"]}
    assert fake.scans == ["lib-books"]

    fake.items["lib-books"].append(("/audiobooks/Frank Herbert/Dune", "Frank Herbert/Dune"))
    assert client.post("/api/audiobookshelf/check", json={}).json()["seen"] == 2


def test_check_endpoint_needs_a_connection(client):
    assert client.post("/api/audiobookshelf/check", json={}).status_code == 400


async def _nothing_nearby(sweep=True):
    return []


def test_guide_endpoint(client, fake, monkeypatch):
    monkeypatch.setattr(audiobookshelf, "find_nearby", _nothing_nearby)
    g = client.get("/api/audiobookshelf/guide").json()
    assert (g["connected"], g["where"], g["library_dir"]) == (False, "", config.load().library_dir)
    config.update({"audiobookshelf_url": "http://abs:13378", "audiobookshelf_token": KEY, "audiobookshelf_library_id": "lib-books"})
    g = client.get("/api/audiobookshelf/guide").json()
    assert g["connected"] and g["where"] == "docker"  # a bare name: another container
    assert g["libraries"][0]["folders"] == ["/audiobooks"]
    assert KEY not in json.dumps(g)
