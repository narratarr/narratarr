from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from narratarr import api, db, worker
from narratarr.sources import archive
from narratarr.worker import Worker

SEARCH = {
    "response": {
        "numFound": 50,
        "docs": [
            {
                "identifier": "adventures_holmes",
                "title": "The Adventures of Sherlock Holmes",
                "creator": "Sir Arthur Conan Doyle",
                "language": "eng",
                "date": "2007-03-10T00:00:00Z",
                "subject": ["librivox", "audiobook", "Holmes", "mystery"],
            },
            {
                "identifier": "bez_librivox",
                "title": "Bez dogmatu",
                "creator": ["Henryk Sienkiewicz"],
                "language": ["pol"],
            },
        ],
    }
}

METADATA = {
    "metadata": {
        "title": "The Adventures of Sherlock Holmes",
        "creator": "Sir Arthur Conan Doyle",
        "description": "<a href='http://librivox.org/'>Librivox</a> recording.<br><br>Twelve <b>stories</b>.",
        "language": "eng",
        "runtime": "10:17:59",
        "subject": "librivox; audiobook; literature; Holmes",
        "date": "2007-03-10",
    },
    "files": [
        {
            "name": "Adventures of Sherlock Holmes_librivox.m4b",
            "format": "Audiobook",
            "size": "1000",
        },
        {"name": "holmes_01.mp3", "format": "VBR MP3", "size": "400"},
        {"name": "holmes_02.mp3", "format": "VBR MP3", "size": "500"},
        {"name": "holmes_01_64kb.mp3", "format": "64Kbps MP3", "size": "200"},
        {"name": "__ia_thumb.jpg", "format": "Item Tile", "size": "20"},
    ],
}


def ia_client(seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.url.path == "/advancedsearch.php":
            return httpx.Response(200, json=SEARCH)
        if request.url.path.startswith("/metadata/"):
            return httpx.Response(200, json=METADATA)
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def clear_cache():
    archive._cache.clear()


async def test_search_scopes_to_collection_and_parses_docs():
    seen = []
    src = archive.librivox(client=ia_client(seen))
    page = await src.search('sherlock "holmes"')
    q = seen[0].url.params["q"]
    assert q == "collection:librivoxaudio AND (sherlock holmes)"
    assert page.total_pages == 3
    holmes, polish = page.items
    assert holmes.url == "https://archive.org/details/adventures_holmes"
    assert holmes.cover_url == "https://archive.org/services/img/adventures_holmes"
    assert (holmes.language, holmes.posted) == ("English", "2007-03-10")
    assert holmes.categories == ["Holmes", "Mystery"]
    assert (polish.author, polish.language) == ("Henryk Sienkiewicz", "Polish")


async def test_categories_and_newest_change_the_query():
    seen = []
    src = archive.librivox(client=ia_client(seen))
    await src.latest(1, "mystery")
    await src.latest(1, "new")
    assert "subject:(mystery OR detective)" in seen[0].url.params["q"]
    assert seen[0].url.params["sort[]"] == "downloads desc"
    assert seen[1].url.params["sort[]"] == "publicdate desc"


async def test_details_prefers_single_m4b_and_cleans_metadata():
    src = archive.librivox(client=ia_client())
    d = await src.details("https://archive.org/details/adventures_holmes")
    assert d.format == "M4B"
    assert [f["name"] for f in d.files] == ["Adventures of Sherlock Holmes_librivox.m4b"]
    assert (
        d.files[0]["url"]
        == "https://archive.org/download/adventures_holmes/Adventures%20of%20Sherlock%20Holmes_librivox.m4b"
    )
    assert d.length == "10 hours and 18 minutes"
    assert d.description == "Librivox recording.\n\nTwelve stories."
    assert "Holmes" in d.categories and "Librivox" not in d.categories


def test_pick_files_falls_back_to_mp3_set():
    files = [f for f in METADATA["files"] if f["format"] != "Audiobook"]
    chosen, fmt = archive.pick_files("x", files)
    assert fmt == "MP3"
    assert [f["name"] for f in chosen] == ["holmes_01.mp3", "holmes_02.mp3"]
    assert archive.pick_files("x", [{"name": "a.pdf", "format": "Text PDF"}]) == ([], "")


def test_owns_only_archive_item_pages():
    src = archive.oldtimeradio()
    assert src.owns("https://archive.org/details/OTRR_Gunsmoke_Singles")
    assert not src.owns("https://archive.org/search?query=x")
    assert not src.owns("https://evil.example/details/x")


async def test_direct_download_skips_real_debrid(isolated_data, monkeypatch):
    monkeypatch.setattr(worker, "POLL_SECONDS", 0)
    hits = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.host)
        if request.url.host == "archive.org" and "/download/" in request.url.path:
            return httpx.Response(200, content=b"m4b-audio")
        if request.url.path.startswith("/services/img/"):
            return httpx.Response(
                200, content=b"\xff\xd8jpg", headers={"content-type": "image/jpeg"}
            )
        return httpx.Response(404)

    book = db.insert(
        {
            "source": "librivox",
            "source_url": "https://archive.org/details/adventures_holmes",
            "title": "The Adventures of Sherlock Holmes",
            "author": "Sir Arthur Conan Doyle",
            "cover_url": "https://archive.org/services/img/adventures_holmes",
            "status": "queued",
            "files": [
                {
                    "url": "https://archive.org/download/adventures_holmes/Holmes.m4b",
                    "name": "Disc 1/Holmes.m4b",
                    "size": 9,
                }
            ],
        }
    )
    from narratarr import config

    config.update({"rd_api_key": ""})  # proves Real-Debrid isn't needed

    await Worker(transport=httpx.MockTransport(handler)).process(book["id"])

    done = db.get(book["id"])
    assert done["status"] == "completed", done["error"]
    dest = Path(done["path"])
    assert (dest / "Disc 1 - Holmes.m4b").read_bytes() == b"m4b-audio"
    assert (dest / "cover.jpg").exists()
    assert "api.real-debrid.com" not in hits


def test_api_lists_sources_and_adds_free_books(monkeypatch):
    src = archive.librivox(client=ia_client())
    real_get = api.get_source
    monkeypatch.setattr(
        api, "get_source", lambda name="audiobookbay": src if name == "librivox" else real_get(name)
    )
    with TestClient(api.app) as c:
        c.app.state.worker.poke = lambda: None
        names = [s["name"] for s in c.get("/api/sources").json()]
        assert names == ["librivox", "oldtimeradio", "audiobookbay"]

        items = c.get("/api/discover", params={"source": "librivox", "category": "mystery"}).json()[
            "items"
        ]
        assert items[0]["source"] == "librivox"

        book = c.post("/api/library", json={"url": items[0]["url"], "source": "librivox"}).json()
        assert book["source"] == "librivox"
        assert book["status"] == "queued"
        assert book["files"][0]["name"].endswith(".m4b")

        assert c.get("/api/discover", params={"source": "nope"}).status_code == 400
