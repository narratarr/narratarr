"""Ratings: the parser, the resolver's order of preference, the cache, and the weighting.

No test here touches the network: every provider and resolver is handed a MockTransport, and the
rate limiter is given a zero gap so the suite does not sleep for a second a book.
"""

import json
from pathlib import Path

import httpx
import pytest

from narratarr import config
from narratarr.ratings import Rating, compact_count, enrich, make_rating, provider, sort_items
from narratarr.ratings.audnex import AudnexProvider, audible_url, parse_score
from narratarr.ratings.bookmarks_provider import BookmarksProvider
from narratarr.ratings.goodreads import GoodreadsProvider, book_id, parse_book
from narratarr.ratings.match import MIN_COUNT, strict_match, title_matches
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


@pytest.fixture(autouse=True)
def clear_audible_cache():
    """audible.py keeps a module-level response cache; without this, one test's catalogue answer
    is served to the next test that asks the same question."""
    from narratarr import audible

    audible._cache.clear()
    yield


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "ratings.sqlite")


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---- the JSON-LD parser -------------------------------------------------------------------


def test_parser_reads_the_book_block_not_the_first_script():
    got = parse_book(BOOK_HTML)
    assert got == {"score": 4.3, "count": 5044634, "name": "Project Hail Mary",
                   "authors": [], "isbn": "9780593135204"}


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
    assert seen == ["openlibrary.org"]  # one request, no fallbacks


async def test_resolver_rejects_a_different_author(store):
    """Open Library happily returns *a* book for any title; the surname gate is what stops it."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "openlibrary.org" in str(request.url):
            return httpx.Response(200, json={"docs": [{"title": "Project Hail Mary",
                                                       "author_name": ["Someone Else"],
                                                       "isbn": ["9780000000000"]}]})
        raise AssertionError(f"unexpected request: {request.url}")

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


async def test_the_local_match_is_a_fallback_not_the_first_answer(store):
    """A differently-spelled author still asks Open Library first — the local row is the last resort,
    so a stale entry can never override a real lookup."""
    store.put_resolved(key_for("Project Hail Mary", "Andy Weir"), remote_id="54493401")
    tried = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append("openlibrary")
        return httpx.Response(200, json={"docs": []})

    async with client(handler) as c:
        # A different spelling of the title, not of the author: a surname alone is no longer a match.
        hit = await Resolver(store, client=c, gap=0).resolve("Project Hail Mary (Unabridged)", "Andy Weir")
    assert tried == ["openlibrary"], "Open Library must be tried before the local row"
    assert hit["via"] == "local"
    assert hit["url"].endswith("/54493401")


async def test_a_transient_failure_is_not_remembered_as_a_missing_book(store):
    """A 500 at Open Library must not become a week of "this book has no rating"."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert "openlibrary" in str(request.url), request.url
        return httpx.Response(503)              # the site is having a bad minute

    async with client(handler) as c:
        r = Resolver(store, client=c, gap=0)
        assert await r.resolve("Project Hail Mary", "Andy Weir") is None
        assert store.get_resolved(key_for("Project Hail Mary", "Andy Weir")) is None, \
            "an unavailable lookup was cached as a dead end"
        before = calls["n"]
        assert await r.resolve("Project Hail Mary", "Andy Weir") is None
    assert calls["n"] > before, "the next search should try again"


