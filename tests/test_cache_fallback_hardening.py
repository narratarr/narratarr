"""The cache fallback's edges: untrusted hashes, a restart mid-download, and the saved setting."""
from contextlib import asynccontextmanager

import httpx
import pytest

from narratarr import cachecheck, config, worker
from narratarr.downloaders import ClientEntry
from narratarr.realdebrid import RealDebrid
from narratarr.torbox import TorBox

HEX = "dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c"
MAGNET = f"magnet:?xt=urn:btih:{HEX}"


# ---- hashes come from indexers, so they are checked before they reach a URL ----

@pytest.mark.parametrize("bad", [
    "../../user", f"{HEX}/../../user", f"{HEX}?x=1", HEX[:39], HEX + "0", "z" * 40, "", None,
])
def test_only_forty_hex_characters_are_a_hash(bad):
    assert cachecheck.valid_infohash(bad) == ""


def test_a_hash_is_normalised_to_lowercase():
    assert cachecheck.valid_infohash(" " + HEX.upper() + " ") == HEX


def test_a_magnet_with_a_hash_of_the_wrong_length_has_no_hash():
    assert cachecheck.infohash_from_payload("magnet", "magnet:?xt=urn:btih:" + "a" * 35) == ""


async def test_an_untrusted_hash_never_reaches_the_cloud_service():
    asked = []

    class Cloud:
        label = "Real-Debrid"

        async def cached(self, infohash):
            asked.append(infohash)
            return True

    state, _ = await cachecheck.torrent_state(Cloud(), "../../user")
    assert state == cachecheck.UNKNOWN
    assert asked == []


@pytest.mark.parametrize("make", [
    lambda t: RealDebrid(api_key="k", transport=t),
    lambda t: TorBox(api_key="k", transport=t),
])
async def test_the_clients_send_no_request_for_a_bad_hash(make):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no request should be made, got {request.url}")

    assert await make(httpx.MockTransport(handler)).cached("../../user") is None


# ---- a grab that was already diverted carries on in the local client after a restart ----

class _Db:
    def __init__(self):
        self.updates = []

    def update(self, book_id, **fields):
        self.updates.append((book_id, fields))


class _Settings:
    debrid_fallback_client = "qb"
    clients = [
        {"id": "torbox", "type": "torbox", "api_key": "k"},
        {"id": "qb", "type": "qbittorrent", "url": "http://qb:8080", "username": "u", "password": "p"},
    ]


class _AskingCloud:
    """A cloud service whose cache check answers "cached" now, as one might after a restart."""
    label = "TorBox"
    prefix = "tb:"

    def __init__(self):
        self.asked = 0

    async def cached(self, infohash):
        self.asked += 1
        return True


@pytest.fixture
def w(monkeypatch):
    monkeypatch.setattr(worker, "db", _Db())

    @asynccontextmanager
    async def fake_client_for(entry, transport=None):
        yield object()

    monkeypatch.setattr(worker.downloaders, "client_for", fake_client_for)
    return worker.Worker()


async def _grab(w, monkeypatch, book, job_still_there):
    went = {}

    async def fake_resume(client, b, name):
        return b["client_job_id"] if job_still_there else ""

    async def fake_local(b, source, entry, decision):
        went["entry"] = entry.id

    monkeypatch.setattr(w, "_resume_job", fake_resume)
    monkeypatch.setattr(w, "_process_torrent_client", fake_local)
    cloud = _AskingCloud()
    diverted = await w._fall_back_if_uncached(
        book, None, ClientEntry(id="torbox", type="torbox"), None, cloud, "magnet", MAGNET, _Settings(),
    )
    return diverted, went, cloud


async def test_a_diverted_grab_resumes_in_the_local_client_without_asking_again(w, monkeypatch):
    book = {"id": 7, "info_hash": HEX, "download_client": "qb", "client_job_id": HEX}
    diverted, went, cloud = await _grab(w, monkeypatch, book, job_still_there=True)
    assert diverted is True and went["entry"] == "qb"
    assert cloud.asked == 0, "asking again could start the same book on the cloud service as well"


async def test_a_local_job_that_is_gone_falls_back_to_the_normal_check(w, monkeypatch):
    book = {"id": 7, "info_hash": HEX, "download_client": "qb", "client_job_id": HEX}
    diverted, went, cloud = await _grab(w, monkeypatch, book, job_still_there=False)
    assert diverted is False and "entry" not in went
    assert cloud.asked == 1


async def test_a_book_with_no_local_job_is_checked_as_usual(w, monkeypatch):
    book = {"id": 7, "info_hash": HEX, "download_client": "", "client_job_id": ""}
    diverted, went, cloud = await _grab(w, monkeypatch, book, job_still_there=True)
    assert diverted is False and cloud.asked == 1


# ---- the saved setting only ever names a local torrent client ----

CLIENTS = [
    {"id": "torbox", "type": "torbox", "api_key": "k"},
    {"id": "qb", "type": "qbittorrent", "url": "http://qb:8080"},
    {"id": "sab", "type": "sabnzbd", "url": "http://sab:8080", "api_key": "k"},
]


@pytest.mark.parametrize("pick, saved", [
    ("qb", "qb"), (" QB ", "qb"), ("torbox", ""), ("sab", ""), ("nope", ""), ("", ""),
])
def test_the_fallback_must_be_a_local_torrent_client(pick, saved):
    s = config.update({"clients": CLIENTS, "debrid_fallback_client": pick})
    assert s.debrid_fallback_client == saved


def test_removing_the_fallback_client_turns_the_fallback_off():
    config.update({"clients": CLIENTS, "debrid_fallback_client": "qb"})
    s = config.update({"clients": [c for c in CLIENTS if c["id"] != "qb"]})
    assert s.debrid_fallback_client == ""


def test_the_settings_page_gets_the_saved_fallback_back(accounts):
    c = accounts
    assert c.put("/api/settings", json={"clients": CLIENTS, "debrid_fallback_client": "qb"}).status_code == 200
    assert c.get("/api/download-clients").json()["debrid_fallback_client"] == "qb"
