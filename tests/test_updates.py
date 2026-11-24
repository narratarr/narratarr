"""Update checks and self-update."""

import httpx
import pytest
from fastapi.testclient import TestClient

from narratarr import api, config, updates


@pytest.fixture(autouse=True)
def fresh():
    updates._cache.clear()
    updates._state.update(status="idle", detail="", started=0.0)


def github(body=None, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.github.com" and "narratarr" in request.headers["user-agent"]
        return httpx.Response(status, json=body or {})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_versions_compare_as_numbers():
    assert updates.parse_version("v0.10.0") > updates.parse_version("0.9.9")
    assert updates.parse_version("0.4.0") > updates.parse_version("0.3.2")
    assert updates.parse_version("garbage") == (0,)


async def test_latest_release_is_cached_and_tolerates_outages():
    release = {
        "tag_name": "v9.9.9",
        "body": "## 9.9.9\n- Everything",
        "html_url": "https://github.com/x",
        "published_at": "2026-11-01T00:00:00Z",
    }
    got = await updates.latest_release(client=github(release))
    assert got == {
        "version": "9.9.9",
        "notes": "## 9.9.9\n- Everything",
        "url": "https://github.com/x",
        "published": "2026-11-01",
    }
    assert await updates.latest_release(client=github(status=500)) == got  # served from cache
    updates._cache.clear()
    assert (
        await updates.latest_release(client=github(status=500)) is None
    )  # outage: no crash, no update badge


async def test_status_respects_the_setting(monkeypatch):
    async def fake_latest(force=False, client=None):
        return {"version": "9.9.9", "notes": "", "url": "", "published": ""}

    monkeypatch.setattr(updates, "latest_release", fake_latest)
    config.update({"check_updates": False})
    s = await updates.status()
    assert s["latest"] is None and not s["update_available"] and not s["checks_enabled"]
    s = await updates.status(force=True)  # "Check now" still works
    assert s["update_available"] and s["latest"]["version"] == "9.9.9"
    config.update({"check_updates": True})
    assert (await updates.status())["update_available"]


def test_changelog_is_bundled():
    text = updates.changelog()
    assert text.startswith("# Changelog") and f"## {updates.current_version()}" in text


def test_apply_refuses_when_it_cannot_self_update(monkeypatch):
    monkeypatch.setattr(updates, "install_kind", lambda: "docker")
    with TestClient(api.app) as c:
        r = c.post("/api/updates/apply", json={"version": "9.9.9"})
        assert r.status_code == 400 and "can't update itself" in r.json()["detail"]
        assert c.post("/api/updates/apply", json={"version": "rm -rf /"}).status_code == 400
        assert c.get("/api/changelog").json()["current"] == updates.current_version()


def test_install_kind_detection(monkeypatch, tmp_path):
    monkeypatch.setattr(updates.sys, "prefix", str(tmp_path / ".local/share/uv/tools/narratarr"))
    assert updates.install_kind() == "uv"
    monkeypatch.setenv("NARRATARR_DATA", "/config")
    assert updates.install_kind() == "docker"
