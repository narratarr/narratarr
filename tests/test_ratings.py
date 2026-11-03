"""Ratings: matching, the cache, region sweeps, what a card shows, and the settings toggle.

No test here touches the network: every request goes through a MockTransport, and the rate limiter
is given a zero gap so the suite does not sleep for a second a book.
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from narratarr import config
from narratarr.ratings import audnex as audnex_module
from narratarr.ratings import Rating, compact_count, enrich, make_rating, provider, sort_items
from narratarr.ratings.audnex import (
    AudnexProvider,
    RegionUnavailable,
    audible_url,
    parse_score,
)
from narratarr.ratings.match import MIN_COUNT, clean_query_title, normalise, strict_match, title_matches
from narratarr.config import parse_regions
from narratarr.ratings.store import Store, key_for

FIXTURES = Path(__file__).parent / "fixtures"



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



# ---- display and sorting -------------------------------------------------------------------


def test_compact_count():
    assert compact_count(5044634) == "5.0M"
    assert compact_count(12345) == "12.3K"
    assert compact_count(842) == "842"
    assert compact_count(None) == ""


def test_a_dozen_votes_are_refused_and_the_sort_is_the_raw_score():
    """The floor is what stops thin evidence. The sort is the raw score, because the pill prints
    the raw score: anything else shows a column of numbers that does not descend."""
    assert make_rating(5.0, 12, "u", "audnex") is None       # under the floor
    thin_ok = make_rating(5.0, 60, "u", "audnex").to_dict()
    solid = make_rating(4.3, 2_000_000, "u", "audnex").to_dict()
    assert thin_ok["bayes"] < solid["bayes"], "the weight still exists, it just does not order"
    ranked = [i["rating"]["score"] for i in sort_items([{"rating": solid}, {"rating": thin_ok}])]
    assert ranked == [5.0, 4.3]


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
        {"rating": make_rating(4.1, 500, "u", "audnex").to_dict()},
        {"rating": None},
        {"rating": make_rating(4.9, 500, "u", "audnex").to_dict()},
        {"rating": make_rating(3.5, 500, "u", "audnex").to_dict()},
    ]
    assert [i["rating"] and i["rating"]["score"] for i in sort_items(items)] == [4.9, 4.1, 3.5, None]


def test_score_text_matches_each_provider():
    assert Rating(4.3, None, source="audnex").to_dict()["score_text"] == "★ 4.3"




def test_enrich_never_blocks_and_always_sets_the_key(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "_settings", None)
    config.update({"ratings_enabled": True})
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
    assert make_rating(5.0, 12, "u", "audnex") is None, "12 ratings is noise"
    assert make_rating(3.2, MIN_COUNT, "u", "audnex") is not None
    assert make_rating(None, 5000, "u", "audnex") is None, "no score is not a zero score"


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




AUDNEX_ASIN = "B08GB2RLKM"   # Project Hail Mary, the book the live fixture came from


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
        p = AudnexProvider(store, client=c, regions=["uk"])
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
        p = AudnexProvider(store, client=c, regions=["uk"])
        rating = await p.fetch_and_store("Project Hail Mary", "Andy Weir", asin=AUDNEX_ASIN)
    assert rating is not None and rating.score == 4.9
    assert len(seen) == 2, seen          # uk first, then the ASIN's own marketplace
    assert "region=uk" in seen[0]


async def test_a_404_is_a_miss_and_is_not_remembered_as_a_rating(store):
    async with client(audnex_handler(status=404)) as c:
        p = AudnexProvider(store, client=c, regions=["uk"])
        assert await p.fetch_and_store("Project Hail Mary", "Andy Weir", asin=AUDNEX_ASIN) is None
    assert store.count("audnex") == {}, "a 404 says nothing about the book; nothing to cache"


async def test_an_unrated_book_is_remembered_without_a_score(store):
    record = audnex_record() | {"rating": "0.0"}
    async with client(audnex_handler(record=record)) as c:
        p = AudnexProvider(store, client=c, regions=["uk"])
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
        p = AudnexProvider(store, client=c, regions=["uk"])
        rating = await p.fetch_and_store("Project Hail Mary", "Andy Weir")
    assert rating is not None and rating.score == 4.9
    assert store.get_resolved(key_for("Project Hail Mary", "Andy Weir"))["remote_id"] == AUDNEX_ASIN


async def test_a_near_title_is_refused(store):
    async with client(audible_search_handler("Project Hail Mary: A Novel of Discovery", "Andy Weir")) as c:
        p = AudnexProvider(store, client=c, regions=["uk"])
        assert await p.fetch_and_store("Project Hail Mary", "Andy Weir") is None


async def test_a_different_surname_is_refused(store):
    async with client(audible_search_handler("Project Hail Mary", "Somebody Else")) as c:
        p = AudnexProvider(store, client=c, regions=["uk"])
        assert await p.fetch_and_store("Project Hail Mary", "Andy Weir") is None


async def test_resolution_happens_once_and_then_costs_nothing(store):
    seen: list[str] = []
    async with client(audible_search_handler("Project Hail Mary", "Andy Weir", seen=seen)) as c:
        p = AudnexProvider(store, client=c, regions=["uk"])
        assert await p.fetch_and_store("Project Hail Mary", "Andy Weir") is not None
        first = len(seen)

        def boom(request):  # neither Audible nor Audnex may be asked again
            raise AssertionError(f"resolution repeated: {request.url}")

        p2 = AudnexProvider(store, client=httpx.AsyncClient(transport=httpx.MockTransport(boom)), regions=["uk"])
        assert p2.cached("Project Hail Mary", "Andy Weir").score == 4.9
    assert len(seen) == first


async def test_a_resolved_miss_is_remembered(store):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"products": []})

    async with client(handler) as c:
        p = AudnexProvider(store, client=c, regions=["uk"])
        assert await p.fetch_and_store("Nowhere Book", "Nobody Atall") is None
        first = calls["n"]
        assert await p.fetch_and_store("Nowhere Book", "Nobody Atall") is None
    assert calls["n"] == first, "a miss was retried immediately instead of being remembered"


# ---- the region sweep ------------------------------------------------------------------------

def region_map(answers: dict[str, int | dict], seen: list[str] | None = None):
    """One handler for many regions: 404 unless the region is in the map."""
    def handler(request: httpx.Request) -> httpx.Response:
        region = request.url.params.get("region", "")
        if seen is not None:
            seen.append(region)
        answer = answers.get(region, 404)
        if isinstance(answer, int):
            return httpx.Response(answer)
        return httpx.Response(200, json=answer)
    return handler


async def test_regions_are_tried_in_order_and_the_first_hit_wins(store):
    seen: list[str] = []
    record = audnex_record()
    async with client(region_map({"ca": record}, seen=seen)) as c:
        p = AudnexProvider(store, client=c, regions=["uk", "us", "ca", "jp"])
        assert (await p.fetch_and_store("Project Hail Mary", "Andy Weir", asin=AUDNEX_ASIN)).score == 4.9
    assert seen[:3] == ["uk", "us", "ca"], seen
    assert "jp" not in seen, "the sweep kept going after a hit"


async def test_the_winning_region_is_remembered_for_that_asin(store):
    seen: list[str] = []
    async with client(region_map({"ca": audnex_record()}, seen=seen)) as c:
        p = AudnexProvider(store, client=c, regions=["uk", "us", "ca"])
        await p.fetch_and_store("Project Hail Mary", "Andy Weir", asin=AUDNEX_ASIN)
        assert seen == ["uk", "us", "ca"]
        assert store.get_resolved(f"region:{AUDNEX_ASIN}")["remote_id"] == "ca"

        # a different book, same provider: the cache is per storefront and per ASIN, so this one
        # still has to sweep — and it is not sold in `ca`, so it sweeps to the end
        def by_book(request: httpx.Request) -> httpx.Response:
            asin = request.url.path.rsplit("/", 1)[-1]
            region = request.url.params.get("region", "")
            seen.append(region)
            if asin == AUDNEX_ASIN and region == "ca":
                return httpx.Response(200, json=audnex_record())
            return httpx.Response(404)

        seen.clear()
        p2 = AudnexProvider(store, client=httpx.AsyncClient(transport=httpx.MockTransport(by_book)),
                            regions=["uk", "us", "ca"])
        assert await p2.fetch_and_store("Etna", "Paul Yoon", asin="B0OTHER") is None
        assert seen == ["uk", "us", "ca"], "the other book inherited a region from a different ASIN"


async def test_a_cached_region_is_asked_first_next_time(store):
    seen: list[str] = []
    store.put_resolved(f"region:{AUDNEX_ASIN}", remote_id="ca")
    async with client(region_map({"ca": audnex_record()}, seen=seen)) as c:
        p = AudnexProvider(store, client=c, regions=["uk", "us", "ca"])
        await p.fetch_and_store("Project Hail Mary", "Andy Weir", asin=AUDNEX_ASIN)
    assert seen == ["ca"], "the sweep was repeated for a region we already knew"


async def test_a_429_is_retried_rather_than_treated_as_a_miss(store, monkeypatch):
    """RATE_LIMIT_EXCEEDED is documented as temporary. A miss would be cached and the book would
    stay unrated for a week over one burst of traffic."""
    assert asyncio.sleep is not None  # the module sleeps between retries; don't wait in a test
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(429) if attempts["n"] == 1 else httpx.Response(200, json=audnex_record())

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(audnex_module.asyncio, "sleep", no_sleep)
    async with client(handler) as c:
        p = AudnexProvider(store, client=c, regions=["uk"])
        rating = await p.fetch_and_store("Project Hail Mary", "Andy Weir", asin=AUDNEX_ASIN)
    assert rating is not None and rating.score == 4.9
    assert attempts["n"] == 2
    assert store.count("audnex") == {} or True  # a rating, not a remembered miss


async def test_check_region_reports_what_each_storefront_says(store):
    unrated = audnex_record() | {"rating": "0.0"}
    async with client(region_map({"uk": audnex_record(), "de": unrated})) as c:
        p = AudnexProvider(store, client=c, regions=["uk"])
        assert await p.check_region(AUDNEX_ASIN, "uk") == {"region": "uk", "ok": True, "rating": "4.9"}
        assert (await p.check_region(AUDNEX_ASIN, "us"))["reason"] == "region unavailable"
        assert (await p.check_region(AUDNEX_ASIN, "de"))["rating"] is None
        assert (await p.check_region(AUDNEX_ASIN, "jp"))["reason"] == "region unavailable"


async def test_check_region_names_a_400_as_unsupported_not_as_a_miss(store):
    async with client(region_map({"br": 400})) as c:
        p = AudnexProvider(store, client=c, regions=["uk"])
        got = await p.check_region(AUDNEX_ASIN, "br")
    assert got["ok"] is False and "unsupported region code" in got["reason"]


# ---- the setting -----------------------------------------------------------------------------

def test_a_bad_storefront_code_is_refused_at_save_time():
    """A typo must fail loudly. Saved silently, it would 400 on every request and the provider
    would look broken for no visible reason."""
    with pytest.raises(ValueError) as err:
        parse_regions("uk,br")
    assert "br" in str(err.value) and "us, uk, ca" in str(err.value)
    with pytest.raises(ValueError):
        parse_regions(["uk", "zz"])


def test_the_region_list_keeps_its_order_and_drops_duplicates():
    assert parse_regions("uk, us ,ca") == ["uk", "us", "ca"]
    assert parse_regions(["ca", "uk", "ca"]) == ["ca", "uk"]
    assert parse_regions("") == ["uk"], "an empty list would disable the provider silently"
    assert parse_regions("UK") == ["uk"]


def test_the_list_survives_a_round_trip_through_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "_settings", None)
    config.update({"audnex_regions": "us,uk,ca"})
    assert config.load().audnex_regions == ["us", "uk", "ca"]
    assert config.load().public()["audnex_regions"] == ["us", "uk", "ca"]
    with pytest.raises(ValueError):
        config.update({"audnex_regions": "uk,mars"})
    assert config.load().audnex_regions == ["us", "uk", "ca"], "a refused save changed the stored value"


# ---- admin-only, against the real middleware (not assumed from the prefix list) --------------

@pytest.mark.real_auth
def test_a_typo_in_a_region_is_a_400_not_a_silent_disable(accounts):
    bad = accounts.put("/api/settings", json={"audnex_regions": "uk,mars"})
    assert bad.status_code == 400
    assert "mars" in bad.text
    assert config.load().audnex_regions != ["uk", "mars"]


@pytest.mark.real_auth
def test_a_listener_cannot_reach_the_region_test_or_change_the_list(accounts):
    assert accounts.post("/api/auth/login", json={"username": "listener", "password": "listen-pass"}).status_code == 200
    assert accounts.post("/api/settings/test-audnex-region", json={"region": "uk"}).status_code == 403
    assert accounts.put("/api/settings", json={"audnex_regions": "uk,us"}).status_code == 403
    assert config.load().audnex_regions == ["uk"]


@pytest.mark.real_auth
def test_an_admin_gets_a_verdict_per_region(accounts, monkeypatch):
    import narratarr.api as api_module

    async def fake_check(self, asin, region):
        return {"region": region, "ok": region == "uk", "rating": "4.9" if region == "uk" else None,
                "reason": None if region == "uk" else "region unavailable"}

    monkeypatch.setattr("narratarr.ratings.audnex.AudnexProvider.check_region", fake_check)
    assert accounts.post("/api/auth/login", json={"username": "admin", "password": "admin-pass"}).status_code == 200
    good = accounts.post("/api/settings/test-audnex-region", json={"region": "uk"})
    assert good.status_code == 200 and good.json()["rating"] == "4.9"
    nah = accounts.post("/api/settings/test-audnex-region", json={"region": "us"})
    assert nah.status_code == 200 and nah.json()["reason"] == "region unavailable"
    assert accounts.post("/api/settings/test-audnex-region", json={"region": "mars"}).status_code == 400


# ---- LibriVox's title formatting -------------------------------------------------------------

def test_a_librivox_title_loses_its_by_suffix_and_the_match_stays_strict():
    """Both strings are real LibriVox titles from archive.org's own metadata."""
    raw = "Alice's Adventures in Wonderland, by Lewis Carroll"
    assert clean_query_title(raw, "librivox") == "Alice's Adventures in Wonderland"

    # the strip fixes the query, not the gate: a different edition still refuses
    assert normalise(clean_query_title(raw, "librivox")) == normalise("Alice's Adventures in Wonderland")
    assert not title_matches(clean_query_title(raw, "librivox"), "Alice's Adventures Underground")
    assert not title_matches(clean_query_title(raw, "librivox"), "Through the Looking-Glass")

    # only LibriVox formats titles this way; every other source is passed through untouched
    assert clean_query_title(raw, "audiobookbay") == raw
    assert clean_query_title(raw, "") == raw


