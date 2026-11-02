"""Ratings: the parser, the resolver's order of preference, the cache, and the weighting.

No test here touches the network: every provider and resolver is handed a MockTransport, and the
rate limiter is given a zero gap so the suite does not sleep for a second a book.
"""

import json
from pathlib import Path

import httpx
import pytest

from narratarr import config
from narratarr.ratings import MIN_COUNT, Rating, compact_count, enrich, sort_items
from narratarr.ratings.bookmarks_provider import BookmarksProvider
from narratarr.ratings.goodreads import GoodreadsProvider, book_id, parse_book
from narratarr.ratings.resolver import Resolver
from narratarr.ratings.store import Store, key_for

FIXTURES = Path(__file__).parent / "fixtures"

BOOK_HTML = """<!doctype html><html><head>
<script type="application/ld+json">{"@type":"BreadcrumbList","itemListElement":[]}</script>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Book","name":"Project Hail Mary",
 "isbn":"9780593135204",
 "aggregateRating":{"@type":"AggregateRating","ratingValue":"4.30","ratingCount":"5044634"}}
</script></head><body></body></html>"""

THIN_HTML = BOOK_HTML.replace('"5044634"', '"12"').replace('"4.30"', '"5.0"')


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "ratings.sqlite")


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---- the JSON-LD parser -------------------------------------------------------------------


def test_parser_reads_the_book_block_not_the_first_script():
    got = parse_book(BOOK_HTML)
    assert got == {"score": 4.3, "count": 5044634, "name": "Project Hail Mary", "isbn": "9780593135204"}


def test_parser_returns_nothing_for_a_page_without_a_rating():
    assert parse_book("<html><body>not a book page</body></html>") is None
    assert parse_book("") is None
    assert parse_book('<script type="application/ld+json">{"@type":"Book"}</script>') is None


def test_book_id_from_a_canonical_url():
    assert book_id("https://www.goodreads.com/book/show/54493401-project-hail-mary") == "54493401"
    assert book_id("https://www.goodreads.com/book/isbn/9780593135204") == ""


# ---- the resolver's order of preference -----------------------------------------------------


def _openlibrary(isbn="9780593135204", title="Project Hail Mary", author="Andy Weir"):
    return {"docs": [{"title": title, "author_name": [author], "isbn": [isbn, "0593135202"]}]}


async def test_resolver_prefers_the_isbn_from_open_library(store):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        assert "openlibrary.org" in str(request.url)
        return httpx.Response(200, json=_openlibrary())

    async with client(handler) as c:
        hit = await Resolver(store, client=c, gap=0).resolve("Project Hail Mary", "Andy Weir")
    assert hit["via"] == "openlibrary"
    assert hit["isbn"] == "9780593135204"
    assert hit["url"] == "https://www.goodreads.com/book/isbn/9780593135204"
    assert seen == ["openlibrary.org"]  # wikidata was never needed


async def test_resolver_falls_through_to_wikidata(store):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "openlibrary.org" in url:
            return httpx.Response(200, json={"docs": []})
        if "wbsearchentities" in url:
            return httpx.Response(200, json={"search": [{"id": "Q123", "label": "Project Hail Mary"}]})
        return httpx.Response(200, json={"entities": {"Q123": {"claims": {
            "P2969": [{"mainsnak": {"datavalue": {"value": "54493401"}}}]}}}})

    async with client(handler) as c:
        hit = await Resolver(store, client=c, gap=0).resolve("Project Hail Mary", "Andy Weir")
    assert hit["via"] == "wikidata"
    assert hit["url"] == "https://www.goodreads.com/book/show/54493401"


async def test_resolver_rejects_a_different_author(store):
    """Open Library happily returns *a* book for any title; the surname gate is what stops it."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "openlibrary.org" in str(request.url):
            return httpx.Response(200, json={"docs": [{"title": "Project Hail Mary",
                                                       "author_name": ["Someone Else"],
                                                       "isbn": ["9780000000000"]}]})
        return httpx.Response(200, json={"search": []})

    async with client(handler) as c:
        hit = await Resolver(store, client=c, gap=0).resolve("Project Hail Mary", "Andy Weir")
    assert hit is None


async def test_a_book_we_have_already_resolved_costs_nothing(store):
    store.put_resolved(key_for("Project Hail Mary", "Andy Weir"), remote_id="54493401")

    def handler(request: httpx.Request) -> httpx.Response:  # must not be called
        raise AssertionError(f"resolver went to the network for a cached book: {request.url}")

    async with client(handler) as c:
        hit = await Resolver(store, client=c, gap=0).resolve("Project Hail Mary", "Andy Weir")
    assert hit["via"] == "cache"
    assert hit["url"].endswith("/54493401")


async def test_the_local_match_is_third_not_first(store):
    """A differently-spelled author still tries the network first — the cheap local match is the
    last resort, so a stale local row can never override a real lookup."""
    store.put_resolved(key_for("Project Hail Mary", "Andy Weir"), remote_id="54493401")
    tried = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        tried.append("openlibrary" if "openlibrary" in url else "wikidata")
        if "openlibrary" in url:
            return httpx.Response(200, json={"docs": []})
        return httpx.Response(200, json={"search": []})

    async with client(handler) as c:
        hit = await Resolver(store, client=c, gap=0).resolve("Project Hail Mary", "A. Weir")
    assert tried == ["openlibrary", "wikidata"], "the local match must come after both lookups"
    assert hit["via"] == "local"
    assert hit["url"].endswith("/54493401")


async def test_a_transient_failure_is_not_remembered_as_a_missing_book(store):
    """A 500 at Open Library must not become a week of "this book has no rating"."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if "openlibrary" in str(request.url):
            return httpx.Response(503)          # the site is having a bad minute
        return httpx.Response(200, json={"search": []})

    async with client(handler) as c:
        r = Resolver(store, client=c, gap=0)
        assert await r.resolve("Project Hail Mary", "Andy Weir") is None
        assert store.get_resolved(key_for("Project Hail Mary", "Andy Weir")) is None, \
            "an unavailable lookup was cached as a dead end"
        before = calls["n"]
        assert await r.resolve("Project Hail Mary", "Andy Weir") is None
    assert calls["n"] > before, "the next search should try again"


