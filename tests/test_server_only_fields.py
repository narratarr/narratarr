"""A release's indexer link and trackers never leave the server (review of 0.9.5).

A Torznab feed served straight from a tracker can put a passkey in the link, and listeners see library
rows too. The page never used either field.
"""

import pytest

from narratarr import db


def _book():
    return db.insert(
        {
            "source": "torznab",
            "source_url": "https://tracker.example/t/1",
            "title": "Dune",
            "author": "Frank Herbert",
            "protocol": "torrent",
            "status": "completed",
            "download_url": "https://tracker.example/dl/1?passkey=SECRET",
            "trackers": ["https://tracker.example/announce/SECRET"],
        }
    )


@pytest.mark.real_auth
def test_library_rows_leave_out_the_link_and_trackers(accounts):
    b = _book()
    c = accounts
    for username, password in (("admin", "admin-pass"), ("listener", "listen-pass")):
        assert (
            c.post("/api/auth/login", json={"username": username, "password": password}).status_code
            == 200
        )
        rows = c.get("/api/library").json()
        assert rows and rows[0]["title"] == "Dune"
        assert "SECRET" not in str(rows)
        assert "download_url" not in rows[0] and "trackers" not in rows[0]
    assert db.get(b["id"])["download_url"].endswith("SECRET"), "the worker still has it"
