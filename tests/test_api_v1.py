"""API v1: app sign-in with rotating tokens, and requesting books on someone's behalf."""

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from narratarr import api, apiv1, audible, auth, db, tokens, users, wanted

pytestmark = pytest.mark.real_auth

HOME = ("192.168.1.20", 50000)
AWAY = ("8.8.4.4", 50000)

DUNE = {
    "asin": "B002V1OF70", "url": "https://www.audible.com/pd/B002V1OF70", "title": "Dune", "subtitle": "",
    "author": "Frank Herbert", "author_asins": [], "narrators": ["Scott Brick", "Orlagh Cassidy"],
    "cover_url": "https://m.media-amazon.com/images/I/dune._SL500_.jpg", "description": "Arrakis.",
    "released": "2007-01-01", "length": "21 hours", "minutes": 1260, "language": "English",
    "series": [{"asin": "B00GMG4N8E", "title": "Dune", "sequence": "1"}], "kind": "SinglePartBook", "abridged": False,
}
HAIL_MARY = {**DUNE, "asin": "B08G9PRS1K", "url": "https://www.audible.com/pd/B08G9PRS1K", "title": "Project Hail Mary",
             "author": "Andy Weir", "narrators": ["Ray Porter"], "series": [], "minutes": 970}
CATALOG = {b["asin"]: b for b in (DUNE, HAIL_MARY)}


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    auth._failures.clear()
    auth._known.clear()
    monkeypatch.setattr(users, "_N", 2**10)  # fast hashing in tests
    for limiter in (apiv1.search_limit, apiv1.request_limit, apiv1.refresh_failures):
        limiter._hits.clear()

    async def book(asin, client=None):
        return CATALOG.get(asin)

    async def search_books(q, page=1, client=None):
        return [b for b in CATALOG.values() if q.lower() in (b["title"] + b["author"]).lower()], 1

    async def no_search(book):
        return None

    monkeypatch.setattr(audible, "book", book)
    monkeypatch.setattr(audible, "search_books", search_books)
    monkeypatch.setattr(wanted, "search_and_grab", no_search)


def client(where=HOME):
    c = TestClient(api.app, client=where)
    c.__enter__()
    c.app.state.worker.poke = lambda: None
    return c


@pytest.fixture
def app():
    c = client(AWAY)
    yield c
    c.__exit__(None, None, None)


@pytest.fixture
def people():
    return users.create("desmond", "correct horse", "admin"), users.create("kid", "listen-only", "listener")


def login(c, username="kid", password="listen-only", app_name="Fable Frog on iPhone"):
    r = c.post("/api/v1/auth/login", json={"username": username, "password": password, "client": app_name})
    assert r.status_code == 200, r.text
    return r.json()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def err(r):
    return r.json()["error"]["code"]


# ---- signing in -----------------------------------------------------------------------------


def test_status_needs_no_sign_in(app):
    r = app.get("/api/v1/status")
    assert r.status_code == 200
    assert r.json()["app"] == "narratarr" and r.json()["api_version"] == 1
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"


def test_login_gives_tokens_and_stores_only_their_hashes(app, people):
    t = login(app)
    assert t["access_token"].startswith("nrt_at_") and t["refresh_token"].startswith("nrt_rt_")
    assert t["token_type"] == "Bearer" and t["expires_in"] == 3600 and t["refresh_expires_in"] == 90 * 24 * 3600
    assert t["user"]["username"] == "kid" and "password_hash" not in t["user"]
    with db.connect() as conn:
        stored = str([tuple(r) for r in conn.execute("SELECT * FROM app_tokens")])
    assert t["access_token"] not in stored and t["refresh_token"] not in stored
    me = app.get("/api/v1/me", headers=bearer(t["access_token"])).json()
    assert me["user"]["username"] == "kid" and me["app"]["name"] == "Fable Frog on iPhone"


def test_wrong_password_and_too_many_tries(app, people):
    r = app.post("/api/v1/auth/login", json={"username": "kid", "password": "nope", "client": "x"})
    assert r.status_code == 401 and err(r) == "invalid_credentials"
    for _ in range(auth.LOGIN_TRIES):
        app.post("/api/v1/auth/login", json={"username": "kid", "password": "nope", "client": "x"})
    r = app.post("/api/v1/auth/login", json={"username": "kid", "password": "listen-only", "client": "x"})
    assert r.status_code == 429 and err(r) == "rate_limited" and r.headers["retry-after"]


def test_login_before_setup(app):
    r = app.post("/api/v1/auth/login", json={"username": "a", "password": "b", "client": "c"})
    assert r.status_code == 409 and err(r) == "setup_required"


def test_validation_errors_never_echo_the_password(app, people):
    r = app.post("/api/v1/auth/login", json={"username": "kid", "password": "hunter2-secret", "client": "", "extra": 1})
    assert r.status_code == 422 and err(r) == "validation_error"
    assert "hunter2-secret" not in r.text
    assert {f["field"] for f in r.json()["error"]["fields"]} >= {"client", "extra"}