async def test_a_dead_end_is_remembered_so_it_is_not_retried(store):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"docs": []} if "openlibrary" in str(request.url) else {"search": []})

    async with client(handler) as c:
        r = Resolver(store, client=c, gap=0)
        assert await r.resolve("No Such Book", "Nobody") is None
        first = calls["n"]
        assert await r.resolve("No Such Book", "Nobody") is None
    assert calls["n"] == first, "a book that failed resolution was resolved again"


# ---- the cache ------------------------------------------------------------------------------


async def test_second_lookup_costs_zero_requests(store):
    def handler(request: httpx.Request) -> httpx.Response:
        if "openlibrary.org" in str(request.url):
            return httpx.Response(200, json=_openlibrary())
        return httpx.Response(200, content=BOOK_HTML)

    async with client(handler) as c:
        p = GoodreadsProvider(store, client=c, gap=0)
        first = await p.fetch_and_store("Project Hail Mary", "Andy Weir")
        assert first.score == 4.3 and first.count == 5044634

        def boom(request):  # any request at all is a failure now
            raise AssertionError(f"cache miss: {request.url}")

        p2 = GoodreadsProvider(store, client=httpx.AsyncClient(transport=httpx.MockTransport(boom)), gap=0)
        cached = p2.cached("Project Hail Mary", "Andy Weir")
    assert cached.score == 4.3
    assert cached.label == "5.0M"
    assert cached.tooltip == "4.30 average from 5,044,634 ratings on Goodreads"
    assert store.count("goodreads") == {"rated": 1}


async def test_a_bot_challenge_is_not_recorded_as_a_book_with_no_rating(store):
    """Goodreads answers 202 with an empty body once it decides you are a robot. If that were cached
    as "unrated", every book seen during the block would stay blank for a week."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "openlibrary.org" in str(request.url):
            return httpx.Response(200, json=_openlibrary())
        return httpx.Response(202, content=b"")

    async with client(handler) as c:
        p = GoodreadsProvider(store, client=c, gap=0)
        assert await p.fetch_and_store("Project Hail Mary", "Andy Weir") is None
    assert store.count("goodreads") == {}, "a bot challenge must leave the cache untouched"


async def test_a_thinly_rated_book_is_not_a_rating(store):
    def handler(request: httpx.Request) -> httpx.Response:
        if "openlibrary.org" in str(request.url):
            return httpx.Response(200, json=_openlibrary())
        return httpx.Response(200, content=THIN_HTML)

    async with client(handler) as c:
        p = GoodreadsProvider(store, client=c, gap=0)
        assert await p.fetch_and_store("Project Hail Mary", "Andy Weir") is None
    assert store.count("goodreads") == {"unrated": 1}  # remembered, so it is not asked again


# ---- display and sorting -------------------------------------------------------------------


def test_compact_count():
    assert compact_count(5044634) == "5.0M"
    assert compact_count(12345) == "12.3K"
    assert compact_count(842) == "842"
    assert compact_count(None) == ""


def test_weighting_stops_a_dozen_votes_topping_the_list():
    """A 12-vote 5.0 must not outrank two million votes at 4.3."""
    thin = Rating(score=5.0, count=12, source="goodreads").to_dict()
    solid = Rating(score=4.3, count=2_000_000, source="goodreads").to_dict()
    assert thin["bayes"] < solid["bayes"]
    ranked = sort_items([{"rating": thin, "n": "thin"}, {"rating": solid, "n": "solid"},
                         {"rating": None, "n": "none"}])
    assert [i["n"] for i in ranked] == ["solid", "thin", "none"]


def test_score_text_matches_each_provider():
    assert Rating(4.3, 5044634, source="goodreads").to_dict()["score_text"] == "★ 4.30"
    assert Rating(3.9, 120, source="bookmarks").to_dict()["score_text"] == "★ 3.9"


# ---- Bookmarks unchanged -------------------------------------------------------------------


def test_bookmarks_still_reads_its_own_dataset(monkeypatch):
    from narratarr import bookmarks

    monkeypatch.setattr(bookmarks, "_entries", [{
        "title": "Project Hail Mary", "author": "Andy Weir", "slug": "phm",
        "url": "https://bookmarks.reviews/reviews/all/phm/",
        "flags": {"rave": 5, "positive": 2, "mixed": 1, "pan": 0},
        "consensus": "Rave", "R": 3.8,
    }])
    rating = BookmarksProvider().cached("Project Hail Mary", "Andy Weir")
    assert rating.score == 3.8 and rating.scale == 4.0
    assert rating.label == "Rave"          # the consensus, as before
    assert rating.to_dict()["R"] == 3.8    # the old field the UI used to read


# ---- the page path -------------------------------------------------------------------------


def test_enrich_never_blocks_and_always_sets_the_key(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "_settings", None)
    config.update({"ratings_provider": "goodreads"})
    called = {"n": 0}
    monkeypatch.setattr("narratarr.ratings._enqueue", lambda t, a: called.__setitem__("n", called["n"] + 1))

    item = enrich({"title": "Unknown Book", "author": "Nobody"})
    assert item["rating"] is None
    assert called["n"] == 1, "a miss should be queued for the background filler"
