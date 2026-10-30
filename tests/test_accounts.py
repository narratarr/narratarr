"""Accounts: first-run setup, signing in, roles, each person's own progress, and the home-network opt-out."""

import io
import sys

import pytest
from fastapi.testclient import TestClient

from narratarr import api, auth, config, db, users
from narratarr.__main__ import users_cli

pytestmark = pytest.mark.real_auth

HOME = ("192.168.1.20", 50000)  # a phone on the home network
AWAY = ("8.8.4.4", 50000)  # somewhere on the internet


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    auth._failures.clear()
    auth._known.clear()
    monkeypatch.setattr(auth, "_setup_code", "")
    monkeypatch.delenv("NARRATARR_PASSWORD", raising=False)
    monkeypatch.setattr(users, "_N", 2**10)  # fast hashing in tests; real installs use 2^15


def client(where=HOME):
    c = TestClient(api.app, client=where)
    c.__enter__()
    c.app.state.worker.poke = lambda: None
    return c


@pytest.fixture
def home():
    c = client(HOME)
    yield c
    c.__exit__(None, None, None)


@pytest.fixture
def away():
    c = client(AWAY)
    yield c
    c.__exit__(None, None, None)


def book(title="Dune"):
    return db.insert({"source": "local", "source_url": f"local:{title}", "title": title, "status": "completed"})


def sign_in(c, username, password):
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r


# ---- first run ----------------------------------------------------------------------------


def test_nothing_works_until_the_admin_exists(home):
    r = home.get("/api/library")
    assert r.status_code == 401 and r.json()["setup"] is True
    assert home.get("/api/health").status_code == 200  # Docker health checks stay open
    assert home.get("/api/auth/status").json() == {"setup": True, "setup_code_needed": False, "user": None}
    assert home.get("/").status_code == 200  # the page itself loads, to show the setup screen


def test_setup_from_the_home_network_needs_no_code(home):
    r = home.post("/api/auth/setup", json={"username": "desmond", "password": "correct horse"})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["role"] == "admin"
    assert home.get("/api/library").status_code == 200  # signed in by the setup itself
    again = home.post("/api/auth/setup", json={"username": "mallory", "password": "correct horse"})
    assert again.status_code == 409


def test_setup_from_the_internet_needs_the_code_from_the_log(away):
    assert away.get("/api/auth/status").json()["setup_code_needed"] is True
    wrong = away.post("/api/auth/setup", json={"username": "mallory", "password": "correct horse", "code": "0000-0000"})
    assert wrong.status_code == 403 and users.count() == 0
    ok = away.post("/api/auth/setup", json={"username": "desmond", "password": "correct horse", "code": auth.setup_code()})
    assert ok.status_code == 200


def test_requests_through_a_proxy_are_never_local(home):
    r = home.post("/api/auth/setup", json={"username": "mallory", "password": "correct horse"},
                  headers={"X-Forwarded-For": "198.51.100.7"})
    assert r.status_code == 403


def test_existing_listening_progress_goes_to_the_first_admin(home):
    b = book()
    db.update(b["id"], listen_position=600.0, listen_duration=3600.0, listened_at=1.0)
    home.post("/api/auth/setup", json={"username": "desmond", "password": "correct horse"})
    mine = home.get("/api/library").json()[0]
    assert (mine["listen_position"], mine["listen_duration"]) == (600.0, 3600.0)


def test_the_old_shared_password_becomes_the_admin_account(monkeypatch):
    monkeypatch.setenv("NARRATARR_PASSWORD", "the-old-secret")
    c = client(AWAY)
    try:
        assert users.by_name("admin")["role"] == "admin"
        sign_in(c, "admin", "the-old-secret")
        assert c.get("/api/library").status_code == 200
    finally:
        c.__exit__(None, None, None)


# ---- signing in -------------------------------------------------------------------------------


def test_sign_in_and_out(away):
    users.create("desmond", "correct horse", "admin")
    assert away.get("/api/library").status_code == 401
    r = away.post("/api/auth/login", json={"username": "DESMOND", "password": "correct horse"})  # names ignore case
    assert r.status_code == 200
    cookie = r.cookies[auth.COOKIE]
    assert "httponly" in r.headers["set-cookie"].lower()
    with db.connect() as conn:  # only a hash of the session is stored
        assert cookie not in str([tuple(row) for row in conn.execute("SELECT * FROM sessions")])
    assert away.get("/api/library").status_code == 200
    away.post("/api/auth/logout")
    assert away.get("/api/library").status_code == 401


def test_wrong_passwords_are_slowed_down(away):
    users.create("desmond", "correct horse", "admin")
    for _ in range(auth.LOGIN_TRIES):
        assert away.post("/api/auth/login", json={"username": "desmond", "password": "nope"}).status_code == 401
    assert away.post("/api/auth/login", json={"username": "desmond", "password": "correct horse"}).status_code == 429


def test_changing_a_password_signs_out_everywhere_else(home, away):
    users.create("desmond", "correct horse", "admin")
    sign_in(home, "desmond", "correct horse")
    sign_in(away, "desmond", "correct horse")
    r = home.put("/api/auth/password", json={"current": "correct horse", "password": "battery staple"})
    assert r.status_code == 200
    assert home.get("/api/library").status_code == 200  # this browser stays signed in
    assert away.get("/api/library").status_code == 401


