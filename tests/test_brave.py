import httpx
import pytest
from fastapi.testclient import TestClient

from narratarr import api, brave, config, search

BASE = "https://audiobookbay.lu"


def brave_response(results, altered=None, more=True):
    query = {"original": "x", "more_results_available": more}
    if altered:
        query["altered"] = altered
    return {"query": query, "web": {"results": results}}


RESULTS = [
    {
        "title": "Dune - Frank Herbert Audiobook M4B",
        "url": "https://audiobookbay.lu/abss/dune-frank-herbert-2/",
        "thumbnail": {"src": "https://imgs.search.brave.com/dune.jpg"},
        "page_age": "2025-12-24T20:07:30",
    },
    {
        "title": "Dune Messiah - Frank Herbert Audiobook MP3",
        "url": "https://audiobookbay.is/abss/dune-messiah/",
    },
    {"title": "Sci-Fi Audiobooks", "url": "https://audiobookbay.lu/audio-books/type/sci-fi/"},
    {
        "title": "Dune - Frank Herbert Audiobook M4B",
        "url": "https://audiobookbay.lu/abss/dune-frank-herbert-2/",
    },
    {"title": "Some blog about Dune", "url": "https://example.com/abss/dune/"},
]


def fake_brave(payload, status=200, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_to_listing_cleans_titles_and_rewrites_mirrors():
    listing = brave.to_listing(RESULTS[1], BASE)
    assert listing.url == "https://audiobookbay.lu/abss/dune-messiah/"
    assert (listing.title, listing.author, listing.format) == (
        "Dune Messiah",
        "Frank Herbert",
        "MP3",
    )
    assert brave.to_listing(RESULTS[2], BASE) is None  # category page, not a book
    assert brave.to_listing(RESULTS[4], BASE) is None  # not AudiobookBay
    odd = brave.to_listing(
        {"title": "Project Hail Mary - Andy Weir | AudioBook Bay", "url": f"{BASE}/abss/phm/"}, BASE
    )
    assert (odd.title, odd.author) == ("Project Hail Mary", "Andy Weir")


async def test_search_sends_site_query_and_filters_results():
    seen = []
    res = await brave.search(
        "dune", "KEY", BASE, page=2, client=fake_brave(brave_response(RESULTS), seen=seen)
    )
    req = seen[0]
    assert req.headers["x-subscription-token"] == "KEY"
    assert req.url.params["q"] == "dune site:audiobookbay.lu"
    assert req.url.params["offset"] == "1"
    assert [i.title for i in res["items"]] == ["Dune", "Dune Messiah"]
    assert res["items"][0].cover_url == "https://imgs.search.brave.com/dune.jpg"
    assert res["more"] is True


async def test_search_reports_spelling_correction():
    res = await brave.search(
        "dnue",
        "KEY",
        BASE,
        client=fake_brave(brave_response(RESULTS, altered="dune site:audiobookbay.lu")),
    )
    assert res["altered"] == "dune"


@pytest.mark.parametrize(
    "status,body,message",
    [
        (401, {}, "rejected the API key"),
        (422, {"error": {"code": "SUBSCRIPTION_TOKEN_INVALID"}}, "rejected the API key"),
        (429, {}, "rate limit"),
    ],
)
async def test_search_errors_are_friendly(status, body, message):
    with pytest.raises(brave.BraveError, match=message):
        await brave.search("dune", "KEY", BASE, client=fake_brave(body, status=status))


async def test_missing_key():
    with pytest.raises(brave.BraveError, match="API key"):
        await brave.search("dune", "", BASE)


async def test_brave_smart_search_falls_back_to_apple_correction(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params["q"]
        calls.append(q)
        return httpx.Response(
            200, json=brave_response(RESULTS if q.startswith("project") else [], more=False)
        )

    async def fake_suggestions(query, limit=8, client=None):
        return [{"title": "Project Hail Mary", "author": "Andy Weir", "cover_url": ""}]

    monkeypatch.setattr(search, "suggestions", fake_suggestions)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    res = await search.brave_smart_search("projct hail marry", "KEY", BASE, client=client)
    assert calls == [
        "projct hail marry site:audiobookbay.lu",
        "project hail mary site:audiobookbay.lu",
    ]
    assert res["did_you_mean"] == "project hail mary"
    assert res["total_pages"] == 1
    assert len(res["items"]) == 2


def test_discover_uses_brave_when_selected(monkeypatch):
    config.update({"search_provider": "brave", "brave_api_key": "KEY", "smart_search": False})
    used = {}

    async def fake_brave_search(
        query, api_key, base_url, batch=1, use_suggestions=True, client=None
    ):
        used.update(query=query, key=api_key)
        return {"items": [], "page": 1, "total_pages": 1, "did_you_mean": ""}

    monkeypatch.setattr(search, "brave_smart_search", fake_brave_search)
    with TestClient(api.app) as c:
        assert (
            c.get("/api/discover", params={"q": "dune", "source": "audiobookbay"}).status_code
            == 200
        )
        assert c.get("/api/settings").json()["brave_api_key"].startswith("•")
    assert used == {"query": "dune", "key": "KEY"}