def test_app_names_are_tidied(app, people):
    t = login(app, app_name="  Fable\x00 Frog\n\n on   iPhone  ")
    assert app.get("/api/v1/me", headers=bearer(t["access_token"])).json()["app"]["name"] == "Fable Frog on iPhone"


# ---- what tokens can and can't do --------------------------------------------------------------


def test_every_route_needs_sign_in_except_the_open_ones(app):
    schema = app.get("/api/v1/openapi.json").json()
    routes = [(path, method) for path, ops in schema["paths"].items() for method in ops if path not in apiv1.OPEN_PATHS]
    assert len(routes) >= 8
    for path, method in routes:
        url = "/api/v1" + path.replace("{asin}", "B002V1OF70").replace("{request_id}", "1")
        resp = app.request(method.upper(), url)
        assert resp.status_code == 401, f"{method} {path} answered {resp.status_code} without sign-in"
        assert resp.headers["www-authenticate"].startswith("Bearer")


def test_app_tokens_dont_work_on_the_internal_api(app, people):
    t = login(app, "desmond", "correct horse")  # even an admin's
    for method, path in (("GET", "/api/library"), ("GET", "/api/settings"), ("GET", "/api/users"),
                         ("PUT", "/api/settings")):
        assert app.request(method, path, headers=bearer(t["access_token"])).status_code == 401


def test_the_home_network_shortcut_doesnt_apply_to_apps(people):
    from narratarr import config

    config.update({"auth_local_bypass": True})
    c = client(HOME)
    try:
        assert c.get("/api/library").status_code == 200  # the web UI skips sign-in at home…
        assert c.get("/api/v1/me").status_code == 401  # …apps still sign in
    finally:
        c.__exit__(None, None, None)


def test_a_web_session_works_too(app, people):
    app.post("/api/auth/login", json={"username": "kid", "password": "listen-only"})
    me = app.get("/api/v1/me").json()
    assert me["user"]["username"] == "kid" and me["app"] is None


def test_bad_tokens(app, people):
    assert err(app.get("/api/v1/me", headers={"Authorization": "Basic a2lkOmxpc3Rlbg=="})) == "invalid_token"
    assert err(app.get("/api/v1/me", headers={"Authorization": "Bearer "})) == "invalid_token"
    assert err(app.get("/api/v1/me", headers=bearer("nrt_at_" + "x" * 43))) == "invalid_token"
    t = login(app)
    assert err(app.get("/api/v1/me", headers=bearer(t["refresh_token"]))) == "invalid_token"  # wrong kind


def test_access_tokens_expire(app, people, monkeypatch):
    t = login(app)
    later = time.time() + tokens.ACCESS_SECONDS + 5
    monkeypatch.setattr(tokens.time, "time", lambda: later)
    r = app.get("/api/v1/me", headers=bearer(t["access_token"]))
    assert r.status_code == 401 and err(r) == "token_expired"
    assert 'error="token_expired"' in r.headers["www-authenticate"]


# ---- refreshing -------------------------------------------------------------------------------------


def test_refresh_rotates(app, people):
    t1 = login(app)
    t2 = app.post("/api/v1/auth/refresh", json={"refresh_token": t1["refresh_token"]}).json()
    assert t2["refresh_token"] != t1["refresh_token"] and t2["access_token"] != t1["access_token"]
    assert app.get("/api/v1/me", headers=bearer(t2["access_token"])).status_code == 200
    t3 = app.post("/api/v1/auth/refresh", json={"refresh_token": t2["refresh_token"]}).json()
    assert app.get("/api/v1/me", headers=bearer(t3["access_token"])).status_code == 200


def test_a_reused_refresh_token_signs_the_app_out(app, people, monkeypatch):
    t1 = login(app)
    t2 = app.post("/api/v1/auth/refresh", json={"refresh_token": t1["refresh_token"]}).json()
    later = time.time() + tokens.RETRY_SECONDS + 5
    monkeypatch.setattr(tokens.time, "time", lambda: later)
    r = app.post("/api/v1/auth/refresh", json={"refresh_token": t1["refresh_token"]})  # a stolen copy
    assert r.status_code == 401 and err(r) == "invalid_token"
    # Everything from that sign-in is dead, including the real app's newer tokens.
    assert app.get("/api/v1/me", headers=bearer(t2["access_token"])).status_code == 401
    assert app.post("/api/v1/auth/refresh", json={"refresh_token": t2["refresh_token"]}).status_code == 401
    assert tokens.grants(users.by_name("kid")["id"]) == []


def test_a_quick_retry_is_forgiven(app, people):
    t1 = login(app)
    lost = app.post("/api/v1/auth/refresh", json={"refresh_token": t1["refresh_token"]}).json()  # reply never arrived
    retry = app.post("/api/v1/auth/refresh", json={"refresh_token": t1["refresh_token"]})
    assert retry.status_code == 200
    assert app.get("/api/v1/me", headers=bearer(retry.json()["access_token"])).status_code == 200
    assert app.get("/api/v1/me", headers=bearer(lost["access_token"])).status_code == 401  # the lost pair is void
    assert app.post("/api/v1/auth/refresh", json={"refresh_token": lost["refresh_token"]}).status_code == 401


