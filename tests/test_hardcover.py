"""Hardcover: the Want to Read shelf comes to Wanted (from the Discord, NOAH).

Hardcover is never called: every answer is an httpx.MockTransport handler shaped like their docs
(docs.hardcover.app: Getting Started, OAuth for native/local clients, User Books, Editions).
"""
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from narratarr import api, config, db, hardcover, wanted

CLIENT = "test-public-client-id"


@pytest.fixture(autouse=True)
def registered(monkeypatch):
    monkeypatch.setenv("NARRATARR_HARDCOVER_CLIENT_ID", CLIENT)
    monkeypatch.setattr(hardcover, "PAGE_PAUSE", 0)
    hardcover._pending.clear()
    hardcover.last_sync.clear()


def form(request: httpx.Request) -> dict:
    return dict(httpx.QueryParams(request.content.decode()))


def connected(expires_in=3600):
    config.update({"hardcover_access_token": "hc_at_old", "hardcover_refresh_token": "hc_rt_old",
                   "hardcover_token_expires": time.time() + expires_in, "hardcover_user": "reader"})


def gql(handler_data):
    """A GraphQL endpoint answering `me` and the shelf query from handler_data(variables)."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == hardcover.GRAPHQL
        assert request.headers["authorization"].startswith("Bearer hc_at_")
        assert "Narratarr/" in request.headers["user-agent"]
        body = json.loads(request.content)
        if "me {" in body["query"]:
            return httpx.Response(200, json={"data": {"me": [{"id": 42, "username": "reader"}]}})
        return httpx.Response(200, json={"data": handler_data(body["variables"])})
    return handler


# ---- connecting ----------------------------------------------------------------------------------


async def test_the_sign_in_code_is_shown_and_the_device_code_stays_on_the_server():
    def handler(request):
        assert request.url == hardcover.DEVICE
        assert form(request) == {"client_id": CLIENT, "scope": "read:me:content read:library"}
        return httpx.Response(200, json={"device_code": "SECRET-DEVICE", "user_code": "ABCD-1234",
                                         "verification_uri": "https://hardcover.app/link",
                                         "verification_uri_complete": "https://hardcover.app/link?code=ABCD-1234",
                                         "expires_in": 600, "interval": 5})

    shown = await hardcover.start(httpx.MockTransport(handler))
    assert shown["user_code"] == "ABCD-1234" and shown["verification_uri_complete"].endswith("ABCD-1234")
    assert "SECRET-DEVICE" not in json.dumps(shown)


async def test_a_sign_in_page_anywhere_but_hardcover_is_refused():
    def handler(request):
        return httpx.Response(200, json={"device_code": "d", "user_code": "u", "verification_uri": "https://evil.example/"})

    with pytest.raises(hardcover.HardcoverError):
        await hardcover.start(httpx.MockTransport(handler))


async def test_without_an_app_id_it_says_so(monkeypatch):
    monkeypatch.delenv("NARRATARR_HARDCOVER_CLIENT_ID")
    monkeypatch.setattr(hardcover, "BUILT_IN_CLIENT_ID", "")
    assert not hardcover.available()
    with pytest.raises(hardcover.HardcoverError):
        await hardcover.start()


async def test_waiting_then_approved_keeps_the_tokens_on_the_server():
    answers = iter([{"error": "authorization_pending"},
                    {"access_token": "hc_at_new", "refresh_token": "hc_rt_new", "expires_in": 3600}])

    def handler(request):
        if request.url == hardcover.TOKEN:
            assert form(request)["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
            assert form(request)["device_code"] == "dev"
            return httpx.Response(200 if "pending" not in json.dumps(a := next(answers)) else 400, json=a)
        return gql(lambda v: {})(request)

    t = httpx.MockTransport(handler)
    hardcover._pending.update(device_code="dev", interval=0, next_poll=0.0, expires_at=time.time() + 600)
    assert (await hardcover.poll(t))["state"] == "pending"
    done = await hardcover.poll(t)
    assert done == {"state": "connected", "user": "reader"}

    s = config.load()
    assert s.hardcover_refresh_token == "hc_rt_new" and s.hardcover_user == "reader"
    public = json.dumps(s.public())
    assert "hc_rt_new" not in public and "hc_at_new" not in public and s.public()["hardcover_connected"]


async def test_polling_faster_than_hardcover_asks_doesnt_call_it():
    def handler(request):
        raise AssertionError("polled Hardcover too soon")

    hardcover._pending.update(device_code="dev", interval=5, next_poll=time.time() + 5, expires_at=time.time() + 600)
    assert (await hardcover.poll(httpx.MockTransport(handler)))["state"] == "pending"


@pytest.mark.parametrize("error, state", [("access_denied", "denied"), ("expired_token", "expired")])
async def test_a_refused_or_expired_code_ends_the_sign_in(error, state):
    hardcover._pending.update(device_code="dev", interval=0, next_poll=0.0, expires_at=time.time() + 600)
    t = httpx.MockTransport(lambda r: httpx.Response(400, json={"error": error}))
    assert (await hardcover.poll(t))["state"] == state
    assert not hardcover._pending and not config.load().hardcover_refresh_token


# ---- asking -------------------------------------------------------------------------------------


async def test_an_expired_access_token_is_renewed_with_the_refresh_token():
    connected(expires_in=-10)
    seen = []

    def handler(request):
        if request.url == hardcover.TOKEN:
            seen.append(form(request))
            return httpx.Response(200, json={"access_token": "hc_at_fresh", "refresh_token": "hc_rt_fresh", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer hc_at_fresh"
        return httpx.Response(200, json={"data": {"me": {"id": 1, "username": "r"}}})

    await hardcover.query("query Me { me { id username } }", transport=httpx.MockTransport(handler))
    assert seen == [{"grant_type": "refresh_token", "refresh_token": "hc_rt_old", "client_id": CLIENT}]
    assert config.load().hardcover_refresh_token == "hc_rt_fresh"


async def test_a_revoked_sign_in_is_forgotten_and_says_so():
    connected(expires_in=-10)
    t = httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(hardcover.HardcoverError, match="Connect it again"):
        await hardcover.query("query Me { me { id } }", transport=t)
    assert not config.load().hardcover_refresh_token


async def test_values_go_in_variables_never_in_the_query():
    connected()
    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        if "me {" in body["query"]:
            return httpx.Response(200, json={"data": {"me": {"id": 42, "username": "reader"}}})
        return httpx.Response(200, json={"data": {"user_books": []}})

    await hardcover.want_to_read(httpx.MockTransport(handler))
    shelf = sent[1]
    assert shelf["variables"] == {"user": 42, "status": 1, "limit": 100, "offset": 0}
    assert "42" not in shelf["query"]


async def test_the_shelf_is_read_in_pages_and_only_audio_editions_give_an_asin():
    connected()
    rows = [{"id": i, "book": {"title": f"Book {i}", "contributions": [{"author": {"name": "Ann Author"}}]},
             "edition": {"asin": "B00AUDIO01", "reading_format_id": 2 if i % 2 else 1}} for i in range(150)]

    def page(v):
        return {"user_books": rows[v["offset"]:v["offset"] + v["limit"]]}

    got, more = await hardcover.want_to_read(httpx.MockTransport(gql(page)))
    assert len(got) == 150 and not more and got[0] == {"id": 0, "title": "Book 0", "author": "Ann Author", "asin": ""}
    assert got[1]["asin"] == "B00AUDIO01"


async def test_reading_stops_once_there_are_enough_new_books():
    connected()
    rows = [{"id": i, "book": {"title": f"B{i}", "contributions": []}, "edition": None} for i in range(1000)]
    pages = []

    def page(v):
        pages.append(v["offset"])
        return {"user_books": rows[v["offset"]:v["offset"] + v["limit"]]}

    got, more = await hardcover.want_to_read(httpx.MockTransport(gql(page)), skip=set(range(150)), enough=25)
    assert [b["id"] for b in got] == list(range(150, 175)) and more
    assert pages == [0, 100], "two pages, not ten"


async def test_a_busy_hardcover_is_an_error_not_a_crash():
    connected()
    t = httpx.MockTransport(lambda r: httpx.Response(429, headers={"retry-after": "90"}, json={"error": "Too Many Requests"}))
    with pytest.raises(hardcover.HardcoverError, match="90 seconds"):
        await hardcover.query("query Me { me { id } }", transport=t)


async def test_a_short_retry_after_is_waited_out_once(monkeypatch):
    connected()
    waits, answers = [], iter([httpx.Response(429, headers={"retry-after": "7"}, json={}),
                               httpx.Response(200, json={"data": {"me": {"id": 1}}})])

    async def no_wait(seconds):
        waits.append(seconds)

    monkeypatch.setattr(hardcover.asyncio, "sleep", no_wait)
    data = await hardcover.query("query Me { me { id } }", transport=httpx.MockTransport(lambda r: next(answers)))
    assert data == {"me": {"id": 1}} and waits == [7]


# ---- the sync -------------------------------------------------------------------------------------


DUNE = {"asin": "B002V1OF70", "url": "https://www.audible.com/pd/B002V1OF70", "title": "Dune", "author": "Frank Herbert",
        "narrators": ["Scott Brick"], "series": [], "released": "2007-01-01"}


@pytest.fixture
def shelf(monkeypatch):
    items = []

    async def want_to_read(transport=None, skip=frozenset(), enough=None):
        new = [i for i in items if i["id"] not in skip]
        return (new[:enough], len(new) > enough) if enough is not None else (new, False)

    async def audible_match(book):
        if book.get("asin") == "B0KINDLE00":
            return {**DUNE, "title": "Some Other Book", "author": "Someone Else"}  # a wrong ASIN on Hardcover
        return DUNE if book["title"] == "Dune" else None

    monkeypatch.setattr(hardcover, "want_to_read", want_to_read)
    monkeypatch.setattr(wanted, "audible_match", audible_match)
    return items


async def test_shelf_books_come_to_wanted_once(shelf):
    shelf += [{"id": 1, "title": "Dune", "author": "Frank Herbert", "asin": ""},
              {"id": 2, "title": "Obscure Zine", "author": "Nobody", "asin": ""}]
    r = await wanted.sync_hardcover()
    assert (r["added"], r["not_found"]) == (1, 1)
    book = db.get(r["added_ids"][0])
    assert book["status"] == "wanted" and book["kind"] == "audiobook" and book["asin"] == "B002V1OF70"

    db.delete(book["id"])  # taken off Wanted
    again = await wanted.sync_hardcover()
    assert again["added"] == 0 and not again["added_ids"], "a book taken off Wanted stays off"
    assert {r["outcome"] for r in db.hardcover_recent()} == {"added", "not_found"}


async def test_both_formats_when_asked(shelf):
    config.update({"hardcover_format": "both"})
    shelf.append({"id": 1, "title": "Dune", "author": "Frank Herbert", "asin": ""})
    r = await wanted.sync_hardcover()
    assert sorted(db.get(i)["kind"] for i in r["added_ids"]) == ["audiobook", "ebook"]


async def test_a_big_shelf_is_matched_a_few_at_a_time(shelf):
    shelf += [{"id": i, "title": "Nope", "author": "X", "asin": ""} for i in range(40)]
    first = await wanted.sync_hardcover()
    assert first["not_found"] == 25 and first["more"]
    second = await wanted.sync_hardcover()
    assert second["not_found"] == 15 and not second["more"]


async def test_a_book_already_in_the_library_isnt_added_twice(shelf):
    db.insert({"source": "local", "source_url": "local:dune", "title": "Dune", "author": "Frank Herbert",
               "status": "completed", "kind": "audiobook"})
    shelf.append({"id": 1, "title": "Dune", "author": "Frank Herbert", "asin": ""})
    r = await wanted.sync_hardcover()
    assert r["in_library"] == 1 and r["added"] == 0


# ---- the API ---------------------------------------------------------------------------------------


@pytest.fixture
def client():
    with TestClient(api.app) as c:
        c.app.state.worker.poke = lambda: None
        yield c


def test_the_settings_form_cant_read_or_set_the_tokens(client):
    connected()
    body = client.get("/api/settings").text
    assert "hc_rt_old" not in body and "hc_at_old" not in body and "hardcover_refresh_token" not in body
    client.put("/api/settings", json={"hardcover_refresh_token": "attacker", "hardcover_access_token": "x",
                                      "hardcover_format": "ebook"})
    s = config.load()
    assert s.hardcover_refresh_token == "hc_rt_old" and s.hardcover_format == "ebook"


def test_status_lists_what_happened_without_secrets(client):
    connected()
    db.hardcover_record(7, "Dune", "Frank Herbert", "added", None)
    r = client.get("/api/hardcover").json()
    assert r["connected"] and r["user"] == "reader" and r["recent"][0]["title"] == "Dune"
    assert "hc_" not in json.dumps(r)


def test_disconnect_revokes_at_hardcover_and_keeps_the_handled_list(client, monkeypatch):
    connected()
    db.hardcover_record(7, "Dune", "Frank Herbert", "added", None)
    revoked = []
    real = hardcover._client

    def fake_client(transport=None):
        def handler(request):
            revoked.append((str(request.url), form(request)))
            return httpx.Response(200, json={})
        return real(httpx.MockTransport(handler))

    monkeypatch.setattr(hardcover, "_client", fake_client)
    assert client.post("/api/hardcover/disconnect").status_code == 200
    assert revoked == [(hardcover.REVOKE, {"token": "hc_rt_old", "client_id": CLIENT, "token_type_hint": "refresh_token"})]
    s = config.load()
    assert not s.hardcover_refresh_token and not s.hardcover_access_token and not s.hardcover_user
    assert db.hardcover_seen_ids() == {7}, "reconnecting won't re-add books already handled"


def test_a_listener_cant_touch_hardcover(monkeypatch):
    from narratarr import auth
    monkeypatch.setattr(auth, "current_user", lambda request: {"id": 2, "username": "l", "role": "listener", "created_at": 0})
    with TestClient(api.app) as c:
        for method, path in (("get", "/api/hardcover"), ("post", "/api/hardcover/connect"),
                             ("post", "/api/hardcover/sync"), ("post", "/api/hardcover/disconnect")):
            assert getattr(c, method)(path).status_code == 403


async def test_an_asin_for_a_different_book_falls_back_to_title_and_author(shelf):
    shelf.append({"id": 1, "title": "Dune", "author": "Frank Herbert", "asin": "B0KINDLE00"})
    r = await wanted.sync_hardcover()
    assert r["added"] == 1 and db.get(r["added_ids"][0])["title"] == "Dune"


async def test_one_check_at_a_time(shelf):
    async with hardcover._lock("sync"):
        with pytest.raises(hardcover.Busy):
            await wanted.sync_hardcover()


# ---- review fixes: rotation, Disconnect, transient errors ---------------------------------------------


async def test_two_renewals_at_once_use_the_refresh_token_once():
    import asyncio
    connected(expires_in=-10)
    renewals = []

    def handler(request):
        if request.url == hardcover.TOKEN:
            sent = form(request)["refresh_token"]
            renewals.append(sent)
            if sent != "hc_rt_old":
                return httpx.Response(400, json={"error": "invalid_grant"})  # rotation: the old one is spent
            return httpx.Response(200, json={"access_token": "hc_at_1", "refresh_token": "hc_rt_1", "expires_in": 3600})
        return httpx.Response(200, json={"data": {"me": {"id": 1, "username": "r"}}})

    t = httpx.MockTransport(handler)
    await asyncio.gather(*(hardcover.query("query Me { me { id } }", transport=t) for _ in range(3)))
    assert renewals == ["hc_rt_old"], "one renewal; the others used the token it stored"
    assert config.load().hardcover_refresh_token == "hc_rt_1"


async def test_a_refused_renewal_of_an_old_token_doesnt_sign_out_a_newer_one():
    connected(expires_in=-10)

    def handler(request):
        config.update({"hardcover_refresh_token": "hc_rt_newer"})  # another renewal stored a newer one meanwhile
        return httpx.Response(400, json={"error": "invalid_grant"})

    with pytest.raises(hardcover.HardcoverError):
        await hardcover.query("query Me { me { id } }", transport=httpx.MockTransport(handler))
    assert config.load().hardcover_refresh_token == "hc_rt_newer"


async def test_disconnect_wins_over_a_renewal_under_way():
    connected(expires_in=-10)

    def handler(request):
        if request.url == hardcover.TOKEN:
            hardcover._forget_tokens()  # Disconnect pressed while the renewal was out
            return httpx.Response(200, json={"access_token": "hc_at_late", "refresh_token": "hc_rt_late", "expires_in": 3600})
        raise AssertionError("asked Hardcover after Disconnect")

    with pytest.raises(hardcover.HardcoverError, match="disconnected"):
        await hardcover.query("query Me { me { id } }", transport=httpx.MockTransport(handler))
    assert not config.load().hardcover_refresh_token


async def test_an_expired_token_reported_as_a_graphql_error_is_renewed():
    connected()
    calls = []

    def handler(request):
        if request.url == hardcover.TOKEN:
            return httpx.Response(200, json={"access_token": "hc_at_fresh", "refresh_token": "hc_rt_fresh", "expires_in": 3600})
        calls.append(request.headers["authorization"])
        if len(calls) == 1:
            return httpx.Response(200, json={"errors": [{"message": "JWTExpired", "extensions": {"code": "invalid-jwt"}}]})
        return httpx.Response(200, json={"data": {"me": {"id": 1, "username": "r"}}})

    await hardcover.query("query Me { me { id } }", transport=httpx.MockTransport(handler))
    assert calls == ["Bearer hc_at_old", "Bearer hc_at_fresh"]


@pytest.mark.parametrize("status, body", [(503, {}), (500, {"oops": 1}), (429, {"error": "Too Many Requests"})])
async def test_hardcover_having_a_bad_moment_keeps_the_sign_in_code(status, body):
    hardcover._pending.update(device_code="dev", interval=5, next_poll=0.0, expires_at=time.time() + 600)
    t = httpx.MockTransport(lambda r: httpx.Response(status, json=body))
    assert (await hardcover.poll(t))["state"] == "pending"
    assert hardcover._pending and hardcover._pending["interval"] == 10


async def test_odd_numbers_from_hardcover_dont_crash_the_sign_in():
    def handler(request):
        return httpx.Response(200, json={"device_code": "d", "user_code": "u", "verification_uri": "https://hardcover.app/link",
                                         "expires_in": "soon", "interval": None})

    shown = await hardcover.start(httpx.MockTransport(handler))
    assert shown["expires_in"] == 600 and shown["interval"] == 5


def test_check_now_says_when_a_check_is_already_running(client, monkeypatch):
    async def busy(transport=None, most=25):
        raise hardcover.Busy("Narratarr is already checking your Hardcover shelf.")

    monkeypatch.setattr(wanted, "sync_hardcover", busy)
    assert client.post("/api/hardcover/sync").status_code == 409
