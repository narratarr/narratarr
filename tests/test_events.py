"""Server-sent events: the version counter, the stream, the ETag, and the frontend's use of them.

Issue #23: the shelf was polled every two seconds from every tab. These tests pin the replacement —
every library write moves the version, the stream says so, a reconnecting client can tell it missed
something, and a refetch it doesn't need is a 304.
"""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from narratarr import api, db, events

PACKAGE = Path(__file__).parent.parent / "narratarr"
SOURCE = (PACKAGE / "static" / "app.js").read_text()


# ---- the counter: every mutation, and only mutations --------------------------------------


def _book(**extra):
    return db.insert({"title": "Dune", "author": "Frank Herbert", "kind": "audiobook",
                      "source": "audiobookbay", "source_url": "https://example.test/dune",
                      "status": "wanted", **extra})


def test_adding_a_book_moves_the_version():
    before = events.library_version()
    _book()
    assert events.library_version() > before


def test_changing_a_book_moves_the_version():
    book = _book()
    before = events.library_version()
    db.update(book["id"], status="downloading")
    assert events.library_version() > before


def test_removing_a_book_moves_the_version():
    book = _book()
    before = events.library_version()
    db.delete(book["id"])
    assert events.library_version() > before


def test_recording_someones_place_moves_the_version():
    """The shelf shows how far you got, so a position is a library change like any other."""
    book = _book()
    before = events.library_version()
    db.save_progress(1, book["id"], position=42.0, duration=3600.0)
    assert events.library_version() > before


def test_reading_the_library_does_not_move_the_version():
    _book()
    before = events.library_version()
    db.list_books()
    db.get(1)
    assert events.library_version() == before


def test_every_runtime_write_to_the_books_table_goes_through_a_bump():
    """The counter is only complete if the writes to `books` are the ones that bump.

    A missed mutation is a stale shelf, which is the very thing polling was covering for, so this
    checks the module that owns the table rather than trusting each caller. `init()` is excluded on
    purpose: it repairs rows at startup, before a client can be connected to miss anything.
    """
    source = (PACKAGE / "db.py").read_text()
    runtime = source[: source.index("def init(")] + source[source.index("def _row("):]
    writes = re.findall(r"(?:UPDATE|INSERT INTO|DELETE FROM)\s+books\b", runtime)
    assert len(writes) == 3, writes          # insert, update, delete
    assert source.count("events.bump()") == 4, source.count("events.bump()")   # those three, plus progress


# ---- the message format ------------------------------------------------------------------


def test_a_frame_is_one_sse_message_of_compact_json():
    assert events.frame("library_changed", version=7) == 'data: {"type":"library_changed","version":7}\n\n'


def test_the_heartbeat_is_short_enough_for_a_proxy():
    # nginx and Nginx Proxy Manager drop a quiet connection well before this.
    assert events.HEARTBEAT_SECONDS <= 30


def test_the_heartbeat_is_a_comment_line_so_no_client_tries_to_parse_it():
    source = (PACKAGE / "events.py").read_text()
    assert re.search(r'yield ": ping\\n\\n"', source)


# ---- the stream --------------------------------------------------------------------------


@pytest.fixture
def client():
    c = TestClient(api.app)
    c.__enter__()
    c.app.state.worker.poke = lambda: None
    yield c
    c.__exit__(None, None, None)


def _messages(response, count, timeout=5.0):
    """Read `count` data frames from a streaming response without waiting for it to end."""
    found, lines = [], response.iter_lines()
    for line in lines:
        if line.startswith("data: "):
            found.append(json.loads(line[6:]))
        if len(found) == count:
            break
    return found


def test_the_stream_starts_by_saying_what_version_it_has(client):
    version = events.library_version()
    with client.stream("GET", "/api/events") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["x-accel-buffering"] == "no"
        assert r.headers["cache-control"] == "no-cache"
        first = _messages(r, 1)[0]
    assert first == {"type": "library_changed", "version": version}


def test_the_stream_reports_a_change_while_it_is_open(client, monkeypatch):
    monkeypatch.setattr(events, "TICK_SECONDS", 0.02)
    with client.stream("GET", "/api/events") as r:
        lines = r.iter_lines()
        handshake = json.loads(next(l for l in lines if l.startswith("data: "))[6:])
        db.update(_book()["id"], status="downloading")
        second = json.loads(next(l for l in lines if l.startswith("data: "))[6:])
    assert second["version"] > handshake["version"]
    assert second["type"] == "library_changed"


def test_the_stream_sends_a_heartbeat_comment_when_nothing_happens(client, monkeypatch):
    monkeypatch.setattr(events, "TICK_SECONDS", 0.01)
    monkeypatch.setattr(events, "HEARTBEAT_SECONDS", 0.03)
    with client.stream("GET", "/api/events") as r:
        # A comprehension would wait for the stream to end, and it never does.
        pings = []
        for line in r.iter_lines():
            if line.startswith(":"):
                pings.append(line)
                break
        assert pings == [": ping"]


# ---- the ETag: a refetch that has nothing to do is cheap ----------------------------------


def test_the_library_carries_an_etag_and_answers_a_matching_one_with_304(client):
    first = client.get("/api/library")
    assert first.status_code == 200
    tag = first.headers["etag"]
    assert tag == f'W/"{events.library_version()}"'

    again = client.get("/api/library", headers={"If-None-Match": tag})
    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["etag"] == tag


def test_the_etag_changes_when_the_library_does_and_the_old_one_stops_working(client):
    tag = client.get("/api/library").headers["etag"]
    _book()
    stale = client.get("/api/library", headers={"If-None-Match": tag})
    assert stale.status_code == 200
    assert stale.headers["etag"] != tag
    assert len(stale.json()) == 1


def test_a_book_change_makes_the_previous_etag_worthless(client):
    book = _book()
    tag = client.get("/api/library").headers["etag"]
    db.update(book["id"], status="downloaded")
    assert client.get("/api/library", headers={"If-None-Match": tag}).status_code == 200


# ---- the frontend: the poll is gone, the stream is used, and there is a fallback ----------


def test_the_two_second_poll_is_gone():
    assert "setInterval(refreshLibrary, 2000)" not in SOURCE
    assert "connectLibraryEvents();" in SOURCE


def test_the_frontend_opens_the_events_stream():
    assert 'new EventSource("/api/events")' in SOURCE
    assert 'message.type !== "library_changed"' in SOURCE


def test_the_frontend_refetches_once_per_burst():
    assert "function refetchLibrarySoon()" in SOURCE
    assert "clearTimeout(libraryRefetch)" in SOURCE
    assert re.search(r"LIBRARY_DEBOUNCE\s*=\s*\d+", SOURCE)


def test_the_frontend_falls_back_to_a_slow_poll_when_the_stream_breaks():
    assert "function slowLibraryPoll(on)" in SOURCE
    assert "stream.onerror" in SOURCE
    # 30 seconds, not 2: the fallback is a safety net, not the design.
    assert re.search(r"LIBRARY_SLOW_POLL\s*=\s*30000", SOURCE)
    assert re.search(r"LIBRARY_RETRY\s*=\s*30000", SOURCE)


def test_a_reconnect_does_not_trust_what_it_rendered():
    """The version is the whole point: after a gap, look once rather than assume."""
    assert "state.eventsVersion = 0;" in SOURCE
    assert "if (!state.eventsVersion)" in SOURCE


def test_an_old_browser_without_eventsource_still_works():
    assert 'typeof EventSource !== "function"' in SOURCE
