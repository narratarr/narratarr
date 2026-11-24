import httpx

from narratarr import search
from narratarr.sources.audiobookbay import AudiobookBay
from narratarr.sources.base import Listing

BASE = "https://audiobookbay.lu"

ITUNES_RESULTS = {
    "results": [
        {
            "collectionName": "Project Hail Mary (Unabridged)",
            "artistName": "Andy Weir",
            "artworkUrl100": "https://a/100x100bb.jpg",
        },
        {"collectionName": "Project Hail Mary", "artistName": "Andy Weir"},
        {"collectionName": "Project Hail Mary (Book Summary & Analysis)", "artistName": "Briefly"},
        {"collectionName": "The Martian", "artistName": "Andy Weir"},
    ]
}


def test_normalize_and_clean_title():
    assert search.normalize("Muad’Dib: Crónicas!") == "muaddib cronicas"
    assert search.clean_title("Mistborn [Dramatized Adaptation] (Unabridged)") == "Mistborn"


def test_score_prefers_exact_title():
    exact = Listing(url="a", title="Dune", author="Frank Herbert")
    near = Listing(url="b", title="Dune Messiah", author="Frank Herbert")
    far = Listing(url="c", title="The Martian", author="Andy Weir")
    assert search.score("dune", exact) > search.score("dune", near) > search.score("dune", far)
    assert search.score("dnue frank herbert", exact) > search.score("dnue frank herbert", far)


async def test_suggestions_clean_dedupe_and_skip_summaries():
    search._cache.clear()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=ITUNES_RESULTS))
    )
    out = await search.suggestions("projct hail marry", client=client)
    assert [s["title"] for s in out] == ["Project Hail Mary", "The Martian"]
    assert out[0]["cover_url"] == "https://a/300x300bb.jpg"


def test_correct_words_fixes_typos():
    sugg = [{"title": "Project Hail Mary", "author": "Andy Weir"}]
    assert search.correct_words("projct hail marry", sugg) == "project hail mary"
    sugg = [{"title": "Mistborn", "author": "Brandon Sanderson"}]
    assert search.correct_words("brandon sandersen mistborn", sugg) == "brandon sanderson mistborn"
    extra, label = search.corrected_queries(
        "projct hail marry", [{"title": "Project Hail Mary", "author": "Andy Weir"}]
    )
    assert label == "project hail mary"
    assert "project hail mary" in extra


async def test_smart_search_retries_with_corrected_query(fixture_html, monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("s", "")
        seen.append(q)
        if q == "dune":
            return httpx.Response(200, text=fixture_html("abb_search.html"))
        return httpx.Response(200, text="<html><body>No results</body></html>")

    async def fake_suggestions(query, limit=8, client=None):
        return [{"title": "Dune", "author": "Frank Herbert", "cover_url": ""}]

    monkeypatch.setattr(search, "suggestions", fake_suggestions)
    source = AudiobookBay(BASE, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    result = await search.smart_search(source, "dnue")

    assert "dune" in seen
    assert result["did_you_mean"] == "dune"
    assert result["items"][0]["title"] == "Dune"
    assert len({i["url"] for i in result["items"]}) == len(result["items"])


async def test_smart_search_without_suggestions_is_literal(fixture_html):
    source = AudiobookBay(
        BASE,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, text=fixture_html("abb_search.html"))
            )
        ),
    )
    result = await search.smart_search(source, "dune", use_suggestions=False)
    assert result["did_you_mean"] == ""
    assert result["items"][0]["score"] >= result["items"][-1]["score"]
    assert result["total_pages"] == 14  # 27 source pages, 2 per batch


def test_relevant_drops_loose_suggestions():
    sugg = [
        {"title": "Project Hail Mary", "author": "Andy Weir"},
        {"title": "Atmosphere", "author": "Taylor Jenkins Reid"},
        {"title": "Whalefall", "author": "Daniel Kraus"},
    ]
    assert [s["title"] for s in search.relevant("projct hail marry", sugg)] == ["Project Hail Mary"]


def test_little_words_do_not_create_matches():
    unrelated = Listing(
        url="a", title="Twenty Thousand Leagues Under the Sea", author="Jules Verne"
    )
    poe = Listing(url="b", title="12 Creepy Tales by Edgar Allan Poe", author="Edgar Allan Poe")
    exact = Listing(url="c", title="The Tell-Tale Heart", author="Edgar Allan Poe")
    q = "the tell tale heart"
    assert search.score(q, exact) > search.score(q, poe) > search.score(q, unrelated)
    assert search.score(q, unrelated) < 60