async def test_a_dead_end_is_remembered_so_it_is_not_retried(store):
    """A definite "no such book" is worth remembering; otherwise every search re-asks about it."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert "openlibrary" in str(request.url), request.url
        return httpx.Response(200, json={"docs": []})

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


def test_a_dozen_votes_do_not_outrank_two_million():
    """The weight is the point: a 12-rating 5.0 must not top a 4.3 backed by two million."""
    thin = make_rating(5.0, 12, "u", "goodreads")          # refused: under the floor
    assert thin is None
    thin_ok = make_rating(5.0, 60, "u", "goodreads").to_dict()
    solid = make_rating(4.3, 2_000_000, "u", "goodreads").to_dict()
    assert thin_ok["score"] > solid["score"]
    assert thin_ok["bayes"] < solid["bayes"], "the score is higher but the evidence is not"
    assert sort_items([{"rating": thin_ok}, {"rating": solid}])[0]["rating"]["score"] == 4.3


def test_count_less_ratings_order_by_score_among_themselves():
    """Every Audnex rating has an unknown count, so they all carry the same weight and the score
    decides between them — which is why a weighted sort suits that provider too."""
    high = make_rating(4.9, None, "u", "audnex").to_dict()
    mid = make_rating(4.5, None, "u", "audnex").to_dict()
    low = make_rating(3.1, None, "u", "audnex").to_dict()
    ranked = sort_items([{"rating": mid}, {"rating": low}, {"rating": high}])
    assert [r["rating"]["score"] for r in ranked] == [4.9, 4.5, 3.1]


def test_sorting_keeps_nulls_last():
    """No weighting: there is no count to weight by, and an invented weight is a made-up number."""
    items = [
        {"rating": make_rating(4.1, 500, "u", "goodreads").to_dict()},
        {"rating": None},
        {"rating": make_rating(4.9, 500, "u", "goodreads").to_dict()},
        {"rating": make_rating(3.5, 500, "u", "goodreads").to_dict()},
    ]
    assert [i["rating"] and i["rating"]["score"] for i in sort_items(items)] == [4.9, 4.1, 3.5, None]


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
    config.update({"ratings_provider": "audnex"})
    called = {"n": 0}
    monkeypatch.setattr("narratarr.ratings._enqueue",
                        lambda t, a, asin="": called.__setitem__("n", called["n"] + 1))

    item = enrich({"title": "Unknown Book", "author": "Nobody"})
    assert item["rating"] is None
    assert called["n"] == 1, "a miss should be queued for the background filler"


# ---- the strict gate: one place, every refusal ---------------------------------------------

def test_near_titles_and_series_neighbours_are_refused():
    """The one test that proves "refuse rather than guess". Each line is a way to be plausibly wrong."""
    # exact, and exact through cosmetic differences
    assert strict_match("Catch 22", "Joseph Heller", "Catch-22", ["Joseph Heller"])
    assert strict_match("Devil's Fjord", "David Hewson", "Devil&apos;s Fjord", ["David Hewson"])
    assert strict_match("Etna", "Paul Yoon", "Etna: A Novel", ["Paul Yoon"])  # subtitle without a digit
    assert strict_match("Project Hail Mary", "Weir, Andy", "Project Hail Mary", ["Andy Weir"]), \
        "an inverted author name is the same author"

    # a different book
    assert not strict_match("Etna", "Paul Yoon", "Etna Rising", ["Paul Yoon"]), "near title"
    assert not strict_match("Etna", "Paul Yoon", "Etna (Book 2)", ["Paul Yoon"]), "series neighbour"
    assert not strict_match("Etna", "Paul Yoon", "Etna (The Etna Saga, #1)", ["Paul Yoon"]), "series suffix"
    assert not strict_match("Dune", "Frank Herbert", "Dune Messiah", ["Frank Herbert"]), "next in series"

    # the right book, the wrong person
    assert not strict_match("Etna", "Paul Yoon", "Etna", ["Yoon"]), "surname only"
    assert not strict_match("Etna", "Paul Yoon", "Etna", ["Paul Auster"]), "different author"
    assert not strict_match("Etna", "Paul Yoon", "Etna", []), "no author at all is not a match"


# ---- the floor lives in one place -----------------------------------------------------------

def test_the_vote_floor_is_applied_once():
    assert make_rating(5.0, 12, "u", "goodreads") is None, "12 ratings is noise"
    assert make_rating(3.2, MIN_COUNT, "u", "goodreads") is not None
    assert make_rating(None, 5000, "u", "goodreads") is None, "no score is not a zero score"


def test_a_rating_with_no_count_is_expressible():
    """Audnex publishes a bare average and no count. The contract has to carry that, and the vote
    floor must not throw it away: a floor needs a count to apply to."""
    r = make_rating(4.9, None, audible_url("B08GB2RLKM", "uk"), "audnex",
                    label="Audible", tooltip="4.9 average on Audible (ASIN B08GB2RLKM)")
    assert r is not None and r.score == 4.9 and r.count is None
    d = r.to_dict()
    assert d["count"] is None
    assert d["label"] == "Audible"          # the small source mark beside the score
    assert d["tooltip"] == "4.9 average on Audible (ASIN B08GB2RLKM)"


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
    config.update({"ratings_provider": "audnex"})
    called = {"n": 0}
    monkeypatch.setattr("narratarr.ratings._enqueue",
                        lambda t, a, asin="": called.__setitem__("n", called["n"] + 1))

    item = enrich({"title": "Unknown Book", "author": "Nobody"})
    assert item["rating"] is None
    assert called["n"] == 1, "a miss should be queued for the background filler"


# ---- the strict gate: one place, every refusal ---------------------------------------------

def test_near_titles_and_series_neighbours_are_refused():
    """The one test that proves "refuse rather than guess". Each line is a way to be plausibly wrong."""
    # exact, and exact through cosmetic differences
    assert strict_match("Catch 22", "Joseph Heller", "Catch-22", ["Joseph Heller"])
    assert strict_match("Devil's Fjord", "David Hewson", "Devil&apos;s Fjord", ["David Hewson"])
    assert strict_match("Etna", "Paul Yoon", "Etna: A Novel", ["Paul Yoon"])  # subtitle without a digit
    assert strict_match("Project Hail Mary", "Weir, Andy", "Project Hail Mary", ["Andy Weir"]), \
        "an inverted author name is the same author"

    # a different book
    assert not strict_match("Etna", "Paul Yoon", "Etna Rising", ["Paul Yoon"]), "near title"
    assert not strict_match("Etna", "Paul Yoon", "Etna (Book 2)", ["Paul Yoon"]), "series neighbour"
    assert not strict_match("Etna", "Paul Yoon", "Etna (The Etna Saga, #1)", ["Paul Yoon"]), "series suffix"
    assert not strict_match("Dune", "Frank Herbert", "Dune Messiah", ["Frank Herbert"]), "next in series"

    # the right book, the wrong person
    assert not strict_match("Etna", "Paul Yoon", "Etna", ["Yoon"]), "surname only"
    assert not strict_match("Etna", "Paul Yoon", "Etna", ["Paul Auster"]), "different author"
    assert not strict_match("Etna", "Paul Yoon", "Etna", []), "no author at all is not a match"


# ---- the floor lives in one place -----------------------------------------------------------

def test_the_vote_floor_is_applied_once():
    assert make_rating(5.0, 12, "u", "goodreads") is None, "12 ratings is noise"
    assert make_rating(3.2, MIN_COUNT, "u", "goodreads") is not None
    assert make_rating(None, 5000, "u", "goodreads") is None, "no score is not a zero score"


def test_a_rating_with_no_count_is_expressible():
    """Audnex returns a bare number with no count behind it. The contract has to carry that, and
    the vote floor must not throw it away: a floor needs a count to apply to."""
    r = make_rating(4.9, None, "https://api.audnex.us/books/B00TEST", "audnex")
    assert r is not None and r.score == 4.9 and r.count is None
    assert r.tooltip == "4.90 average on audnex"          # no invented count
    d = r.to_dict()
    assert d["count"] is None and d["label"] == ""        # the pill shows the score alone
    assert d["display"]


AUDNEX_ASIN = "B08GB2RLKM"


def audnex_record() -> dict:
    return json.loads((FIXTURES / "audnex_book_uk.json").read_text())


def audnex_handler(record: dict | None = None, status: int = 200, seen: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(request.url))
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(200, json=record if record is not None else audnex_record())
    return handler


def test_a_normal_audnex_record():
    got = parse_score(audnex_record())
    assert got == 4.9
    assert audnex_record()["rating"] == "4.9"  # it really is a string in the payload


def test_zero_point_zero_is_no_rating_not_zero_stars():
    """`"0.0"` is the API saying nobody has rated it yet. As a number it would be the worst score
    on every page and would show as half a star, which is wrong twice over."""
    assert parse_score({"rating": "0.0"}) is None
    assert parse_score({"rating": ""}) is None
    assert parse_score({"rating": None}) is None
    assert parse_score({}) is None
    assert parse_score(None) is None
    assert parse_score({"rating": "6.2"}) is None  # outside any real scale


async def test_region_is_sent_and_matters(store):
    seen: list[str] = []

    async with client(audnex_handler(seen=seen)) as c:
        p = AudnexProvider(store, client=c, region="uk")
        rating = await p.fetch_and_store("Project Hail Mary", "Andy Weir", asin=AUDNEX_ASIN)
    assert rating.score == 4.9 and rating.count is None
    assert "region=uk" in seen[0]
    assert rating.url == "https://www.audible.co.uk/pd/B08GB2RLKM"
    assert rating.tooltip == "4.9 average on Audible (ASIN B08GB2RLKM)"


async def test_a_404_in_the_configured_region_falls_back_to_the_asin_marketplace(store):
    """Measured on the real API: an ASIN answers in its own marketplace and 404s everywhere else.
    Since every ASIN we hold came out of audible.py, its region is known, not guessed."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "region=uk" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, json=audnex_record())

    async with client(handler) as c:
        p = AudnexProvider(store, client=c, region="uk")
        rating = await p.fetch_and_store("Project Hail Mary", "Andy Weir", asin=AUDNEX_ASIN)
    assert rating is not None and rating.score == 4.9
    assert len(seen) == 2, seen          # uk first, then the ASIN's own marketplace
    assert "region=uk" in seen[0]