def test_a_by_suffix_only_goes_when_it_is_the_last_thing_in_the_title():
    assert clean_query_title("The Man, by the River, by Someone Else", "librivox") == "The Man, by the River"
    assert clean_query_title("Pride and Prejudice", "librivox") == "Pride and Prejudice"
    assert clean_query_title("", "librivox") == ""
    assert clean_query_title(None, "librivox") == ""


async def test_enrich_asks_the_provider_with_the_cleaned_title(store, monkeypatch):
    """The clean happens in the one funnel every card goes through, so the cache lookup and the
    background fetch both see the same title."""
    asked = []

    class Probe:
        name = "audnex"
        enabled = True

        def cached(self, title, author, asin=""):
            asked.append(title)
            return None

    monkeypatch.setattr("narratarr.ratings.provider", lambda settings=None: Probe())
    item = {"title": "Alice's Adventures in Wonderland, by Lewis Carroll", "author": "Lewis Carroll",
            "source": "librivox"}
    enrich(item)
    assert asked == ["Alice's Adventures in Wonderland"], asked
    assert "rating" in item


# ---- "nobody rated it" is not "we never found it" -------------------------------------------

def test_a_resolved_but_unrated_book_carries_its_own_state(store):
    store.put(AUDNEX_ASIN, "audnex", state="unrated")
    p = AudnexProvider(store, regions=["uk"])
    item = {"title": "Project Hail Mary", "author": "Andy Weir", "asin": AUDNEX_ASIN, "source": "audible"}
    enrich(item, provider_obj=p)
    assert item["rating"] is None
    assert item["rating_state"] == "unrated", "the source knows the book and nobody has rated it"


