"""From the Discord: series that Audible splits up, setting a series by hand, filling in what imported
books are missing, and counting the books already in Audiobookshelf."""

import httpx
import pytest
from fastapi.testclient import TestClient

from narratarr import api, audible, audiobookshelf, config, db, wanted


@pytest.fixture
def client():
    with TestClient(api.app) as c:
        c.app.state.worker.poke = lambda: None
        yield c


def book(asin, title, series=None, abridged=False, author="Conn Iggulden"):
    return {"asin": asin, "url": f"https://www.audible.com/pd/{asin}", "title": title, "author": author,
            "author_asins": ["B0024JAJII"], "narrators": [], "cover_url": "", "description": "", "released": "2020-01-01",
            "length": "", "language": "English", "kind": "SinglePartBook", "abridged": abridged,
            "series": [{"asin": a, "title": t, "sequence": n} for a, t, n in (series or [])]}


@pytest.fixture
def series_lists(monkeypatch):
    lists = {}

    async def members(asin, client=None):
        return lists.get(asin, {})

    monkeypatch.setattr(audible, "series_members", members)
    return lists


# ---- series grouping ----------------------------------------------------------------


async def test_abridged_series_merge_and_the_unabridged_edition_wins(series_lists):
    books = [
        book("U1", "The Gates of Rome", [("S1", "Emperor", "1")]),
        book("A1", "The Gates of Rome", [("S2", "Emperor (abridged)", "1")], abridged=True),
        book("A2", "The Death of Kings", [("S2", "Emperor (Abridged)", "2")], abridged=True),
    ]
    tidy = await audible.tidy_series(books)
    assert [b["asin"] for b in tidy] == ["U1", "A2"]  # the abridged book 1 goes; abridged-only book 2 stays
    assert {b["series"][0]["title"] for b in tidy} == {"Emperor"}  # one series, not two


async def test_a_book_the_series_lists_is_put_in_it(series_lists):
    series_lists["S9"] = {"N1": "1", "N2": "2", "N3": "3"}
    books = [book("N1", "Nero"), book("N2", "Tyrant", [("S9", "The Nero Trilogy", "2")]),
             book("N3", "Inferno", [("S9", "The Nero Trilogy", "3")]), book("X1", "Blackwater")]
    tidy = await audible.tidy_series(books)
    by = {b["asin"]: b["series"] for b in tidy}
    assert by["N1"] == [{"asin": "S9", "title": "The Nero Trilogy", "sequence": "1"}]
    assert by["X1"] == []  # a real standalone stays one


# ---- setting a series by hand -----------------------------------------------------------


def library_book(**over):
    b = db.insert({"source": "local", "source_url": "file:///books/nero", "title": "Nero", "author": "Conn Iggulden",
                   "status": "completed", **over})
    return b


def test_edit_details_sets_series_number_and_year(client):
    b = library_book()
    out = client.patch(f"/api/library/{b['id']}", json={"series": "The Nero Trilogy", "series_number": "1", "year": "2024"}).json()
    assert out["series"] == [{"asin": "", "title": "The Nero Trilogy", "sequence": "1"}] and out["released"] == "2024"
    assert client.patch(f"/api/library/{b['id']}", json={"series_number": "one"}).status_code == 400
    assert client.patch(f"/api/library/{b['id']}", json={"year": "24"}).status_code == 400
    cleared = client.patch(f"/api/library/{b['id']}", json={"series": ""}).json()
    assert cleared["series"] == []


def test_renaming_a_series_drops_audibles_id_but_keeping_it_keeps_it(client):
    b = library_book(series=[{"asin": "S9", "title": "The Nero Trilogy", "sequence": "1"}])
    kept = client.patch(f"/api/library/{b['id']}", json={"series": "The Nero Trilogy", "series_number": "1"}).json()
    assert kept["series"][0]["asin"] == "S9"
    renamed = client.patch(f"/api/library/{b['id']}", json={"series": "Nero"}).json()
    assert renamed["series"][0]["asin"] == "" and renamed["series"][0]["sequence"] == "1"


# ---- filling in from Audible ------------------------------------------------------------


@pytest.fixture
def audible_search(monkeypatch):
    catalog = []
    asked = []

    async def search_books(query, page=1, client=None):
        asked.append(query)
        return catalog, 1

    monkeypatch.setattr(audible, "search_books", search_books)
    return catalog, asked


async def test_only_an_exact_match_fills_and_only_what_is_missing(audible_search):
    catalog, _ = audible_search
    catalog += [book("B0OTHER", "Nero", author="Someone Else"),
                {**book("B0NERO1", "Nero", [("S9", "The Nero Trilogy (Unabridged)", "1")]),
                 "narrators": ["Kris Dyer"], "released": "2024-05-23"}]
    b = library_book(narrators=["Already Here"])
    changed = await wanted.fill_from_audible(b)
    got = db.get(b["id"])
    assert set(changed) == {"series", "released", "asin"}
    assert got["series"] == [{"asin": "S9", "title": "The Nero Trilogy", "sequence": "1"}]
    assert got["narrators"] == ["Already Here"] and got["asin"] == "B0NERO1" and got["details_checked"]


