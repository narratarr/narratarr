"""Torznab as a full indexer: Wanted searches, ebook categories, and the catalogs it unlocks."""

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from narratarr import api, config, db, wanted
from narratarr.sources import torznab

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH = (FIXTURES / "torznab_search.xml").read_bytes()
BASE = "http://10.2.3.144:9117/api/v2.0/indexers/all/results/torznab/api"


@pytest.fixture(autouse=True)
def only_torznab(monkeypatch):
    torznab._releases.clear()
    torznab._search_cache = torznab.Cache(torznab.CACHE_SECONDS, 200)
    config.update({"torznab_url": BASE, "torznab_api_key": "K", "prowlarr_url": "", "prowlarr_api_key": ""})
    yield
    torznab._releases.clear()


@pytest.fixture
def jackett(monkeypatch):
    """Every Torznab request goes to a fake Jackett; the categories asked for are kept."""
    asked: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.params.get_list("cat"))
        return httpx.Response(200, content=SEARCH)

    real = httpx.AsyncClient

    def client(*a, **k):
        k["transport"] = httpx.MockTransport(handler)
        return real(*a, **k)

    monkeypatch.setattr(torznab.httpx, "AsyncClient", client)

    async def no_librivox(*a, **k):
        from narratarr.sources import Page
        return Page(items=[], page=1, total_pages=1)

    monkeypatch.setattr("narratarr.sources.archive.ArchiveSource.find", no_librivox)
    return asked


async def test_wanted_books_are_looked_for_on_torznab(jackett):
    book = db.insert({"source": "audible", "source_url": "https://www.audible.com/pd/B08G9PRS1K", "title": "Project Hail Mary",
                      "author": "Andy Weir", "status": "wanted"})
    found = await wanted.find_releases(book)
    assert {f["source"] for f in found} == {"torznab"}
    assert jackett[-1] == ["3030"]


async def test_wanted_ebooks_use_the_ebook_categories(jackett):
    config.update({"torznab_ebook_categories": "7020,7050"})
    book = db.insert({"source": "openlibrary", "source_url": "https://openlibrary.org/works/OL1W#ebook",
                      "title": "Project Hail Mary", "author": "Andy Weir", "status": "wanted", "kind": "ebook"})
    await wanted.find_releases(book)
    assert jackett[-1] == ["7020", "7050"]


async def test_automatic_search_runs_with_only_torznab(jackett, monkeypatch):
    calls = []

    async def fake_search_and_grab(b, hold=False):
        calls.append(b["id"])
        return None

    monkeypatch.setattr(wanted, "search_and_grab", fake_search_and_grab)
    monkeypatch.setattr(wanted, "due_for_search", lambda: [{"id": 7, "title": "x"}])
    await wanted.auto_search()
    assert calls == [7]


def test_torznab_errors_are_indexer_errors():
    from narratarr.sources import ProwlarrError

    assert issubclass(torznab.TorznabError, ProwlarrError)


@pytest.fixture
def client():
    with TestClient(api.app) as c:
        c.app.state.worker.poke = lambda: None
        yield c


def test_catalogs_appear_with_only_torznab(client):
    names = [s["name"] for s in client.get("/api/sources").json()]
    assert "torznab" in names and "audible" in names and "openlibrary" in names
    assert "prowlarr" not in names
    config.update({"torznab_url": ""})
    names = [s["name"] for s in client.get("/api/sources").json()]
    assert "audible" not in names and "torznab" not in names


def test_an_unreachable_indexer_is_a_readable_error(client, monkeypatch):
    async def down(self, query):
        raise torznab.TorznabError("Your indexer took too long to answer.")

    monkeypatch.setattr(torznab.Torznab, "_search", down)
    res = client.get("/api/discover", params={"source": "torznab", "q": "dune"})
    assert res.status_code == 502 and "too long" in res.json()["detail"]
