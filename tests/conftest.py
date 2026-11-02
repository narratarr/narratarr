from pathlib import Path

import pytest

from narratarr import config, db

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """Every test gets a fresh data dir, settings and database."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "data" / "settings.json")
    monkeypatch.setattr(config, "_settings", None)
    config.update({"library_dir": str(tmp_path / "library"), "rd_api_key": "test-key"})
    from narratarr.sources import audiobookbay

    audiobookbay._page_cache.clear()
    db.init()
    return tmp_path


ADMIN = {"id": 1, "username": "tester", "role": "admin", "created_at": 0}


@pytest.fixture(autouse=True)
def signed_in(request, monkeypatch):
    """API tests run as a signed-in admin. Tests marked real_auth go through real accounts and sessions."""
    if request.node.get_closest_marker("real_auth"):
        return
    from narratarr import auth

    monkeypatch.setattr(auth, "current_user", lambda request: ADMIN)


@pytest.fixture
def fixture_html():
    return lambda name: (FIXTURES / name).read_text()


# A real admin and a real listener, through the real middleware. Lives here rather than in one
# module: more than one test file needs to prove a permission boundary rather than assume it.
HOME = ("192.168.1.20", 50000)


@pytest.fixture
def accounts(monkeypatch):
    from fastapi.testclient import TestClient

    from narratarr import api, auth, users

    auth._failures.clear()
    monkeypatch.setattr(users, "_N", 2**10)  # fast hashing in tests
    c = TestClient(api.app, client=HOME)
    c.__enter__()
    c.app.state.worker.poke = lambda: None
    assert c.post("/api/auth/setup", json={"username": "admin", "password": "admin-pass"}).status_code == 200
    made = c.post("/api/users", json={"username": "listener", "password": "listen-pass", "role": "listener"})
    assert made.status_code == 200, made.text
    yield c
    c.__exit__(None, None, None)