async def test_a_404_is_a_miss_and_is_not_remembered_as_a_rating(store):
    async with client(audnex_handler(status=404)) as c:
        p = AudnexProvider(store, client=c, region="uk")
        assert await p.fetch_and_store("Project Hail Mary", "Andy Weir", asin=AUDNEX_ASIN) is None
    assert store.count("audnex") == {}, "a 404 says nothing about the book; nothing to cache"


async def test_an_unrated_book_is_remembered_without_a_score(store):
    record = audnex_record() | {"rating": "0.0"}
    async with client(audnex_handler(record=record)) as c:
        p = AudnexProvider(store, client=c, region="uk")
        assert await p.fetch_and_store("Project Hail Mary", "Andy Weir", asin=AUDNEX_ASIN) is None
    assert store.count("audnex") == {"unrated": 1}


# ---- title -> ASIN resolution ----------------------------------------------------------------

def audible_payload(title: str, author: str, asin: str = AUDNEX_ASIN) -> dict:
    """Shaped as Audible's API answers, because audible._keep filters on kind and language."""
    return {"total_results": 1, "products": [{
        "asin": asin, "title": title, "authors": [{"name": author}],
        "content_delivery_type": "SinglePartBook", "language": "english", "format_type": "unabridged",
    }]}


def audible_search_handler(title: str, author: str, seen: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(request.url))
        if "api.audible" in str(request.url):
            return httpx.Response(200, json=audible_payload(title, author))
        return httpx.Response(200, json=audnex_record())
    return handler


