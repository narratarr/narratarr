"""Cache state before a debrid grab, and the opt-in fallback to a local torrent client.

No network: the provider clients are driven through httpx.MockTransport, and the worker
decision is tested against a stub cloud so the only thing under test is the routing.
"""
import httpx
import pytest

from narratarr import cachecheck, worker
from narratarr.downloaders import ClientEntry
from narratarr.realdebrid import RealDebrid
from narratarr.torbox import TorBox

HEX = "dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c"
MAGNET = f"magnet:?xt=urn:btih:{HEX}&dn=Some+Book&tr=udp%3A%2F%2Ftracker.example%3A80"
# base32 of the same 20 bytes
B32 = "3WBFL3G4PSSV7MF37AJSHWDQMLNR63I4"


def test_infohash_from_a_magnet():
    assert cachecheck.infohash_from_payload("magnet", MAGNET) == HEX


def test_infohash_from_a_base32_magnet_is_converted_to_hex():
    got = cachecheck.infohash_from_payload("magnet", f"magnet:?xt=urn:btih:{B32}")
    assert got == HEX, got


def test_a_torrent_file_has_no_hash_without_a_bencode_parser():
    """Narratarr does not ship one, so this reports the truth: no hash, no check."""
    assert cachecheck.infohash_from_payload("torrent", b"d8:announce...") == ""


class _Cloud:
    label = "TorBox"
    prefix = "tb:"

    def __init__(self, answer):
        self._answer = answer

    async def cached(self, infohash):
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


class _NoCheck:
    label = "AllDebrid"
    prefix = "ad:"


async def test_state_without_a_hash_is_unknown():
    state, reason = await cachecheck.torrent_state(_Cloud(True), "")
    assert state == cachecheck.UNKNOWN
    assert "couldn't identify" in reason


async def test_a_provider_without_a_cache_check_is_unavailable():
    state, reason = await cachecheck.torrent_state(_NoCheck(), HEX)
    assert state == cachecheck.UNAVAILABLE
    assert "doesn't say whether" in reason


@pytest.mark.parametrize("answer,expected", [(True, cachecheck.CACHED), (False, cachecheck.UNCACHED), (None, cachecheck.UNKNOWN)])
async def test_answers_map_to_states(answer, expected):
    state, _ = await cachecheck.torrent_state(_Cloud(answer), HEX)
    assert state == expected


async def test_an_error_is_unknown_and_never_a_miss():
    """The whole feature turns on this: a broken provider must not reroute a download."""
    state, reason = await cachecheck.torrent_state(_Cloud(httpx.ConnectError("boom")), HEX)
    assert state == cachecheck.UNKNOWN
    assert state != cachecheck.UNCACHED
    assert "Couldn't ask" in reason


def _torbox(handler):
    return TorBox(api_key="tb-key", transport=httpx.MockTransport(handler))


async def test_torbox_reports_cached_when_it_lists_files():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/v1/api/torrents/checkcached")
        assert request.url.params["hash"] == HEX
        return httpx.Response(200, json={
            "success": True, "error": None, "detail": "",
            "data": {HEX: {"hash": HEX, "files": [{"id": 1, "name": "book.m4b", "size": 10}], "size": 10}},
        })

    assert await _torbox(handler).cached(HEX) is True


async def test_torbox_empty_answer_is_a_miss():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "error": None, "detail": "", "data": None})

    assert await _torbox(handler).cached(HEX) is False


async def test_torbox_entry_with_no_files_is_not_cached():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "success": True, "error": None, "detail": "",
            "data": {HEX: {"hash": HEX, "files": [], "size": 0}},
        })

    assert await _torbox(handler).cached(HEX) is False


def _rd(handler):
    return RealDebrid(api_key="rd-key", transport=httpx.MockTransport(handler))


async def test_realdebrid_positive_answer_is_cached():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/torrents/instantAvailability/" in request.url.path
        return httpx.Response(200, json={HEX: {"rd": [{"filename": "book.m4b", "filesize": 10}]}})

    assert await _rd(handler).cached(HEX) is True


@pytest.mark.parametrize("body", [{}, {HEX: {}}])
async def test_realdebrid_anything_else_is_unknown_never_a_miss(body):
    """Real-Debrid may have switched this endpoint off. An empty body proves nothing."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    assert await _rd(handler).cached(HEX) is None


class _Settings:
    def __init__(self, fallback=""):
        self.debrid_fallback_client = fallback
        self.clients = [
            {"id": "torbox", "type": "torbox", "api_key": "k"},
            {"id": "qb", "type": "qbittorrent", "url": "http://qb:8080", "username": "u", "password": "p"},
        ]


@pytest.fixture
def w(monkeypatch):
    monkeypatch.setattr(worker, "db", _Db())
    return worker.Worker()


class _Db:
    def __init__(self):
        self.updates = []

    def update(self, book_id, **fields):
        self.updates.append((book_id, fields))

    def get(self, book_id):
        return {"id": book_id}


BOOK = {"id": 7, "info_hash": HEX, "download_url": "https://x/y.torrent", "source": "audiobookbay"}


async def _run(w, monkeypatch, answer, settings, fallback_id="qb"):
    diverted = {}

    async def fake_local(book, source, entry, decision):
        diverted["entry"] = entry.id

    monkeypatch.setattr(w, "_process_torrent_client", fake_local)
    settings.debrid_fallback_client = fallback_id
    return await w._fall_back_if_uncached(
        BOOK, None, ClientEntry(id="torbox", type="torbox"), ClientEntry(id="qb", type="qbittorrent"),
        _Cloud(answer), "magnet", MAGNET, settings,
    ), diverted


async def test_off_in_the_settings_never_diverts(w, monkeypatch):
    decided, diverted = await _run(w, monkeypatch, False, _Settings(), fallback_id="")
    assert decided is False
    assert "entry" not in diverted


async def test_uncached_diverts_to_the_local_client(w, monkeypatch):
    decided, diverted = await _run(w, monkeypatch, False, _Settings())
    assert decided is True
    assert diverted["entry"] == "qb"
    assert any("downloads from the swarm" in f.get("detail", "") for _, f in worker.db.updates)


async def test_cached_stays_on_the_cloud(w, monkeypatch):
    decided, diverted = await _run(w, monkeypatch, True, _Settings())
    assert decided is False
    assert "entry" not in diverted


async def test_unknown_stays_on_the_cloud_and_says_why(w, monkeypatch):
    decided, diverted = await _run(w, monkeypatch, None, _Settings())
    assert decided is False
    assert "entry" not in diverted
    assert any("Sending it to TorBox" in f.get("detail", "") for _, f in worker.db.updates)


async def test_a_fallback_that_is_not_a_torrent_client_does_not_divert(w, monkeypatch):
    settings = _Settings()
    settings.clients = [{"id": "sab", "type": "sabnzbd", "url": "http://sab"}]
    decided, diverted = await _run(w, monkeypatch, False, settings, fallback_id="sab")
    assert decided is False
    assert "entry" not in diverted