def test_no_retry_once_the_new_pair_has_been_used(app, people):
    t1 = login(app)
    t2 = app.post("/api/v1/auth/refresh", json={"refresh_token": t1["refresh_token"]}).json()
    app.post("/api/v1/auth/refresh", json={"refresh_token": t2["refresh_token"]})  # the app clearly got its reply
    assert app.post("/api/v1/auth/refresh", json={"refresh_token": t1["refresh_token"]}).status_code == 401
    assert tokens.grants(users.by_name("kid")["id"]) == []


def test_unused_apps_lapse(app, people, monkeypatch):
    t = login(app)
    later = time.time() + tokens.REFRESH_SECONDS + 5
    monkeypatch.setattr(tokens.time, "time", lambda: later)
    assert app.post("/api/v1/auth/refresh", json={"refresh_token": t["refresh_token"]}).status_code == 401


def test_junk_refreshes_cant_lock_anyone_out_of_signing_in(app, people, monkeypatch):
    monkeypatch.setattr(apiv1, "refresh_failures", apiv1.Limiter(5, 600))
    for _ in range(5):
        assert app.post("/api/v1/auth/refresh", json={"refresh_token": "nrt_rt_" + "y" * 43}).status_code == 401
    r = app.post("/api/v1/auth/refresh", json={"refresh_token": "nrt_rt_" + "y" * 43})
    assert r.status_code == 429 and err(r) == "rate_limited"  # floods stop…
    assert app.post("/api/v1/auth/login", json={"username": "kid", "password": "listen-only", "client": "x"}).status_code == 200


def test_a_thief_replaying_inside_the_retry_window_gets_everyone_signed_out(app, people):
    t0 = login(app)
    t1 = app.post("/api/v1/auth/refresh", json={"refresh_token": t0["refresh_token"]}).json()  # the app's trade
    stolen = app.post("/api/v1/auth/refresh", json={"refresh_token": t0["refresh_token"]})  # a copy, seconds later
    assert stolen.status_code == 200  # indistinguishable from a lost reply, so far…
    thief = stolen.json()
    assert app.get("/api/v1/me", headers=bearer(t1["access_token"])).status_code == 401  # the app's pair is superseded
    r = app.post("/api/v1/auth/refresh", json={"refresh_token": t1["refresh_token"]})  # …until the app refreshes
    assert r.status_code == 401 and err(r) == "invalid_token"
    assert app.get("/api/v1/me", headers=bearer(thief["access_token"])).status_code == 401  # the thief is out too
    assert app.post("/api/v1/auth/refresh", json={"refresh_token": thief["refresh_token"]}).status_code == 401
    assert tokens.grants(users.by_name("kid")["id"]) == []


def test_no_retry_once_the_new_access_token_has_been_used(app, people):
    t0 = login(app)
    t1 = app.post("/api/v1/auth/refresh", json={"refresh_token": t0["refresh_token"]}).json()
    assert app.get("/api/v1/me", headers=bearer(t1["access_token"])).status_code == 200  # the app got its reply
    assert app.post("/api/v1/auth/refresh", json={"refresh_token": t0["refresh_token"]}).status_code == 401
    assert tokens.grants(users.by_name("kid")["id"]) == []


def test_refreshing_is_limited_per_app_without_losing_the_token(app, people, monkeypatch):
    monkeypatch.setattr(tokens, "REFRESHES_PER_WINDOW", 3)
    t = login(app)
    for _ in range(2):
        t = app.post("/api/v1/auth/refresh", json={"refresh_token": t["refresh_token"]}).json()
    r = app.post("/api/v1/auth/refresh", json={"refresh_token": t["refresh_token"]})
    assert r.status_code == 429 and err(r) == "rate_limited"
    assert app.get("/api/v1/me", headers=bearer(t["access_token"])).status_code == 200  # still signed in
    monkeypatch.setattr(tokens, "REFRESHES_PER_WINDOW", 30)
    assert app.post("/api/v1/auth/refresh", json={"refresh_token": t["refresh_token"]}).status_code == 200


def test_the_token_table_stays_small(app, people, monkeypatch):
    monkeypatch.setattr(tokens, "REFRESHES_PER_WINDOW", 1000)
    t = login(app)
    for _ in range(60):
        t = app.post("/api/v1/auth/refresh", json={"refresh_token": t["refresh_token"]}).json()
    with db.connect() as conn:
        spent = conn.execute("SELECT COUNT(*) FROM app_tokens WHERE kind='refresh' AND used_at IS NOT NULL").fetchone()[0]
    assert spent <= tokens.KEEP_SPENT