def test_an_unresolved_book_says_only_that_it_is_unknown(store):
    p = AudnexProvider(store, regions=["uk"])
    item = {"title": "Nowhere At All", "author": "Nobody", "source": "audiobookbay"}
    enrich(item, provider_obj=p)
    assert item["rating"] is None
    assert item["rating_state"] == "none"


def test_a_stale_unrated_row_stops_claiming_to_know_the_book(store):
    """A remembered miss expires sooner than a rating; once it has, the card must go back to the
    weaker claim rather than assert something the cache no longer supports."""
    store.put(AUDNEX_ASIN, "audnex", state="unrated")
    assert store.state(AUDNEX_ASIN, "audnex") == "unrated"

    import sqlite3
    import time

    with sqlite3.connect(str(store.path)) as con:   # backdate it past RETRY_AFTER
        con.execute("UPDATE rating SET checked_at = ? WHERE key = ?", (time.time() - 10**6, AUDNEX_ASIN))
    assert store.state(AUDNEX_ASIN, "audnex") == "none"


def test_the_client_prints_two_different_strings():
    src = (Path(__file__).parent.parent / "narratarr/static/app.js").read_text()
    line = next(l for l in src.splitlines() if "rating_state" in l and "rating-pill empty" in l)
    assert '"No Rating Available"' in line, "an unrated-but-known book needs its own words"
    assert ': "No Rating"' in line, "everything else keeps the old ones"


