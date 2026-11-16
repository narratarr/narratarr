"""A release with a known hash but no trackers is fetched from its indexer, not sent as a bare magnet.

From the Discord: qBittorrent sat on "Downloading metadata" with nothing in its Trackers tab. Prowlarr
gives most torrents an info hash, and its magnetUrl is usually a link back to Prowlarr rather than a
magnet, so Narratarr built a magnet from the hash alone. That magnet has no trackers.
"""
import pytest

from narratarr import config, db, worker
from narratarr.sources.prowlarr import ProwlarrError, to_details

HASH = "0f54898dc1b8e49d96e32827377f651ea6c935af"
PROWLARR = "http://prowlarr:9696"
TORRENT = b"d8:announce30:udp://tracker.example:1337/ann4:infod4:name4:Dunee" + b"e"


def test_a_prowlarr_release_keeps_its_download_link_when_the_hash_is_known():
    d = to_details({
        "title": "Frank Herbert - Dune [M4B]", "protocol": "torrent", "infoHash": HASH.upper(),
        "magnetUrl": f"{PROWLARR}/1/download?apikey=SECRET&link=abc&file=Dune",
        "downloadUrl": f"{PROWLARR}/1/download?apikey=SECRET&link=abc&file=Dune",
        "guid": "https://tracker.example/t/1", "seeders": 22,
    })
    assert d.info_hash == HASH and d.trackers == []
    assert d.download_url.startswith(PROWLARR)
    assert "SECRET" not in d.download_url and "apikey" not in d.download_url.lower()


def _book(**over):
    row = {"source": "prowlarr", "source_url": "https://tracker.example/t/1", "title": "Dune",
           "protocol": "torrent", "info_hash": HASH, "trackers": [],
           "download_url": f"{PROWLARR}/1/download?link=abc", "status": "queued"}
    row.update(over)
    return db.insert(row)


@pytest.fixture
def fetched(monkeypatch):
    config.update({"prowlarr_url": PROWLARR, "prowlarr_api_key": "SECRET"})

    class Calls(list):
        fake = None

    calls = Calls()

    async def fake_fetch(url, base, key, transport=None):
        calls.append((url, base, key))
        if isinstance(fake_fetch.result, Exception):
            raise fake_fetch.result
        return fake_fetch.result

    fake_fetch.result = ("torrent", TORRENT)
    monkeypatch.setattr(worker, "fetch", fake_fetch)
    calls.fake = fake_fetch
    return calls


async def test_no_trackers_means_the_indexer_torrent_is_used(fetched):
    book = _book()
    kind, payload = await worker.Worker()._torrent_payload(book, None, config.load())
    assert (kind, payload) == ("torrent", TORRENT)
    assert fetched[0] == (f"{PROWLARR}/1/download?link=abc", PROWLARR, "SECRET")


async def test_a_magnet_that_has_trackers_is_still_built_from_the_hash(fetched):
    book = _book(trackers=["udp://tracker.example:1337/announce"])
    kind, payload = await worker.Worker()._torrent_payload(book, None, config.load())
    assert kind == "magnet" and f"btih:{HASH}" in payload and "tr=udp" in payload
    assert fetched == [], "no need to ask the indexer when the magnet already has trackers"


async def test_an_indexer_that_fails_falls_back_to_the_hash_and_says_so(fetched):
    fetched.fake.result = ProwlarrError("The indexer wouldn't hand over this release (410). Try another one.")
    book = _book()
    kind, payload = await worker.Worker()._torrent_payload(book, None, config.load())
    assert kind == "magnet" and f"btih:{HASH}" in payload
    detail = db.get(book["id"])["detail"]
    assert "info hash" in detail and "SECRET" not in detail


async def test_an_indexer_that_fails_with_no_hash_is_an_error(fetched):
    fetched.fake.result = ProwlarrError("Couldn't reach Prowlarr to fetch this release.")
    book = _book(info_hash="")
    with pytest.raises(ProwlarrError):
        await worker.Worker()._torrent_payload(book, None, config.load())


async def test_a_usenet_file_from_a_torrent_link_is_refused(fetched):
    fetched.fake.result = ("nzb", b"<nzb/>")
    with pytest.raises(ProwlarrError):
        await worker.Worker()._torrent_payload(_book(), None, config.load())