def test_a_burst_of_guesses_cant_slip_past_the_limit(app, people):
    results = []

    def guess():
        r = app.post("/api/v1/auth/login", json={"username": "kid", "password": "wrong-guess", "client": "x"})
        results.append(r.status_code)

    threads = [threading.Thread(target=guess) for _ in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(401) <= auth.LOGIN_TRIES and results.count(429) >= 24 - auth.LOGIN_TRIES


def test_guesses_from_many_addresses_still_run_out(people):
    hits = []
    for n in range(auth.NAME_TRIES + 1):
        c = client((f"8.8.{n // 200}.{n % 200 + 1}", 50000))
        try:
            hits.append(c.post("/api/v1/auth/login", json={"username": "KID", "password": "no", "client": "x"}).status_code)
        finally:
            c.__exit__(None, None, None)
    assert hits[-1] == 429 and hits.count(401) == auth.NAME_TRIES


def test_knowing_a_username_isnt_enough_to_lock_its_owner_out(people):
    familiar = client(("9.9.9.9", 50000))
    try:
        assert familiar.post("/api/auth/login", json={"username": "kid", "password": "listen-only"}).status_code == 200
        for n in range(auth.NAME_TRIES):  # someone hammers the account from all over
            c = client((f"7.7.{n}.1", 50000))
            try:
                c.post("/api/v1/auth/login", json={"username": "kid", "password": "guess", "client": "x"})
            finally:
                c.__exit__(None, None, None)
        stranger = client(("7.7.200.1", 50000))
        try:
            assert stranger.post("/api/v1/auth/login", json={"username": "kid", "password": "listen-only",
                                                              "client": "x"}).status_code == 429  # strangers stop…
        finally:
            stranger.__exit__(None, None, None)
        assert familiar.post("/api/auth/login", json={"username": "kid", "password": "listen-only"}).status_code == 200
        home = client(HOME)  # …but a network that signed in before, or the home network, still gets in
        try:
            assert home.post("/api/auth/login", json={"username": "kid", "password": "listen-only"}).status_code == 200
        finally:
            home.__exit__(None, None, None)
    finally:
        familiar.__exit__(None, None, None)


def test_made_up_usernames_dont_get_counters(app):
    for n in range(20):
        app.post("/api/v1/auth/login", json={"username": f"nobody{n}", "password": "x", "client": "x"})
        auth._failures.pop(f"ip:{AWAY[0]}", None)
    assert not [k for k in auth._failures if k.startswith("user:")]


def test_a_flood_of_addresses_cant_reset_an_account_counter(people, monkeypatch):
    monkeypatch.setattr(auth, "_MAX_KEYS", 50)
    now = time.time()
    auth._failures["user:kid"] = [now] * auth.NAME_TRIES
    for n in range(200):
        auth._failures[f"ip:flood-{n}"] = [now]
    auth._trim(now)
    assert len(auth._failures["user:kid"]) == auth.NAME_TRIES


def test_ipv6_addresses_count_by_network(people):
    for n in range(auth.LOGIN_TRIES):
        c = client((f"2001:db8:1:2::{n + 1:x}", 50000))  # one machine, many addresses
        try:
            c.post("/api/v1/auth/login", json={"username": "nobody", "password": "x", "client": "x"})
        finally:
            c.__exit__(None, None, None)
    c = client(("2001:db8:1:2::ffff", 50000))
    try:
        assert c.post("/api/v1/auth/login", json={"username": "kid", "password": "listen-only", "client": "x"}).status_code == 429
    finally:
        c.__exit__(None, None, None)


def test_checking_the_current_password_is_limited_too(people):
    c = client(AWAY)
    try:
        c.post("/api/auth/login", json={"username": "kid", "password": "listen-only"})
        for _ in range(auth.LOGIN_TRIES):
            c.put("/api/auth/password", json={"current": "guess", "password": "whatever-new"})
        assert c.put("/api/auth/password", json={"current": "listen-only", "password": "whatever-new"}).status_code == 429
    finally:
        c.__exit__(None, None, None)


def test_using_a_retired_access_token_signs_the_app_out(app, people):
    t0 = login(app)
    t1 = app.post("/api/v1/auth/refresh", json={"refresh_token": t0["refresh_token"]}).json()
    thief = app.post("/api/v1/auth/refresh", json={"refresh_token": t0["refresh_token"]}).json()  # replayed copy
    r = app.get("/api/v1/me", headers=bearer(t1["access_token"]))  # the real app's next call
    assert r.status_code == 401 and err(r) == "invalid_token"
    assert app.post("/api/v1/auth/refresh", json={"refresh_token": thief["refresh_token"]}).status_code == 401
    assert app.get("/api/v1/me", headers=bearer(thief["access_token"])).status_code == 401


def test_right_passwords_dont_count_against_anyone(app, people):
    for _ in range(auth.LOGIN_TRIES * 2):
        login(app)


def test_passwords_are_taken_exactly_as_typed(app):
    users.create("spacey", "  padded pass ", "listener")
    assert app.post("/api/v1/auth/login", json={"username": "spacey", "password": "  padded pass ", "client": "x"}).status_code == 200
    assert app.post("/api/v1/auth/login", json={"username": "spacey", "password": "padded pass", "client": "x"}).status_code == 401


def test_the_real_address_behind_a_trusted_proxy(monkeypatch):
    class Req:
        def __init__(self, host, xff):
            self.client = type("C", (), {"host": host})()
            from starlette.datastructures import Headers
            self.headers = Headers({"x-forwarded-for": xff} if xff else {})

    assert auth.client_ip(Req("10.0.0.2", "1.2.3.4")) == "10.0.0.2"  # untrusted: headers ignored
    monkeypatch.setenv("NARRATARR_TRUSTED_PROXIES", "10.0.0.0/24")
    assert auth.client_ip(Req("10.0.0.2", "1.2.3.4")) == "1.2.3.4"
    assert auth.client_ip(Req("10.0.0.2", "6.6.6.6, 1.2.3.4, 10.0.0.9")) == "1.2.3.4"  # a forged first hop is ignored
    assert auth.client_ip(Req("10.0.0.2", "not-an-ip")) == "10.0.0.2"
    assert auth.client_ip(Req("10.0.0.2", "1.2.3.4:5678")) == "1.2.3.4"
    assert auth.client_ip(Req("10.0.0.2", "[2001:db8::1]:443")) == "2001:db8::1"
    assert auth.client_ip(Req("10.0.0.2", "[2001:db8::1]")) == "2001:db8::1"
    assert auth.client_ip(Req("10.0.0.2", "unknown")) == "10.0.0.2"
    assert auth.client_ip(Req("8.8.8.8", "1.2.3.4")) == "8.8.8.8"  # not our proxy


def test_the_attempt_table_stays_bounded_without_forgetting_everyone(monkeypatch):
    monkeypatch.setattr(auth, "_MAX_KEYS", 50)
    now = time.time()
    auth._failures["ip:victim-of-a-guesser"] = [now] * auth.LOGIN_TRIES
    for n in range(200):
        auth._failures[f"ip:old-{n}"] = [now - auth.LOGIN_WINDOW - 1]
    auth._trim(now)
    assert len(auth._failures) <= 50 and "ip:victim-of-a-guesser" in auth._failures


# ---- signing apps out ---------------------------------------------------------------------------------


def test_logout(app, people):
    t = login(app)
    assert app.post("/api/v1/auth/logout", headers=bearer(t["access_token"])).status_code == 204
    assert app.get("/api/v1/me", headers=bearer(t["access_token"])).status_code == 401
    assert app.post("/api/v1/auth/refresh", json={"refresh_token": t["refresh_token"]}).status_code == 401


def test_changing_the_password_or_removing_the_person_signs_apps_out(app, people):
    admin, kid = people
    t = login(app)
    users.set_password(kid["id"], "a-new-password")
    assert app.get("/api/v1/me", headers=bearer(t["access_token"])).status_code == 401
    t = login(app, password="a-new-password")
    users.delete(kid["id"])
    assert app.get("/api/v1/me", headers=bearer(t["access_token"])).status_code == 401
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM app_tokens").fetchone()[0] == 0


def test_people_see_and_revoke_their_own_apps_in_the_web_ui(app, people):
    t = login(app)
    web = client(HOME)
    try:
        web.post("/api/auth/login", json={"username": "desmond", "password": "correct horse"})
        assert web.get("/api/auth/apps").json() == []  # someone else's apps aren't listed…
        grant = tokens.grants(users.by_name("kid")["id"])[0]
        assert web.delete(f"/api/auth/apps/{grant['id']}").status_code == 404  # …or revocable
        web.post("/api/auth/logout")
        web.post("/api/auth/login", json={"username": "kid", "password": "listen-only"})
        mine = web.get("/api/auth/apps").json()
        assert [a["name"] for a in mine] == ["Fable Frog on iPhone"] and mine[0]["last_ip"] == AWAY[0]
        assert web.delete(f"/api/auth/apps/{mine[0]['id']}").status_code == 200
    finally:
        web.__exit__(None, None, None)
    assert app.get("/api/v1/me", headers=bearer(t["access_token"])).status_code == 401


def test_the_home_network_shortcut_cant_manage_apps(people):
    from narratarr import config

    config.update({"auth_local_bypass": True})
    c = client(HOME)
    try:
        assert c.get("/api/auth/apps").status_code == 403
        assert c.delete("/api/auth/apps/1").status_code == 403
    finally:
        c.__exit__(None, None, None)


def test_signing_in_too_many_apps_retires_the_oldest(app, people, monkeypatch):
    monkeypatch.setattr(tokens, "MAX_GRANTS", 3)
    first = login(app, app_name="one")
    for name in ("two", "three", "four"):
        login(app, app_name=name)
    names = [g["name"] for g in tokens.grants(users.by_name("kid")["id"])]
    assert sorted(names) == ["four", "three", "two"]
    assert app.get("/api/v1/me", headers=bearer(first["access_token"])).status_code == 401


# ---- searching and requesting ---------------------------------------------------------------------------


@pytest.fixture
def kid(app, people):
    return bearer(login(app)["access_token"])


def test_search(app, kid):
    r = app.get("/api/v1/search", params={"q": "dune"}, headers=kid).json()
    assert r["page"] == 1 and r["pages"] == 1
    (b,) = r["items"]
    assert b["asin"] == "B002V1OF70" and b["authors"] == ["Frank Herbert"] and b["runtime_minutes"] == 1260
    assert b["series"] == [{"asin": "B00GMG4N8E", "name": "Dune", "position": "1"}] and b["request"] is None
    assert app.get("/api/v1/search", params={"q": "  "}, headers=kid).status_code == 422
    assert app.get("/api/v1/search", params={"q": "x" * 201}, headers=kid).status_code == 422


def test_request_a_book_and_follow_it(app, kid):
    r = app.post("/api/v1/requests", json={"asin": "B002V1OF70"}, headers=kid)
    assert r.status_code == 201 and r.headers["location"] == f"/api/v1/requests/{r.json()['id']}"
    req = r.json()
    assert (req["status"], req["stage"], req["progress"]) == ("wanted", None, None)
    assert req["requested_by"]["username"] == "kid" and req["requested_at"].endswith("Z")
    assert req["narrators"] == ["Scott Brick", "Orlagh Cassidy"]

    again = app.post("/api/v1/requests", json={"asin": "B002V1OF70"}, headers=kid)
    assert again.status_code == 200 and again.json()["id"] == req["id"]  # asking twice is harmless
    assert app.get("/api/v1/search", params={"q": "dune"}, headers=kid).json()["items"][0]["request"]["id"] == req["id"]
    assert app.get("/api/v1/books/B002V1OF70", headers=kid).json()["request"]["status"] == "wanted"

    db.update(req["id"], status="downloading", progress=42.04, detail="3.1 MB/s")
    got = app.get(f"/api/v1/requests/{req['id']}", headers=kid).json()
    assert (got["status"], got["stage"], got["progress"], got["message"]) == ("downloading", "downloading", 42.0, "3.1 MB/s")
    db.update(req["id"], status="completed", progress=100, completed_at=time.time())
    got = app.get(f"/api/v1/requests/{req['id']}", headers=kid).json()
    assert got["status"] == "available" and got["available_at"].endswith("Z") and got["progress"] is None


def test_a_book_already_in_the_library_isnt_requested_again(app, kid):
    db.insert({"source": "local", "source_url": "local:dune", "title": "Dune", "author": "Frank Herbert",
               "status": "completed"})
    r = app.post("/api/v1/requests", json={"asin": "B002V1OF70"}, headers=kid)
    assert r.status_code == 200 and r.json()["status"] == "available"
    assert len(db.list_books()) == 1


def test_request_validation_and_unknown_books(app, kid):
    assert err(app.post("/api/v1/requests", json={"asin": "b002v1of70"}, headers=kid)) == "validation_error"
    assert err(app.post("/api/v1/requests", json={"asin": "'; DROP TABLE books"}, headers=kid)) == "validation_error"
    assert err(app.post("/api/v1/requests", json={}, headers=kid)) == "validation_error"
    assert err(app.post("/api/v1/requests", json={"asin": "B000000000"}, headers=kid)) == "not_found"
    assert err(app.get("/api/v1/books/nope", headers=kid)) == "validation_error"
    assert err(app.get("/api/v1/requests/999", headers=kid)) == "not_found"
    assert err(app.get("/api/v1/nowhere", headers=kid)) == "not_found"


def test_book_lookups_are_limited(app, kid, monkeypatch):
    monkeypatch.setattr(apiv1, "search_limit", apiv1.Limiter(2, 60))
    for _ in range(2):
        assert app.get("/api/v1/books/B002V1OF70", headers=kid).status_code == 200
    assert err(app.get("/api/v1/books/B002V1OF70", headers=kid)) == "rate_limited"


def test_audible_being_down_is_a_clear_error(app, kid, monkeypatch):
    async def down(*a, **k):
        raise audible.AudibleError("Couldn't reach Audible's catalog. Try again in a moment.")

    monkeypatch.setattr(audible, "search_books", down)
    r = app.get("/api/v1/search", params={"q": "dune"}, headers=kid)
    assert r.status_code == 502 and err(r) == "upstream_unavailable"


def test_listing_requests(app, kid, people):
    admin_token = bearer(login(app, "desmond", "correct horse")["access_token"])
    mine = app.post("/api/v1/requests", json={"asin": "B002V1OF70"}, headers=kid).json()
    theirs = app.post("/api/v1/requests", json={"asin": "B08G9PRS1K"}, headers=admin_token).json()
    db.update(theirs["id"], status="failed", error="No release found")

    every = app.get("/api/v1/requests", headers=kid).json()
    assert every["total"] == 2 and {i["id"] for i in every["items"]} == {mine["id"], theirs["id"]}
    assert [i["id"] for i in app.get("/api/v1/requests", params={"mine": "true"}, headers=kid).json()["items"]] == [mine["id"]]
    failed = app.get("/api/v1/requests", params={"status": "failed"}, headers=kid).json()["items"]
    assert [(i["id"], i["message"]) for i in failed] == [(theirs["id"], "No release found")]
    assert app.get("/api/v1/requests", params={"status": "wanted,failed"}, headers=kid).json()["total"] == 2
    page = app.get("/api/v1/requests", params={"limit": 1, "offset": 1}, headers=kid).json()
    assert (len(page["items"]), page["total"], page["limit"], page["offset"]) == (1, 2, 1, 1)
    assert err(app.get("/api/v1/requests", params={"status": "done"}, headers=kid)) == "validation_error"
    assert err(app.get("/api/v1/requests", params={"limit": 1000}, headers=kid)) == "validation_error"


def test_cancelling(app, kid, people):
    admin_token = bearer(login(app, "desmond", "correct horse")["access_token"])
    mine = app.post("/api/v1/requests", json={"asin": "B002V1OF70"}, headers=kid).json()
    theirs = app.post("/api/v1/requests", json={"asin": "B08G9PRS1K"}, headers=admin_token).json()

    assert err(app.delete(f"/api/v1/requests/{theirs['id']}", headers=kid)) == "forbidden"  # not yours
    assert app.delete(f"/api/v1/requests/{mine['id']}", headers=kid).status_code == 204
    assert db.get(mine["id"]) is None

    db.update(theirs["id"], status="completed")
    assert err(app.delete(f"/api/v1/requests/{theirs['id']}", headers=admin_token)) == "conflict"  # books stay
    db.update(theirs["id"], status="failed")
    assert app.delete(f"/api/v1/requests/{theirs['id']}", headers=admin_token).status_code == 204


def test_requests_are_rate_limited_per_person(app, kid, monkeypatch):
    monkeypatch.setattr(apiv1, "request_limit", apiv1.Limiter(1, 3600))
    assert app.post("/api/v1/requests", json={"asin": "B002V1OF70"}, headers=kid).status_code == 201
    r = app.post("/api/v1/requests", json={"asin": "B08G9PRS1K"}, headers=kid)
    assert r.status_code == 429 and err(r) == "rate_limited" and int(r.headers["retry-after"]) > 0
    assert app.post("/api/v1/requests", json={"asin": "B002V1OF70"}, headers=kid).status_code == 200  # re-asking is free


def test_requests_from_the_web_ui_remember_who_asked(people):
    c = client(HOME)
    try:
        c.post("/api/auth/login", json={"username": "kid", "password": "listen-only"})
        book = c.post("/api/library", json={"url": DUNE["url"], "source": "audible", "download": False}).json()
        assert book["requested_by"] == people[1]["id"]
    finally:
        c.__exit__(None, None, None)


# ---- the published API ------------------------------------------------------------------------------


def test_docs_are_published_for_v1_only(app):
    schema = app.get("/api/v1/openapi.json").json()
    assert "/requests" in schema["paths"] and "/auth/login" in schema["paths"]
    assert app.get("/api/v1/docs").status_code == 200
    assert app.get("/openapi.json").status_code != 200 and app.get("/docs").status_code != 200


def test_other_websites_cant_use_a_session_to_request(app, people):
    app.post("/api/auth/login", json={"username": "kid", "password": "listen-only"})
    r = app.post("/api/v1/requests", json={"asin": "B002V1OF70"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403 and db.list_books() == []


# ---- staying stable ------------------------------------------------------------------------------------

CONTRACT = Path(__file__).parent.parent / "docs" / "openapi-v1.json"
INPUTS = {"Login", "Refresh", "NewRequest"}  # what apps send; everything else is what they get back


def _plain(schema):
    """A schema without the wording, which can change freely."""
    if isinstance(schema, dict):
        return {k: _plain(v) for k, v in schema.items() if k not in ("description", "title", "summary", "examples")}
    if isinstance(schema, list):
        return [_plain(v) for v in schema]
    return schema


def test_v1_only_ever_grows():
    """Nothing an app relies on may disappear or change. To add to the API, regenerate the
    contract: python -m narratarr.apiv1 > docs/openapi-v1.json"""
    old, new = json.loads(CONTRACT.read_text()), apiv1.contract()
    for path, ops in old["paths"].items():
        assert path in new["paths"], f"{path} was removed"
        for method, op in ops.items():
            assert method in new["paths"][path], f"{method.upper()} {path} was removed"
            now = new["paths"][path][method]
            for param in op.get("parameters", []):
                match = next((q for q in now.get("parameters", []) if q["name"] == param["name"]), None)
                assert match is not None, f"{method.upper()} {path} lost its {param['name']} parameter"
                assert _plain(match["schema"]) == _plain(param["schema"]), f"{param['name']} on {path} changed"
            for p in now.get("parameters", []):
                assert not p.get("required") or any(q["name"] == p["name"] for q in op.get("parameters", [])), \
                    f"{method.upper()} {path} has a new required parameter {p['name']}"
            assert set(op["responses"]) <= set(now["responses"]), f"{method.upper()} {path} lost a response"
    for name, schema in old["components"]["schemas"].items():
        assert name in new["components"]["schemas"], f"{name} was removed"
        now = new["components"]["schemas"][name]
        for field, spec in schema.get("properties", {}).items():
            assert field in now.get("properties", {}), f"{name}.{field} was removed"
            assert _plain(now["properties"][field]) == _plain(spec), f"{name}.{field} changed type"
        if name in INPUTS:  # a new required input would break apps that don't send it
            assert set(now.get("required", [])) <= set(schema.get("required", [])), f"{name} needs a new field"
        else:  # an output field that may now be missing would break apps that read it
            assert set(schema.get("required", [])) <= set(now.get("required", [])), f"{name} made a field optional"


def test_concurrent_refreshes_leave_one_live_pair(people):
    pair = tokens.issue(people[1]["id"], "Fable Frog")
    results, errors = [], []

    def go():
        try:
            results.append(tokens.refresh(pair["refresh_token"])[0])
        except tokens.TokenError as e:
            errors.append(e)

    threads = [threading.Thread(target=go) for _ in range(1 + tokens.RETRIES_PER_TOKEN)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 1 + tokens.RETRIES_PER_TOKEN and not errors  # one trade, three retries: all fine
    with db.connect() as conn:
        live = conn.execute("SELECT token_hash FROM app_tokens WHERE kind='access' AND superseded=0 AND parent != ''").fetchall()
    assert len(live) == 1
    newest = next(r for r in results if tokens._digest(r["access_token"]) == live[0]["token_hash"])
    assert tokens.authenticate(newest["access_token"])[0]["username"] == "kid"
    retired = next(r for r in results if r is not newest)
    with pytest.raises(tokens.TokenError):
        tokens.authenticate(retired["access_token"])  # two holders of one refresh token: everyone out
    with pytest.raises(tokens.TokenError):
        tokens.authenticate(newest["access_token"])


def test_endless_retries_of_one_refresh_token_sign_the_app_out(people):
    pair = tokens.issue(people[1]["id"], "Fable Frog")
    for _ in range(1 + tokens.RETRIES_PER_TOKEN):
        tokens.refresh(pair["refresh_token"])
    with pytest.raises(tokens.TokenError):
        tokens.refresh(pair["refresh_token"])
    assert tokens.grants(people[1]["id"]) == []


def test_retried_pairs_cant_pile_up(people, monkeypatch):
    monkeypatch.setattr(tokens, "MAX_SUPERSEDED", 4)
    monkeypatch.setattr(tokens, "REFRESHES_PER_WINDOW", 1000)
    pair = tokens.issue(people[1]["id"], "Fable Frog")
    with pytest.raises(tokens.TokenError):
        for _ in range(10):  # each step: trade, retry, retry
            current = pair["refresh_token"]
            for _ in range(3):
                pair = tokens.refresh(current)[0]
    assert tokens.grants(people[1]["id"]) == []


def test_a_thief_cant_bury_the_tripwire(app, people, monkeypatch):
    """Refreshing again and again mustn't clean away the app's superseded pair."""
    monkeypatch.setattr(tokens, "REFRESHES_PER_WINDOW", 1000)
    t0 = login(app)
    t1 = app.post("/api/v1/auth/refresh", json={"refresh_token": t0["refresh_token"]}).json()  # the app
    thief = app.post("/api/v1/auth/refresh", json={"refresh_token": t0["refresh_token"]}).json()  # a copy
    for _ in range(tokens.KEEP_SPENT + 10):
        thief = app.post("/api/v1/auth/refresh", json={"refresh_token": thief["refresh_token"]}).json()
    r = app.post("/api/v1/auth/refresh", json={"refresh_token": t1["refresh_token"]})  # the app comes back
    assert r.status_code == 401
    assert app.get("/api/v1/me", headers=bearer(thief["access_token"])).status_code == 401  # the thief is out


def test_an_idle_app_hears_expired_not_invalid(app, people, monkeypatch):
    idle = login(app, app_name="iPad")
    later = time.time() + tokens.ACCESS_SECONDS + 600
    monkeypatch.setattr(tokens.time, "time", lambda: later)
    other = login(app, app_name="iPhone")  # sign-ins and refreshes elsewhere clean up old tokens
    app.post("/api/v1/auth/refresh", json={"refresh_token": other["refresh_token"]})
    r = app.get("/api/v1/me", headers=bearer(idle["access_token"]))
    assert r.status_code == 401 and err(r) == "token_expired"
    assert app.post("/api/v1/auth/refresh", json={"refresh_token": idle["refresh_token"]}).status_code == 200