# ---- roles -----------------------------------------------------------------------------------


def test_listeners_listen_but_dont_run_the_place(home):
    users.create("desmond", "correct horse", "admin")
    users.create("kid", "listen-only", "listener")
    b = book()
    sign_in(home, "kid", "listen-only")
    assert home.get("/api/library").status_code == 200
    assert home.put(f"/api/library/{b['id']}/progress", json={"position": 30}).status_code == 200
    assert home.get("/api/users").status_code == 403
    assert home.put("/api/settings", json={"library_dir": "/tmp/x"}).status_code == 403
    assert home.post("/api/settings/test-prowlarr").status_code == 403
    assert home.post("/api/audiobookshelf/find", json={}).status_code == 403
    assert home.delete(f"/api/library/{b['id']}").status_code == 403
    seen = home.get("/api/settings").json()
    assert "torrent_configured" in seen
    assert not {"rd_api_key", "library_dir", "prowlarr_url", "audiobookshelf_token"} & set(seen)


def test_everyone_has_their_own_place_in_a_book(home, away):
    users.create("desmond", "correct horse", "admin")
    users.create("kid", "listen-only", "listener")
    b = book()
    sign_in(home, "desmond", "correct horse")
    sign_in(away, "kid", "listen-only")
    home.put(f"/api/library/{b['id']}/progress", json={"position": 1000, "duration": 5000})
    away.put(f"/api/library/{b['id']}/progress", json={"position": 20, "finished": True})
    assert home.get("/api/library").json()[0]["listen_position"] == 1000
    assert home.get("/api/library").json()[0]["finished"] is False
    assert away.get("/api/library").json()[0]["finished"] is True


def test_admins_manage_users_but_never_lose_the_last_admin(home):
    users.create("desmond", "correct horse", "admin")
    sign_in(home, "desmond", "correct horse")
    kid = home.post("/api/users", json={"username": "kid", "password": "listen-only"}).json()
    assert kid["role"] == "listener"
    assert home.post("/api/users", json={"username": "Kid", "password": "whatever-long"}).status_code == 400
    assert home.post("/api/users", json={"username": "x", "password": "short"}).status_code == 400
    me = users.by_name("desmond")
    assert home.patch(f"/api/users/{me['id']}", json={"role": "listener"}).status_code == 400
    assert home.delete(f"/api/users/{me['id']}").status_code == 400
    assert home.delete(f"/api/users/{kid['id']}").status_code == 200
    assert [u["username"] for u in home.get("/api/users").json()] == ["desmond"]


# ---- the home-network opt-out -------------------------------------------------------------------


def test_skipping_sign_in_at_home_is_off_by_default_and_never_reaches_the_internet(home, away):
    users.create("desmond", "correct horse", "admin")
    assert home.get("/api/library").status_code == 401
    config.update({"auth_local_bypass": True})
    assert home.get("/api/library").status_code == 200
    assert home.get("/api/auth/status").json()["user"]["username"] == "desmond"
    assert away.get("/api/library").status_code == 401
    assert home.get("/api/library", headers={"X-Forwarded-For": "198.51.100.7"}).status_code == 401


# ---- the command line --------------------------------------------------------------------------------


def run_cli(monkeypatch, *argv, stdin=""):
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    return users_cli(list(argv))


def test_cli(monkeypatch, capsys):
    db.init()
    assert run_cli(monkeypatch, "any") == 1
    assert run_cli(monkeypatch, "add", "desmond", "--password-stdin", stdin="correct horse\n") == 0
    assert users.by_name("desmond")["role"] == "admin"  # the first account is the admin
    assert run_cli(monkeypatch, "add", "kid", "--password-stdin", stdin="listen-only\n") == 0
    assert users.by_name("kid")["role"] == "listener"
    assert run_cli(monkeypatch, "any") == 0
    assert run_cli(monkeypatch, "passwd", "kid", "--password-stdin", stdin="a-new-password\n") == 0
    assert users.authenticate("kid", "a-new-password")
    assert run_cli(monkeypatch, "remove", "desmond") == 1  # the only admin
    assert run_cli(monkeypatch, "role", "kid", "admin") == 0
    assert run_cli(monkeypatch, "remove", "desmond") == 0
    assert run_cli(monkeypatch, "add", "x", "--password-stdin", stdin="short\n") == 1
    assert "at least 8" in capsys.readouterr().err


def test_a_database_from_before_the_superseded_column_is_upgraded():
    with db.connect() as conn:
        conn.execute("DROP TABLE app_tokens")
        conn.execute("CREATE TABLE app_tokens (token_hash TEXT PRIMARY KEY, grant_id INTEGER NOT NULL, kind TEXT NOT NULL, "
                     "created_at REAL NOT NULL, expires_at REAL NOT NULL, used_at REAL, parent TEXT NOT NULL DEFAULT '')")
    db.init()
    with db.connect() as conn:
        assert "superseded" in {r["name"] for r in conn.execute("PRAGMA table_info(app_tokens)")}
