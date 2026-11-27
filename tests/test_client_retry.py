"""A download client that is briefly unreachable no longer costs the book.

Narratarr used to fail a book as soon as the client refused a connection, which left a finished
torrent in the client with nobody to import it (the 10:29 case on the TrueNAS box: recreating the
Deluge container for a minute stranded two audiobooks, and nothing retried them). Such a failure is
about the client being up, not about the release, so the book now waits and tries the same release
again; only a client that stays down past every attempt ends up failed.
"""

import time

import httpx

from narratarr import db
from narratarr.downloaders import ClientError, is_client_unreachable
from narratarr.worker import CLIENT_RETRY_DELAYS, Worker

HASH1 = "a" * 40


def downloading(**over) -> dict:
    return db.insert({"source": "torznab", "source_url": "https://t.example/1", "title": "Dune",
                      "author": "Frank Herbert", "status": "queued", "release_url": "https://n.example/1",
                      "info_hash": HASH1, "protocol": "torrent", "client_job_id": "job-1", **over})


def unreachable(message: str = "Couldn't reach Deluge at 10.2.3.144:8112. Check the address in Settings.") -> ClientError:
    """The shape the client modules raise: their own error, `from` the transport failure."""
    try:
        try:
            raise httpx.ConnectError("connection refused")
        except httpx.ConnectError as cause:
            raise ClientError(message) from cause
    except ClientError as e:
        return e


def failing_with(monkeypatch, error: Exception):
    async def process(self, book_id):
        raise error
    monkeypatch.setattr(Worker, "process", process)


async def test_an_unreachable_client_requeues_the_book_instead_of_failing_it(monkeypatch):
    book = downloading()
    failing_with(monkeypatch, unreachable())
    before = time.time()
    await Worker()._run(book["id"])

    got = db.get(book["id"])
    assert got["status"] == "queued"          # not failed
    assert got["error"] == ""
    assert got["client_retries"] == 1
    assert got["retry_at"] >= before + CLIENT_RETRY_DELAYS[0] - 1
    assert "isn't answering" in got["detail"]


async def test_the_book_waits_its_turn_then_comes_back(monkeypatch):
    book = downloading()
    failing_with(monkeypatch, unreachable())
    await Worker()._run(book["id"])

    assert db.next_queued(set()) is None      # still waiting: the queue doesn't spin on it
    db.update(book["id"], retry_at=time.time() - 1)
    assert db.next_queued(set())["id"] == book["id"]


async def test_attempts_run_out_and_the_book_then_fails(monkeypatch):
    book = downloading(client_retries=len(CLIENT_RETRY_DELAYS))
    failing_with(monkeypatch, unreachable())
    await Worker()._run(book["id"])

    got = db.get(book["id"])
    assert got["status"] == "failed"
    assert "Couldn't reach Deluge" in got["error"]   # the client's own words reach the user


async def test_a_release_verdict_is_not_a_client_retry():
    """The client answered: another release is the right response, so this must not look transient."""
    from narratarr.downloaders import bad_release

    assert is_client_unreachable(bad_release(ClientError("qBittorrent reports a problem with this torrent."))) is False
    assert is_client_unreachable(ClientError("Deluge no longer has this download.")) is False
    assert is_client_unreachable(unreachable()) is True


def test_an_unreachable_indexer_is_not_a_client_retry():
    """A Torznab or Prowlarr error wraps the same transport failure, but the client isn't the problem:
    retrying in a minute would just ask the same unreachable indexer again."""
    from narratarr.sources import TorznabError

    try:
        try:
            raise httpx.ConnectError("no route to host")
        except httpx.ConnectError as cause:
            raise TorznabError("Your indexer took too long to answer.") from cause
    except TorznabError as e:
        err = e
    assert is_client_unreachable(err) is False


async def test_a_setup_problem_still_fails_at_once(monkeypatch):
    book = downloading()
    failing_with(monkeypatch, ValueError("Deluge saved this book to /downloads/Dune, but Narratarr can't see that folder."))
    await Worker()._run(book["id"])

    got = db.get(book["id"])
    assert got["status"] == "failed" and got["client_retries"] == 0 and got["retry_at"] is None


async def test_a_client_outage_does_not_mark_the_release_as_bad(monkeypatch):
    """Nothing is remembered against the release itself: once the client is back the same one is used."""
    book = downloading()
    failing_with(monkeypatch, unreachable())
    await Worker()._run(book["id"])

    got = db.get(book["id"])
    assert got["failed_releases"] == []
    assert got["release_url"] == "https://n.example/1"
