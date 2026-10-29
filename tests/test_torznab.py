"""The Torznab source, driven through the real pipeline against the two Jackett fixtures.

The fixtures are real Jackett responses with the API key replaced, so they carry the awkward parts:
magnets beside download links in one response, an upper-case infohash, RFC 822 dates, and a key
called `jackett_apikey` on every download and cover link.
"""

import json
from pathlib import Path

import httpx
import pytest

from narratarr import api, config, db
from narratarr.sources import torznab
from narratarr.worker import Worker

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH = (FIXTURES / "torznab_search.xml").read_bytes()
CAPS = (FIXTURES / "torznab_caps.xml").read_bytes()
KEY = "REDACTED-JACKETT-KEY"
# The fixtures' download links point at this host, which is what a real setup looks like:
# the indexer address in Settings and the links it returns share a host, so the key can be put back.
BASE = "http://10.2.3.144:9117/api/v2.0/indexers/all/results/torznab/api"


@pytest.fixture(autouse=True)
def clean_registry():
    torznab._releases.clear()
    torznab._search_cache = torznab.Cache(torznab.CACHE_SECONDS, 200)
    yield
    torznab._releases.clear()


def transport(handler):
    return httpx.MockTransport(handler)


def jackett(request: httpx.Request) -> httpx.Response:
    if request.url.params.get("t") == "caps":
        return httpx.Response(200, content=CAPS)
    return httpx.Response(200, content=SEARCH)


async def test_caps_connection_check():
    async with httpx.AsyncClient(transport=transport(jackett)) as client:
        got = await torznab.Torznab(BASE, KEY, client=client).status()
    assert got["title"] == "Jackett"
    assert got["book_search"] is True
    assert got["categories"] > 5


async def test_search_returns_both_shapes_with_clean_titles():
    async with httpx.AsyncClient(transport=transport(jackett)) as client:
        page = await torznab.Torznab(BASE, KEY, client=client).search("Project Hail Mary")

    assert len(page.items) == 4
    assert all("[" not in i.title for i in page.items), [i.title for i in page.items]
    assert {i.indexer for i in page.items} == {"The Pirate Bay", "AudioBook Bay"}

    magnet, *links = page.items
    assert magnet.info_hash == "0f54898dc1b8e49d96e32827377f651ea6c935af"  # fixture is upper case
    assert magnet.download_url == ""  # the hash is enough; nothing to fetch
    assert magnet.seeders == 338
    assert links[0].info_hash == "" and links[0].download_url  # a /dl/ link with no published hash
    assert links[0].format == "MP3" and links[0].bitrate == "128 Kbps"
    assert all(i.posted[:2] == "20" and len(i.posted) == 10 for i in page.items)


async def test_rfc_822_dates_become_iso():
    assert torznab._posted("Sun, 16 May 2021 11:33:22 +0100") == "2021-05-16"
    assert torznab._posted("Sun, 14 Nov 2021 00:00:00 +0000") == "2021-11-14"
    assert torznab._posted("2021-05-16T11:33:22Z") == "2021-05-16"  # ISO, as Prowlarr sends
    assert torznab._posted("") == ""


def test_the_key_never_survives_into_a_stored_url():
    """Both spellings, in the query and at the end of the URL, in any case."""
    for url in (
        "http://h:9117/dl/x/?jackett_apikey=SECRET&path=a",
        "http://h/api?apikey=SECRET&t=search",
        "http://h/dl/x/?JACKETT_APIKEY=SECRET&path=a",
        "http://h/dl/x/?path=a&jackett_apikey=SECRET",
    ):
        assert "SECRET" not in torznab.strip_key(url)
    assert torznab.strip_key("magnet:?xt=urn:btih:ABC") == "magnet:?xt=urn:btih:ABC"


async def test_no_key_reaches_any_details_field():
    async with httpx.AsyncClient(transport=transport(jackett)) as client:
        src = torznab.Torznab(BASE, KEY, client=client)
        page = await src.search("Project Hail Mary")
        for item in page.items:
            d = await src.details(item.url)
            blob = json.dumps(d.to_dict())
            assert KEY not in blob, f"{KEY} leaked through {d.url}"
            assert "jackett_apikey" not in blob