async def test_an_exact_match_resolves_to_the_asin(store):
    async with client(audible_search_handler("Project Hail Mary", "Andy Weir")) as c:
        p = AudnexProvider(store, client=c, region="uk")
        rating = await p.fetch_and_store("Project Hail Mary", "Andy Weir")
    assert rating is not None and rating.score == 4.9
    assert store.get_resolved(key_for("Project Hail Mary", "Andy Weir"))["remote_id"] == AUDNEX_ASIN


async def test_a_near_title_is_refused(store):
    async with client(audible_search_handler("Project Hail Mary: A Novel of Discovery", "Andy Weir")) as c:
        p = AudnexProvider(store, client=c, region="uk")
        assert await p.fetch_and_store("Project Hail Mary", "Andy Weir") is None


async def test_a_different_surname_is_refused(store):
    async with client(audible_search_handler("Project Hail Mary", "Somebody Else")) as c:
        p = AudnexProvider(store, client=c, region="uk")
        assert await p.fetch_and_store("Project Hail Mary", "Andy Weir") is None


async def test_resolution_happens_once_and_then_costs_nothing(store):
    seen: list[str] = []
    async with client(audible_search_handler("Project Hail Mary", "Andy Weir", seen=seen)) as c:
        p = AudnexProvider(store, client=c, region="uk")
        assert await p.fetch_and_store("Project Hail Mary", "Andy Weir") is not None
        first = len(seen)

        def boom(request):  # neither Audible nor Audnex may be asked again
            raise AssertionError(f"resolution repeated: {request.url}")

        p2 = AudnexProvider(store, client=httpx.AsyncClient(transport=httpx.MockTransport(boom)), region="uk")
        assert p2.cached("Project Hail Mary", "Andy Weir").score == 4.9
    assert len(seen) == first


async def test_a_resolved_miss_is_remembered(store):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"products": []})

    async with client(handler) as c:
        p = AudnexProvider(store, client=c, region="uk")
        assert await p.fetch_and_store("Nowhere Book", "Nobody Atall") is None
        first = calls["n"]
        assert await p.fetch_and_store("Nowhere Book", "Nobody Atall") is None
    assert calls["n"] == first, "a miss was retried immediately instead of being remembered"
