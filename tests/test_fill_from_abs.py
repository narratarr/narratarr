"""From the Discord: Fill in missing details takes what the user's Audiobookshelf library already has."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from narratarr import api, audiobookshelf, config, db, wanted
from narratarr.audiobookshelf import _book_of, _first_series


@pytest.mark.parametrize("text, expected", [
    ("Dune Chronicles #1", [{"asin": "", "title": "Dune Chronicles", "sequence": "1"}]),
    ("The Expanse #2.5, Other #3", [{"asin": "", "title": "The Expanse", "sequence": "2.5"}]),
    ("Discworld", [{"asin": "", "title": "Discworld", "sequence": ""}]),
    ("Rivers, Roads", []),  # one series with a comma, or two? Not guessed at.
    ("", []),
])
def test_the_first_series_is_read_from_audiobookshelfs_one_line(text, expected):
    assert _first_series(text) == expected


def test_an_item_gives_narrators_and_a_date():
    item = {"media": {"metadata": {"title": "Dune", "authorName": "Frank Herbert", "narratorName": "Scott Brick, Euan Morton",
                                   "seriesName": "Dune Chronicles #1", "publishedYear": "1965", "asin": "B002V1OF70"}}}
    got = _book_of(item)
    assert got["narrators"] == ["Scott Brick", "Euan Morton"]
    assert got["released"] == "1965" and got["series"][0]["sequence"] == "1"
    assert _book_of({"media": {"metadata": {"publishedDate": "2007-01-01", "publishedYear": "2007"}}})["released"] == "2007-01-01"
    assert _book_of({"media": {"metadata": {"publishedYear": "unknown"}}})["released"] == ""


def _item(rel, title="Dune", author="Frank Herbert", **meta):
    base = {"path": f"/audiobooks/{rel}", "rel": rel, "missing": False, "title": title, "author": author,
            "asin": "", "narrators": ["Scott Brick"], "released": "1965",
            "series": [{"asin": "", "title": "Dune Chronicles", "sequence": "1"}]}
    return {**base, **meta}


def _book(lib, rel, title="Dune", author="Frank Herbert", **extra):
    return db.insert({"source": "local", "source_url": f"local:{rel}", "title": title, "author": author,
                      "status": "completed", "path": str(Path(lib) / rel), "kind": "audiobook", **extra})


def test_the_book_at_the_same_place_in_the_library_is_the_match(isolated_data):
    lib = config.load().library_dir
    b = _book(lib, "Frank Herbert/Dune (2019 rip)", title="dune rip")
    other = _item("Someone Else/Dune (2019 rip)", title="Not it")
    mine = _item("Frank Herbert/Dune (2019 rip)", title="Dune")
    assert wanted.abs_match(b, [other, mine], lib) is mine


def test_one_folder_name_alone_doesnt_match_a_book_elsewhere_in_the_library(isolated_data):
    lib = config.load().library_dir
    b = _book(lib, "Frank Herbert/Dune", title="Totally different", author="Nobody")
    assert wanted.abs_match(b, [_item("Dune", title="Dune")], lib) is None


def test_only_empty_details_are_filled(isolated_data):
    lib = config.load().library_dir
    b = _book(lib, "Frank Herbert/Dune", narrators=["Someone I picked"])
    changed = wanted.fill_from_audiobookshelf(b, [_item("Frank Herbert/Dune", asin="B002V1OF70")], lib)
    after = db.get(b["id"])
    assert set(changed) == {"series", "released", "asin"}
    assert after["narrators"] == ["Someone I picked"]
    assert after["series"][0]["title"] == "Dune Chronicles" and after["released"] == "1965"


def test_an_asin_that_isnt_one_is_ignored(isolated_data):
    lib = config.load().library_dir
    b = _book(lib, "Frank Herbert/Dune")
    wanted.fill_from_audiobookshelf(b, [_item("Frank Herbert/Dune", asin="../../x")], lib)
    assert not db.get(b["id"]).get("asin")


@pytest.fixture
def client():
    with TestClient(api.app) as c:
        c.app.state.worker.poke = lambda: None
        yield c


def test_audible_is_only_asked_for_what_audiobookshelf_didnt_have(client, isolated_data, monkeypatch):
    lib = config.load().library_dir
    whole = _book(lib, "Frank Herbert/Dune")
    partial = _book(lib, "Andy Weir/Artemis", title="Artemis", author="Andy Weir")
    items = [_item("Frank Herbert/Dune"),
             _item("Andy Weir/Artemis", title="Artemis", author="Andy Weir", series=[], released="")]

    async def owned(settings):
        return items

    asked = []

    async def audible_match(book):
        asked.append(book["title"])
        return None

    monkeypatch.setattr(audiobookshelf, "owned", owned)
    monkeypatch.setattr(wanted, "audible_match", audible_match)
    r = client.post("/api/tidy/fill-details", json={"batch": 12}).json()

    assert asked == ["Artemis"], "Dune had everything from Audiobookshelf"
    assert r["from_audiobookshelf"] == 2 and r["filled"] == 2 and r["remaining"] == 0
    assert db.get(whole["id"])["details_checked"]
    assert db.get(partial["id"])["narrators"] == ["Scott Brick"]