async def test_key_is_put_back_only_for_our_own_indexer():
    ours = torznab.with_key("http://10.2.3.144:9117/dl/x/?path=a", BASE, "K")
    assert "path=a" in ours and "jackett_apikey=K" in ours and "apikey=K" in ours
    foreign = "http://elsewhere.example/dl/x/?path=a"
    assert torznab.with_key(foreign, BASE, "K") == foreign


async def test_expired_release_is_a_clear_error():
    src = torznab.Torznab(BASE, KEY)
    with pytest.raises(torznab.TorznabError, match="expired"):
        await src.details("https://example.invalid/never-seen")


async def test_missing_settings_say_so():
    with pytest.raises(torznab.TorznabError, match="Settings"):
        await torznab.Torznab("", "").status()


# ---- the pipeline, end to end -------------------------------------------------------------


class FakeIndexerAndClient:
    """One transport for the whole worker: it serves the indexer *and* the torrent client, because
    the worker hands its single transport to every client it builds."""

    TORRENT = b"d8:announce13:http://tracker4:infod4:name4:Dune12:piece lengthi16384e6:pieces0:ee"

    def __init__(self, content_path: Path):
        self.content_path = content_path
        self.fetched: list[str] = []
        self.added = b""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, request.url.params
        if "/dl/" in path:                                    # the indexer's download link
            self.fetched.append(str(request.url))
            if not (params.get("jackett_apikey") or params.get("apikey")):
                return httpx.Response(401)                    # Jackett wants its own parameter name
            return httpx.Response(200, content=self.TORRENT)
        if "/torznab/api" in path:                            # the indexer's search/caps endpoint
            return httpx.Response(200, content=CAPS if params.get("t") == "caps" else SEARCH)
        if path == "/api/v2/auth/login":                      # qBittorrent
            return httpx.Response(204, headers={"set-cookie": "SID=abc; path=/"})
        if path == "/api/v2/torrents/add":
            self.added = request.content
            return httpx.Response(200, text="Ok.")
        if path == "/api/v2/torrents/info":
            return httpx.Response(200, json=[{"hash": "a" * 40, "state": "uploading", "progress": 1,
                                              "dlspeed": 0, "num_seeds": 1,
                                              "content_path": str(self.content_path)}])
        return httpx.Response(404)


async def test_pipeline_fetches_the_torrent_through_the_right_source(isolated_data, monkeypatch):
    """A .torrent release has no info hash, so the worker fetches it — from the indexer that listed
    it, with that indexer's address and key. Getting this wrong is invisible until a live run."""
    config.update({"torznab_url": BASE, "torznab_api_key": KEY, "torznab_categories": "3030",
                   "torrent_client": "qbittorrent", "qbittorrent_url": "http://qbit:8080",
                   "qbittorrent_username": "admin", "qbittorrent_password": "pw"})

    folder = isolated_data / "downloads" / "Dune"
    folder.mkdir(parents=True)
    (folder / "Dune.m4b").write_bytes(b"audio")
    fake = FakeIndexerAndClient(folder)

    def no_prowlarr(*a, **k):
        raise AssertionError("the torrent was fetched through Prowlarr instead of the indexer")

    monkeypatch.setattr("narratarr.worker.fetch", no_prowlarr)

    mock = httpx.MockTransport(fake)
    async with httpx.AsyncClient(transport=mock) as client:
        src = torznab.Torznab(BASE, KEY, client=client)
        page = await src.search("Project Hail Mary")
        release = next(i for i in page.items if i.download_url)   # a /dl/ link, no published hash
        d = await src.details(release.url)
        book = db.insert({
            "source": "torznab", "source_url": d.url, "title": d.title, "author": "Andy Weir",
            "protocol": "torrent", "download_url": d.download_url, "status": "queued",
        })

    await Worker(transport=mock).process(book["id"])

    assert fake.fetched, "the .torrent was never fetched from the indexer"
    assert "jackett_apikey=" in fake.fetched[0], fake.fetched[0]
    assert fake.added, "the torrent never reached the client"
    assert db.get(book["id"])["status"] == "completed", db.get(book["id"])["error"]