async def test_no_exact_match_changes_nothing(audible_search):
    catalog, _ = audible_search
    catalog.append(book("B0OTHER", "Nero: A Novel About Something Else", author="Someone Else"))
    b = library_book()
    assert await wanted.fill_from_audible(b) == []
    assert db.get(b["id"])["series"] == []


def test_fill_details_works_through_the_library_in_batches(client, audible_search):
    catalog, asked = audible_search
    catalog.append({**book("B0NERO1", "Nero", [("S9", "The Nero Trilogy", "1")]), "narrators": ["Kris Dyer"]})
    for i in range(3):
        library_book(source_url=f"file:///books/{i}", title="Nero" if i == 0 else f"Unknown {i}")
    library_book(source_url="file:///books/done", title="Tyrant", narrators=["X"], released="2025",
                 series=[{"asin": "", "title": "The Nero Trilogy", "sequence": "2"}])  # nothing missing
    first = client.post("/api/tidy/fill-details", json={"batch": 2}).json()
    assert (first["checked"], first["remaining"]) == (2, 1)
    second = client.post("/api/tidy/fill-details", json={"batch": 2}).json()
    assert (second["checked"], second["remaining"]) == (1, 0)
    assert first["filled"] + second["filled"] == 1 and len(asked) == 3
    assert client.post("/api/tidy/fill-details", json={}).json()["checked"] == 0  # each book is asked once


def test_audible_match_fills_the_form_without_saving(client, audible_search):
    catalog, _ = audible_search
    catalog.append({**book("B0NERO1", "Nero", [("S9", "The Nero Trilogy", "1")]), "narrators": ["Kris Dyer"]})
    b = library_book()
    m = client.get(f"/api/library/{b['id']}/audible-match").json()
    assert (m["series"], m["series_number"], m["narrators"], m["year"]) == ("The Nero Trilogy", "1", ["Kris Dyer"], "2020")
    assert db.get(b["id"])["series"] == []
    nothing = library_book(source_url="file:///books/x", title="Not On Audible")
    assert client.get(f"/api/library/{nothing['id']}/audible-match").status_code == 404


@pytest.mark.real_auth
def test_listeners_cannot_ask_audible_for_a_books_details(accounts):
    b = library_book()
    assert accounts.post("/api/auth/login", json={"username": "listener", "password": "listen-pass"}).status_code == 200
    assert accounts.get(f"/api/library/{b['id']}/audible-match").status_code == 403
    assert accounts.post("/api/tidy/fill-details", json={}).status_code == 403


# ---- books already in Audiobookshelf -------------------------------------------------------


@pytest.fixture
def author_page(monkeypatch, series_lists):
    async def author(asin, client=None):
        return {"asin": asin, "name": "Conn Iggulden", "image": "", "description": ""}

    async def author_books(asin, name, client=None, pages=4):
        return [book("N1", "Nero", [("S9", "The Nero Trilogy", "1")]), book("N2", "Tyrant", [("S9", "The Nero Trilogy", "2")]),
                book("X1", "Blackwater")]

    monkeypatch.setattr(audible, "author", author)
    monkeypatch.setattr(audible, "author_books", author_books)
    shelf = []

    async def owned(settings):
        return shelf

    monkeypatch.setattr(audiobookshelf, "owned", owned)
    return db.add_follow("author", "B0024JAJII", "Conn Iggulden", "", []), shelf


def test_books_in_audiobookshelf_count_as_yours(client, author_page):
    f, shelf = author_page
    shelf += [{"title": "Nero: Book One of the Nero Trilogy", "author": "Conn Iggulden", "asin": ""},  # by title
              {"title": "Something", "author": "Conn Iggulden", "asin": "N2"}]  # by ASIN
    detail = client.get(f"/api/follows/{f['id']}").json()
    assert {b["asin"]: b["abs"] for b in detail["books"]} == {"N1": True, "N2": True, "X1": False}


def test_add_all_missing_skips_what_audiobookshelf_has(client, author_page, monkeypatch):
    f, shelf = author_page
    shelf.append({"title": "Nero", "author": "Conn Iggulden", "asin": ""})
    monkeypatch.setattr(api, "_auto_search_now", lambda: _noop())
    out = client.post(f"/api/follows/{f['id']}/want-all").json()
    assert out["added"] == 2 and {b["title"] for b in db.list_books()} == {"Tyrant", "Blackwater"}


