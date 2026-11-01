"""Notifications: what each destination receives, what's kept secret, and that sending never gets in the way."""

import asyncio
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from narratarr import api, auth, db, notify, users

BOOK = {"id": 4, "title": 'The "Martian"', "author": "Andy Weir", "narrators": ["R. C. Bray"],
        "cover_url": "https://covers.example/martian.jpg", "kind": "audiobook"}


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(notify, "RETRY_SECONDS", 0)


class Catcher:
    def __init__(self, status=200):
        self.status, self.requests = status, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status)


def dest(**overrides) -> dict:
    data = {"kind": "ntfy", "url": "https://ntfy.sh/books", "events": ["wanted", "completed", "failed"]}
    return notify.create({**data, **overrides})


# ---- what each kind sends --------------------------------------------------------------


def test_ntfy_posts_json_to_the_server_root():
    ctx = notify.context("wanted", BOOK, "alice")
    url, headers, body = notify.build({"kind": "ntfy", "url": "https://ntfy.example/sub/books", "token": "tk_abc"}, ctx)
    sent = json.loads(body)
    assert url == "https://ntfy.example/sub/"
    assert sent["topic"] == "books" and sent["title"] == "Added to Wanted"
    assert sent["message"] == 'alice wants The "Martian" by Andy Weir'
    assert sent["attach"] == BOOK["cover_url"]
    assert headers["Authorization"] == "Bearer tk_abc"
    _, headers, _ = notify.build({"kind": "ntfy", "url": "https://ntfy.sh/b", "token": "me:pw"}, ctx)
    assert headers["Authorization"].startswith("Basic ")


def test_gotify_and_discord():
    ctx = notify.context("completed", BOOK, "alice")
    url, headers, body = notify.build({"kind": "gotify", "url": "https://gotify.example/", "token": "AbC"}, ctx)
    assert url == "https://gotify.example/message" and headers["X-Gotify-Key"] == "AbC"
    assert json.loads(body)["message"] == 'The "Martian" by Andy Weir is in your library, for alice'

    hook = "https://discord.com/api/webhooks/1/secret"
    url, _, body = notify.build({"kind": "discord", "url": hook}, notify.context("wanted", {**BOOK, "title": "@everyone"}))
    sent = json.loads(body)
    assert url == hook
    assert sent["allowed_mentions"] == {"parse": []}  # a title can't ping the whole server
    assert sent["embeds"][0]["thumbnail"]["url"] == BOOK["cover_url"]


def test_webhook_body_is_filled_in_safely():
    n = {"kind": "webhook", "url": "https://hooks.example/x", "headers": [{"name": "Authorization", "value": "Bearer s"}],
         "body": '{"t": "{bookTitle}", "who": "{username}", "x": "{unknown}"}'}
    _, headers, body = notify.build(n, notify.context("wanted", BOOK, 'bob"}, "evil": "1'))
    sent = json.loads(body)  # quotes in the values didn't break out of their strings
    assert sent == {"t": 'The "Martian"', "who": 'bob"}, "evil": "1', "x": "{unknown}"}
    assert headers["Authorization"] == "Bearer s"


def test_a_library_only_cover_is_left_out():
    assert notify.context("wanted", {**BOOK, "cover_url": "/api/library/4/cover"})["cover"] == ""


# ---- saving destinations --------------------------------------------------------------


def test_secrets_are_masked_and_kept(isolated_data):
    hook = "https://discord.com/api/webhooks/123/very-secret-token"
    n = notify.create({"kind": "discord", "url": hook, "events": ["wanted"]})
    shown = notify.public(n)
    assert "very-secret" not in json.dumps(shown) and shown["url"].startswith("https://discord.com/•")
    notify.update(n["id"], {**shown, "name": "Family"})  # what Settings sends back
    assert notify.get(n["id"])["url"] == hook

    w = notify.create({"kind": "webhook", "url": "https://hooks.example/x", "events": ["failed"],
                       "headers": [{"name": "Authorization", "value": "Bearer topsecret"}]})
    shown = notify.public(w)
    assert "topsecret" not in json.dumps(shown)
    notify.update(w["id"], shown)
    assert notify.get(w["id"])["headers"] == [{"name": "Authorization", "value": "Bearer topsecret"}]
    assert (isolated_data / "data" / "notifications.json").stat().st_mode & 0o077 == 0  # only Narratarr can read it


@pytest.mark.parametrize(("data", "words"), [
    ({"kind": "ntfy", "url": "https://ntfy.sh"}, "topic"),
    ({"kind": "gotify", "url": "https://g.example"}, "application token"),
    ({"kind": "discord", "url": "https://discord.com/channels/1"}, "webhook address"),
    ({"kind": "webhook", "url": "ftp://x"}, "http"),
    ({"kind": "webhook", "url": "https://x.example", "body": '{"a": {title}}'}, "valid JSON"),
    ({"kind": "webhook", "url": "https://x.example", "headers": [{"name": "Host", "value": "x"}]}, "itself"),
    ({"kind": "webhook", "url": "https://x.example", "headers": [{"name": "X-A", "value": "a\r\nB: c"}]}, "lines"),
    ({"kind": "ntfy", "url": "https://ntfy.sh/b", "events": []}, "at least one"),
    ({"kind": "pager", "url": "https://x.example"}, "Choose"),
])
def test_bad_destinations_are_explained(isolated_data, data, words):
    with pytest.raises(notify.NotifyError, match=words):
        notify.create({"events": ["wanted"], **data})