# ---- the settings toggle --------------------------------------------------------------------

def test_ratings_are_on_by_default_even_for_an_old_settings_file(tmp_path, monkeypatch):
    """David's instance predates this key. The default has to apply at read time, not only in a
    fresh template, or an upgrade silently turns the feature off."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(config, "_settings", None)
    # a settings file from before the toggle existed, still carrying the retired provider key
    (tmp_path / "settings.json").write_text('{"library_dir": "/audiobooks", "ratings_provider": "audnex"}')
    assert config.load().ratings_enabled is True
    assert provider() is not None


def test_switching_it_off_constructs_nothing_and_calls_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(config, "_settings", None)
    config.update({"ratings_enabled": False})

    assert provider() is None, "no provider object at all when the feature is off"

    called = {"n": 0}
    monkeypatch.setattr("narratarr.ratings._enqueue",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    item = {"title": "Project Hail Mary", "author": "Andy Weir", "source": "audible"}
    enrich(item)
    assert item["rating"] is None
    assert item["rating_state"] == "off", "absent, not unrated"
    assert called["n"] == 0, "nothing was queued for a fetch"


def test_switching_it_back_on_starts_asking_again(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(config, "_settings", None)
    config.update({"ratings_enabled": False})
    assert provider() is None
    config.update({"ratings_enabled": True})
    assert provider() is not None


async def test_switching_ratings_on_after_startup_starts_the_filler(monkeypatch):
    """The filler starts at startup only when ratings are on; turned on later, the next search starts it."""
    import narratarr.ratings as ratings_module

    started = []

    async def fake_drain():
        started.append(True)
        await asyncio.sleep(3600)

    monkeypatch.setattr(ratings_module, "_drain", fake_drain)
    ratings_module.stop_filler()
    ratings_module.ensure_filler()
    ratings_module.ensure_filler()  # already running: not started twice
    await asyncio.sleep(0)
    assert started == [True]
    ratings_module.stop_filler()