async def _noop():
    return None


async def test_owned_reads_titles_from_audiobookshelf_and_remembers_them(monkeypatch):
    calls = []

    def abs_server(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/items"):
            return httpx.Response(200, json={"total": 2, "results": [
                {"id": "1", "path": "/a/Nero", "relPath": "Nero", "isMissing": False,
                 "media": {"metadata": {"title": "Nero", "authorName": "Conn Iggulden", "asin": "B0NERO1"}}},
                {"id": "2", "path": "/a/Gone", "relPath": "Gone", "isMissing": True,
                 "media": {"metadata": {"title": "Gone", "authorName": "X", "asin": ""}}}]})
        return httpx.Response(404)

    monkeypatch.setattr(audiobookshelf, "transport", httpx.MockTransport(abs_server))
    monkeypatch.setattr(audiobookshelf, "_owned", {"key": None, "at": 0.0, "books": []})
    config.update({"audiobookshelf_url": "http://abs", "audiobookshelf_token": "k", "audiobookshelf_library_id": "lib"})
    got = await audiobookshelf.owned(config.load())
    assert [(b["title"], b["asin"]) for b in got] == [("Nero", "B0NERO1")]  # a missing item doesn't count
    await audiobookshelf.owned(config.load())
    assert len(calls) == 1  # remembered


async def test_owned_is_empty_without_a_connection():
    assert await audiobookshelf.owned(config.load()) == []


# ---- names that lead with their series --------------------------------------------------------


def test_series_first_names_are_split_only_on_an_explicit_marker():
    assert wanted.series_first("Playing for Keeps #1 - Consider Me") == ("Playing for Keeps", "1", "Consider Me")
    assert wanted.series_first("Mistborn, Book 2 - The Well of Ascension") == ("Mistborn", "2", "The Well of Ascension")
    assert wanted.series_first("Discworld Vol. 3: Equal Rites") == ("Discworld", "3", "Equal Rites")
    assert wanted.series_first("The Expanse #07.5 - Strange Dogs") == ("The Expanse", "7.5", "Strange Dogs")
    for title in ("Catch-22 - A Novel", "1984", "Nero", "Book of Night", "Fahrenheit 451 - Special Edition"):
        assert wanted.series_first(title) is None, title


async def test_a_series_first_name_fills_in_and_loses_its_prefix(audible_search):
    catalog, asked = audible_search
    catalog.append({**book("B0KEEPS1", "Consider Me", [("S7", "Playing for Keeps", "1")], author="Becka Mack"),
                    "narrators": ["Erin Mallon"], "released": "2022-10-01"})
    b = library_book(source_url="file:///books/keeps", title="Playing for Keeps #1 - Consider Me", author="Becka Mack")
    changed = await wanted.fill_from_audible(b)
    got = db.get(b["id"])
    assert set(changed) == {"series", "narrators", "released", "asin", "title"}
    assert got["title"] == "Consider Me" and got["series"][0]["title"] == "Playing for Keeps"
    assert asked[-1] == "Consider Me Becka Mack"


async def test_a_series_first_name_needs_audible_to_agree_on_the_series(audible_search):
    catalog, _ = audible_search
    catalog.append(book("B0OTHER1", "Consider Me", [("S8", "Some Other Series", "1")], author="Becka Mack"))
    b = library_book(source_url="file:///books/keeps", title="Playing for Keeps #1 - Consider Me", author="Becka Mack")
    assert await wanted.fill_from_audible(b) == []
    assert db.get(b["id"])["title"] == "Playing for Keeps #1 - Consider Me"


def test_books_checked_before_get_one_more_look_if_their_name_leads_with_a_series():
    old = wanted.SERIES_FIRST_SINCE - 10
    keeps = library_book(source_url="file:///books/k", title="Playing for Keeps #1 - Consider Me", details_checked=old)
    plain = library_book(source_url="file:///books/p", title="Nero", details_checked=old)
    again = library_book(source_url="file:///books/a", title="Playing for Keeps #2 - Chasing Wishes",
                         details_checked=wanted.SERIES_FIRST_SINCE + 10)
    assert [wanted.details_due(db.get(x["id"])) for x in (keeps, plain, again)] == [True, False, False]


def test_audible_match_offers_the_title_without_its_series(client, audible_search):
    catalog, _ = audible_search
    catalog.append(book("B0KEEPS1", "Consider Me", [("S7", "Playing for Keeps", "1")], author="Becka Mack"))
    b = library_book(source_url="file:///books/keeps", title="Playing for Keeps #1 - Consider Me", author="Becka Mack")
    m = client.get(f"/api/library/{b['id']}/audible-match").json()
    assert (m["series_first"], m["series"], m["series_number"]) == ("Consider Me", "Playing for Keeps", "1")