# ---- sending ------------------------------------------------------------------------------


async def test_deliver_retries_a_server_error_but_not_a_refusal():
    flaky = Catcher(503)
    assert "error (503)" in await notify.deliver({"kind": "ntfy", "url": "https://ntfy.sh/b"}, notify.context("wanted", BOOK),
                                                 httpx.MockTransport(flaky))
    assert len(flaky.requests) == 2
    refused = Catcher(401)
    assert "token" in await notify.deliver({"kind": "ntfy", "url": "https://ntfy.sh/b"}, notify.context("wanted", BOOK),
                                           httpx.MockTransport(refused))
    assert len(refused.requests) == 1


async def test_emit_logs_and_sends_only_to_those_who_want_it(isolated_data, monkeypatch, caplog):
    wants = dest(name="Phone", events=["wanted"])
    dest(name="Done only", events=["completed"])
    dest(name="Off", events=["wanted"], enabled=False)
    sent = []

    async def fake_deliver(n, ctx, transport=None):
        sent.append((n["name"], ctx["message"]))
        return ""

    monkeypatch.setattr(notify, "deliver", fake_deliver)
    with caplog.at_level(logging.INFO, logger="narratarr.notify"):
        notify.emit("wanted", BOOK, "alice")
        await asyncio.gather(*notify._tasks)

    assert sent == [("Phone", 'alice wants The "Martian" by Andy Weir')]
    assert '[wanted] alice wants The "Martian" by Andy Weir (book 4)' in caplog.text
    assert notify.get(wants["id"])["last_at"]


async def test_a_failure_is_recorded_not_raised(isolated_data):
    n = dest()

    async def broken(*a, **k):
        raise RuntimeError("boom")

    notify.deliver, real = broken, notify.deliver
    try:
        notify.emit("wanted", BOOK, "alice")
        await asyncio.gather(*notify._tasks)
    finally:
        notify.deliver = real
    assert "boom" in notify.get(n["id"])["last_error"]


def test_emit_without_an_event_loop_still_logs(isolated_data, caplog):
    dest()
    with caplog.at_level(logging.INFO, logger="narratarr.notify"):
        notify.emit("failed", BOOK, error="disk full")
    assert "couldn't download: disk full" in caplog.text


async def test_the_requester_is_named(isolated_data):
    u = users.create("carol", "a-long-password", "listener")
    book = db.insert({"source": "audible", "source_url": "https://www.audible.com/pd/X", "title": "Dune",
                      "author": "Frank Herbert", "status": "queued", "requested_by": u["id"]})
    assert notify.context("completed", book)["message"] == "Dune by Frank Herbert is in your library, for carol"


# ---- the worker and the API ---------------------------------------------------------------


async def test_worker_announces_start_and_failure(isolated_data, monkeypatch):
    from narratarr.worker import Worker

    seen = []
    monkeypatch.setattr(notify, "emit", lambda event, book, username="", error="": seen.append((event, error)))
    book = db.insert({"source": "audiobookbay", "source_url": "https://audiobookbay.lu/abss/x/", "title": "X",
                      "author": "Y", "info_hash": "a" * 40, "status": "queued"})
    db.update(book["id"], rd_torrent_id="")
    from narratarr import config

    config.update({"torrent_client": "realdebrid", "rd_api_key": ""})
    await Worker(transport=httpx.MockTransport(Catcher(500)))._run(book["id"])
    assert seen[0] == ("grabbed", "")
    assert seen[1][0] == "failed" and "API key" in seen[1][1]


@pytest.fixture
def client():
    with TestClient(api.app) as c:
        c.app.state.worker.poke = lambda: None
        yield c


def test_settings_api_round_trip(client, monkeypatch):
    made = client.post("/api/settings/notifications", json={
        "kind": "gotify", "url": "https://gotify.example", "token": "SECRET-TOKEN", "events": ["wanted"]}).json()
    assert made["token"].startswith("•") and "SECRET" not in json.dumps(made)
    listed = client.get("/api/settings/notifications").json()
    assert [i["id"] for i in listed["items"]] == [made["id"]] and "wanted" in listed["events"]
    assert client.post("/api/settings/notifications", json={"kind": "gotify", "url": "x", "events": ["wanted"]}).status_code == 400

    catcher, real = Catcher(200), notify.deliver

    async def via_catcher(n, ctx, transport=None):
        return await real(n, ctx, httpx.MockTransport(catcher))

    monkeypatch.setattr(notify, "deliver", via_catcher)
    assert client.post(f"/api/settings/notifications/{made['id']}/test").status_code == 200
    assert catcher.requests and catcher.requests[0].headers["x-gotify-key"] == "SECRET-TOKEN"

    assert client.put(f"/api/settings/notifications/{made['id']}", json={"enabled": False}).json()["enabled"] is False
    assert client.delete(f"/api/settings/notifications/{made['id']}").status_code == 200
    assert client.delete(f"/api/settings/notifications/{made['id']}").status_code == 404



def test_only_admins_manage_notifications():
    assert auth.admin_only("GET", "/api/settings/notifications")
    assert auth.admin_only("POST", "/api/settings/notifications/3/test")
