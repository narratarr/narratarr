import httpx
import pytest
from fastapi.testclient import TestClient

from narratarr import api, db
from narratarr.sources.audiobookbay import AudiobookBay

BASE = "https://audiobookbay.lu"
BOOK_URL = f"{BASE}/abss/dune-frank-herbert-2/"


@pytest.fixture
def client(monkeypatch, fixture_html):
    def handler(request: httpx.Request) -> httpx.Response:
        if "/abss/" in request.url.path:
            return httpx.Response(200, text=fixture_html("abb_detail.html"))
        return httpx.Response(200, text=fixture_html("abb_search.html"))

    fake = AudiobookBay(BASE, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(api, "get_source", lambda name="audiobookbay": fake)
    monkeypatch.setattr(api, "source_for_url", lambda url: fake if fake.owns(url) else None)
    monkeypatch.setattr(api.config.load(), "smart_search", False)  # no calls to Apple from tests
    with TestClient(api.app) as c:
        c.app.state.worker.poke = lambda: None  # keep the background worker out of these tests
        yield c


def test_discover_marks_library_books(client):
    data = client.get("/api/discover", params={"q": "dune", "source": "audiobookbay"}).json()
    assert len(data["items"]) == 9
    assert all(i["library"] is None for i in data["items"])

    db.insert(
        {"source": "audiobookbay", "source_url": BOOK_URL, "title": "Dune", "status": "wanted"}
    )
    data = client.get("/api/discover", params={"q": "dune", "source": "audiobookbay"}).json()
    dune = next(i for i in data["items"] if i["url"] == BOOK_URL)
    assert dune["library"]["status"] == "wanted"


def test_add_book_scrapes_details_and_queues(client):
    book = client.post("/api/library", json={"url": BOOK_URL, "download": False}).json()
    assert book["status"] == "wanted"
    assert book["info_hash"] == "061850ead3eb6f1c5c6d8420211b4bbf2d4ee3e2"
    assert book["narrators"][0] == "Scott Brick"

    again = client.post("/api/library", json={"url": BOOK_URL, "download": True}).json()
    assert again["id"] == book["id"]
    assert again["status"] == "queued"


def test_rejects_urls_from_other_hosts(client):
    assert (
        client.get("/api/details", params={"url": "http://169.254.169.254/abss/x"}).status_code
        == 400
    )
    assert (
        client.post("/api/library", json={"url": "https://evil.example/abss/x/"}).status_code == 400
    )


def test_settings_mask_secrets(client):
    s = client.put("/api/settings", json={"rd_api_key": "ABCDEFGH1234"}).json()
    assert s["rd_api_key"].endswith("1234") and "ABCD" not in s["rd_api_key"]
    assert s["rd_configured"] is True
    # Sending the masked value back must not overwrite the real key.
    client.put("/api/settings", json={"rd_api_key": s["rd_api_key"], "max_concurrent": 3})
    from narratarr import config

    assert config.load().rd_api_key == "ABCDEFGH1234"
    assert config.load().max_concurrent == 3


def test_remove_book(client):
    book = client.post("/api/library", json={"url": BOOK_URL, "download": False}).json()
    assert client.delete(f"/api/library/{book['id']}").json() == {"ok": True, "kept": ""}
    assert client.get("/api/library").json() == []