# ---- the accounts model -------------------------------------------------------------------

HOME = ("192.168.1.20", 50000)


@pytest.fixture
def accounts(monkeypatch):
    """A real admin and a real listener, signed in through the actual middleware."""
    from fastapi.testclient import TestClient
    from narratarr import api, auth, users

    auth._failures.clear()
    monkeypatch.setattr(users, "_N", 2**10)  # fast hashing in tests
    c = TestClient(api.app, client=HOME)
    c.__enter__()
    c.app.state.worker.poke = lambda: None
    assert c.post("/api/auth/setup", json={"username": "admin", "password": "admin-pass"}).status_code == 200
    made = c.post("/api/users", json={"username": "listener", "password": "listen-pass", "role": "listener"})
    assert made.status_code == 200, made.text
    yield c
    c.__exit__(None, None, None)


def as_role(client, username, password):
    assert client.post("/api/auth/login", json={"username": username, "password": password}).status_code == 200
    return client


@pytest.mark.real_auth
def test_settings_are_masked_for_an_admin_and_hidden_from_a_listener(accounts):
    config.update({"torznab_url": BASE, "torznab_api_key": KEY, "torznab_categories": "3030"})

    admin = as_role(accounts, "admin", "admin-pass").get("/api/settings").json()
    assert admin["torznab_configured"] is True
    assert KEY not in json.dumps(admin)
    assert admin["torznab_api_key"].startswith("\u2022")  # masked, last four characters only

    listener = as_role(accounts, "listener", "listen-pass").get("/api/settings").json()
    assert KEY not in json.dumps(listener)
    assert "torznab_url" not in listener and "torznab_api_key" not in listener
    assert listener["torznab_configured"] is True  # so the request UI doesn't say to configure one


@pytest.mark.real_auth
def test_masked_key_round_trips_and_host_change_clears_it(accounts):
    config.update({"torznab_url": BASE, "torznab_api_key": KEY})
    masked = config.load().public()["torznab_api_key"]
    config.update({"torznab_api_key": masked})  # what the browser posts back
    assert config.load().torznab_api_key == KEY
    config.update({"torznab_url": "http://elsewhere:9117/torznab/api"})
    assert config.load().torznab_api_key == ""  # a key never follows the address to a new host


@pytest.mark.real_auth
def test_a_listener_cannot_reach_the_test_button_or_change_the_indexer(accounts):
    as_role(accounts, "listener", "listen-pass")
    assert accounts.post("/api/settings/test-torznab").status_code == 403
    assert accounts.put("/api/settings", json={"torznab_url": "http://evil"}).status_code == 403


@pytest.mark.real_auth
def test_an_admin_can_reach_the_test_button(accounts, monkeypatch):
    config.update({"torznab_url": BASE, "torznab_api_key": KEY})

    class Stub:
        def __init__(self, *a, **k):
            pass

        async def status(self):
            return {"title": "Jackett", "book_search": True, "categories": 8}

    monkeypatch.setattr(api, "Torznab", Stub)  # the gate is what is under test, not the network
    as_role(accounts, "admin", "admin-pass")
    got = accounts.post("/api/settings/test-torznab")
    assert got.status_code == 200, got.text
    assert got.json()["title"] == "Jackett"


@pytest.mark.real_auth
def test_sources_lists_torznab_without_leaking_its_address(accounts):
    config.update({"torznab_url": BASE, "torznab_api_key": KEY})
    as_role(accounts, "listener", "listen-pass")
    listed = accounts.get("/api/sources").json()
    assert "torznab" in [s["name"] for s in listed]
    blob = json.dumps(listed)
    assert KEY not in blob and "10.2.3.144:9117" not in blob
