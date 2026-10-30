"""Ebooks: finding them on disk, wanting them, filing them next to their audiobooks, and removing them safely."""

import time
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from narratarr import api, audible, audiobookshelf, config, db, ebooks, importer, wanted, worker
from narratarr.sources import openlibrary, prowlarr
from narratarr.worker import Worker

PNG = b"\x89PNG\r\n\x1a\n" + b"cover" * 10
PROWLARR = "http://prowlarr:9696"
DUNE = {
    "asin": "B002V1OF70", "url": "https://www.audible.com/pd/B002V1OF70", "title": "Dune", "subtitle": "",
    "author": "Frank Herbert", "narrators": ["Scott Brick"], "cover_url": "https://img.example/dune.jpg",
    "description": "Arrakis.", "released": "2007-01-01", "length": "21 hours", "language": "English",
    "series": [], "abridged": False,
}


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(worker, "POLL_SECONDS", 0)
    prowlarr._releases.clear()
    prowlarr._search_cache.clear()
    openlibrary._cache.clear()


@pytest.fixture
def client():
    with TestClient(api.app) as c:
        c.app.state.worker.poke = lambda: None  # tests run the worker themselves
        yield c


def make_epub(path: Path, title="Dune", author="Frank Herbert", cover=PNG, epub3=False, cover_type="image/png") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    cover_ref = '<meta name="cover" content="cov"/>' if not epub3 else ""
    props = ' properties="cover-image"' if epub3 else ""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", (
            '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>"))
        zf.writestr("OEBPS/content.opf", (
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>"
            "<dc:description>&lt;p&gt;Spice &amp;amp; sand.&lt;/p&gt;</dc:description><dc:language>en</dc:language>"
            f"{cover_ref}</metadata><manifest>"
            f'<item id="cov" href="images/cover.png" media-type="{cover_type}"{props}/>'
            "</manifest></package>"))
        if cover:
            zf.writestr("OEBPS/images/cover.png", cover)
    return path


def wait_done(book_id):
    for _ in range(100):
        b = db.get(book_id)
        if b["status"] in ("completed", "failed"):
            return b
        time.sleep(0.05)
    return db.get(book_id)


# ---- files -------------------------------------------------------------------------------------


def test_the_best_format_is_kept(tmp_path):
    files = []
    for name, size in [("Dune.pdf", 900), ("Dune.mobi", 500), ("Dune.epub", 10), ("Dune sample.epub", 5)]:
        (tmp_path / name).write_bytes(b"x" * size)
        files.append(tmp_path / name)
    assert ebooks.best(files).name == "Dune.epub"  # EPUB first; the bigger one, not the sample
    assert ebooks.best([tmp_path / "Dune.pdf", tmp_path / "Dune.mobi"]).name == "Dune.mobi"
    assert ebooks.best([]) is None
    (tmp_path / "._Dune.epub").write_bytes(b"mac junk")
    assert tmp_path / "._Dune.epub" not in ebooks.ebook_files(tmp_path)


def test_epub_details_and_cover(tmp_path):
    book = make_epub(tmp_path / "dune.epub")
    meta = ebooks.epub_meta(book)
    assert (meta["title"], meta["author"], meta["language"]) == ("Dune", "Frank Herbert", "en")
    assert meta["description"] == "Spice & sand."  # markup removed
    assert ebooks.epub_cover(book) == (PNG, "image/png")
    assert ebooks.epub_cover(make_epub(tmp_path / "three.epub", epub3=True)) == (PNG, "image/png")

    broken = tmp_path / "broken.epub"
    broken.write_bytes(b"not a zip")
    assert ebooks.epub_meta(broken)["title"] == "" and ebooks.epub_cover(broken) is None


def test_scan_finds_ebooks_but_leaves_an_audiobooks_own_alone(tmp_path):
    (tmp_path / "Frank Herbert/Dune").mkdir(parents=True)
    (tmp_path / "Frank Herbert/Dune/01.mp3").write_bytes(b"audio")
    make_epub(tmp_path / "Frank Herbert/Dune/Dune.epub")  # comes along with the audiobook
    (tmp_path / "Ebooks").mkdir()
    (tmp_path / "Ebooks/Andy Weir - Artemis.mobi").write_bytes(b"m" * 50)
    (tmp_path / "Ebooks/Andy Weir - Artemis.epub").write_bytes(b"e" * 10)
    (tmp_path / "Ursula K. Le Guin").mkdir()
    (tmp_path / "Ursula K. Le Guin/A Wizard of Earthsea.pdf").write_bytes(b"pdf")
    make_epub(tmp_path / "Inbox/download-1234.epub", title="Hyperion", author="Dan Simmons")

    found = {Path(f["path"]).name: f for f in importer.scan(tmp_path)}
    assert set(found) == {"Dune", "Andy Weir - Artemis.epub", "A Wizard of Earthsea.pdf", "download-1234.epub"}
    assert found["Dune"]["kind"] == "audiobook"
    artemis = found["Andy Weir - Artemis.epub"]
    assert (artemis["kind"], artemis["title"], artemis["author"], artemis["format"]) == ("ebook", "Artemis", "Andy Weir", "EPUB")
    earthsea = found["A Wizard of Earthsea.pdf"]
    assert (earthsea["title"], earthsea["author"]) == ("A Wizard of Earthsea", "Ursula K. Le Guin")
    hyperion = found["download-1234.epub"]
    assert (hyperion["title"], hyperion["author"]) == ("Hyperion", "Dan Simmons")  # from inside the EPUB


# ---- filing ------------------------------------------------------------------------------------------


def library() -> Path:
    return Path(config.load().library_dir).resolve()


def audiobook_in_library(title="Dune", author="Frank Herbert") -> dict:
    folder = library() / author / title
    folder.mkdir(parents=True)
    (folder / "01.mp3").write_bytes(b"audio")
    (folder / "cover.jpg").write_bytes(b"jpg")
    (folder / "desc.txt").write_text("Arrakis.")
    return db.insert({"source": "local", "source_url": f"local:{title}", "title": title, "author": author,
                      "status": "completed", "path": str(folder), "kind": "audiobook"})


def queue_import(src: Path, title: str, author: str, kind: str) -> dict:
    return db.insert({"source": "local", "source_url": importer.url_for(src), "title": title, "author": author,
                      "status": "queued", "import_mode": "copy", "kind": kind})


async def test_an_ebook_joins_its_audiobook_and_both_can_be_removed_safely(client, isolated_data):
    audio = audiobook_in_library()
    src = make_epub(isolated_data / "downloads" / "dune-retail.epub")
    ebook = queue_import(src, "Dune", "Frank Herbert", "ebook")
    await Worker().process(ebook["id"])

    ebook = db.get(ebook["id"])
    folder = Path(audio["path"])
    assert ebook["status"] == "completed", ebook["error"]
    assert Path(ebook["path"]) == folder / "Dune.epub" and Path(ebook["path"]).is_file()
    assert (folder / "cover.jpg").read_bytes() == b"jpg"  # the audiobook's cover stays

    pairs = {b["id"]: b["companion_id"] for b in client.get("/api/library").json()}
    assert pairs == {audio["id"]: ebook["id"], ebook["id"]: audio["id"]}

    # Removing the audiobook with its files leaves the ebook, and what it shows with.
    client.delete(f"/api/library/{audio['id']}", params={"delete_files": True})
    assert not (folder / "01.mp3").exists()
    assert sorted(p.name for p in folder.iterdir()) == ["Dune.epub", "cover.jpg", "desc.txt"]

    # Removing the ebook too takes the folder with it, now nothing else is there.
    client.delete(f"/api/library/{ebook['id']}", params={"delete_files": True})
    assert not folder.exists()


async def test_an_audiobook_joins_the_folder_its_ebook_made(client, isolated_data):
    src = make_epub(isolated_data / "downloads" / "Andy Weir - Artemis.epub", title="Artemis", author="Andy Weir")
    ebook = queue_import(src, "Artemis", "Andy Weir", "ebook")
    await Worker().process(ebook["id"])
    ebook = db.get(ebook["id"])
    folder = library() / "Andy Weir" / "Artemis"
    assert Path(ebook["path"]) == folder / "Artemis.epub"
    assert (folder / "cover.png").read_bytes() == PNG  # taken from inside the EPUB

    audio_src = isolated_data / "downloads" / "Artemis audio"
    audio_src.mkdir()
    (audio_src / "01.m4b").write_bytes(b"audio")
    audio = queue_import(audio_src, "Artemis", "Andy Weir", "audiobook")
    await Worker().process(audio["id"])
    audio = db.get(audio["id"])
    assert Path(audio["path"]) == folder  # together, not "Artemis (2)"
    assert {p.name for p in folder.iterdir()} >= {"01.m4b", "Artemis.epub"}

    # Removing the ebook with its files leaves the audiobook alone.
    client.delete(f"/api/library/{ebook['id']}", params={"delete_files": True})
    assert (folder / "01.m4b").exists() and not (folder / "Artemis.epub").exists()


async def test_an_ebook_never_moves_into_another_books_folder(isolated_data):
    other = audiobook_in_library("Dune", "Frank Herbert")
    db.update(other["id"], title="Dune Messiah")  # a different book now owns the folder "Dune" would get
    src = make_epub(isolated_data / "downloads" / "dune.epub")
    ebook = queue_import(src, "Dune", "Frank Herbert", "ebook")
    await Worker().process(ebook["id"])
    path = Path(db.get(ebook["id"])["path"])
    assert path.parent.name == "Dune (2)" and not (Path(other["path"]) / "Dune.epub").exists()


async def test_downloading_an_ebook_again_replaces_its_file(isolated_data):
    book = db.insert({"source": "prowlarr", "source_url": "https://idx.example/1", "title": "Dune",
                      "author": "Frank Herbert", "status": "organizing", "kind": "ebook"})
    w, settings = Worker(), config.load()
    tmp = worker.tmp_dir(book, settings)
    tmp.mkdir(parents=True)
    (tmp / "Dune.mobi").write_bytes(b"mobi")
    first = await w._organize(db.get(book["id"]), tmp, settings)
    db.update(book["id"], path=str(first), status="completed")

    tmp.mkdir(parents=True)
    (tmp / "nested").mkdir()
    (tmp / "nested" / "Dune.epub").write_bytes(b"epub")
    (tmp / "Dune.pdf").write_bytes(b"pdf")
    second = await w._organize(db.get(book["id"]), tmp, settings)
    assert second.name == "Dune.epub" and second.parent == first.parent
    assert sorted(p.name for p in second.parent.iterdir() if p.suffix != ".txt") == ["Dune.epub"]

    empty = worker.tmp_dir(book, settings)
    empty.mkdir(parents=True)
    (empty / "Dune.m4b").write_bytes(b"audio")
    with pytest.raises(ValueError, match="no ebook"):
        await w._organize(db.get(book["id"]), empty, settings)


def test_audiobookshelf_counts_a_shared_folder_once():
    books = [{"title": "Dune", "path": "/books/Frank Herbert/Dune", "kind": "audiobook"},
             {"title": "Dune", "path": "/books/Frank Herbert/Dune/Dune.epub", "kind": "ebook"},
             {"title": "Artemis", "path": "/books/Andy Weir/Artemis/Artemis.epub", "kind": "ebook"}]
    items = [{"path": "/audiobooks/Frank Herbert/Dune", "rel": "Frank Herbert/Dune", "missing": False},
             {"path": "/audiobooks/Andy Weir/Artemis", "rel": "Andy Weir/Artemis", "missing": False}]
    assert audiobookshelf.compare(books, "/books", items) == {"books": 2, "seen": 2, "missing": [], "seen_at": "/audiobooks"}


# ---- the web UI's API ----------------------------------------------------------------------------------


def test_downloading_the_ebook_file(client, isolated_data):
    path = make_epub(library() / "Frank Herbert" / "Dune" / "Dune.epub")
    ebook = db.insert({"source": "local", "source_url": "local:dune.epub", "title": "Dune: Deluxe/Edition",
                       "author": "Frank Herbert", "status": "completed", "path": str(path), "kind": "ebook"})
    r = client.get(f"/api/library/{ebook['id']}/ebook")
    assert r.status_code == 200 and r.content == path.read_bytes()
    assert r.headers["content-type"] == "application/epub+zip" and r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-disposition"].startswith("attachment") and "DeluxeEdition.epub" in r.headers["content-disposition"]
    assert client.get(f"/api/library/{ebook['id']}/cover").content == PNG  # from inside the EPUB

    audio = audiobook_in_library("Hyperion", "Dan Simmons")
    assert client.get(f"/api/library/{audio['id']}/ebook").status_code == 404
    db.update(ebook["id"], status="wanted")
    assert client.get(f"/api/library/{ebook['id']}/ebook").status_code == 404


def test_wanting_a_catalog_book_in_both_formats(client, monkeypatch):
    async def book(asin, client=None):
        return DUNE if asin == DUNE["asin"] else None

    monkeypatch.setattr(audible, "book", book)
    first = client.post("/api/library", json={"url": DUNE["url"], "source": "audible", "format": "both", "download": False})
    assert first.status_code == 200 and first.json()["kind"] == "audiobook"
    rows = {b["kind"]: b for b in db.list_books()}
    assert set(rows) == {"audiobook", "ebook"}
    assert rows["ebook"]["source_url"] == DUNE["url"] + "#ebook" and rows["ebook"]["narrators"] == []
    assert rows["ebook"]["length"] == ""  # an audiobook's runtime isn't the ebook's length
    assert rows["audiobook"]["length"] == "21 hours"

    again = client.post("/api/library", json={"url": DUNE["url"], "source": "audible", "format": "ebook", "download": False})
    assert again.json()["id"] == rows["ebook"]["id"] and len(db.list_books()) == 2
    found = client.get("/api/details", params={"url": DUNE["url"], "source": "audible"}).json()
    assert found["library"]["id"] == rows["audiobook"]["id"]
    bad = client.post("/api/library", json={"url": DUNE["url"], "source": "audible", "format": "vinyl"})
    assert bad.status_code == 422


def test_wanting_the_other_format_of_a_library_book(client):
    audio = audiobook_in_library("The Hobbit", "J.R.R. Tolkien")
    other = client.post(f"/api/library/{audio['id']}/other-format").json()
    assert other["kind"] == "ebook" and other["status"] == "wanted" and other["source"] == "openlibrary"
    assert other["source_url"].startswith("https://openlibrary.org/search?") and other["source_url"].endswith("#ebook")
    assert client.post(f"/api/library/{audio['id']}/other-format").json()["id"] == other["id"]  # once
    assert client.post(f"/api/library/{other['id']}/other-format").json()["id"] == audio["id"]
    assert len(db.list_books()) == 2

    catalog = db.insert({"source": "audible", "source_url": DUNE["url"], "asin": DUNE["asin"], "title": "Dune",
                         "author": "Frank Herbert", "status": "wanted", "narrators": ["Scott Brick"]})
    ebook = client.post(f"/api/library/{catalog['id']}/other-format").json()
    assert (ebook["source"], ebook["source_url"], ebook["asin"]) == ("audible", DUNE["url"] + "#ebook", DUNE["asin"])


def test_importing_ebooks(client, isolated_data):
    src = isolated_data / "old-books"
    make_epub(src / "Frank Herbert - Dune.epub")
    scan = client.post("/api/import/scan", json={"folder": str(src)}).json()["books"]
    assert [(b["kind"], b["title"], b["in_library"]) for b in scan] == [("ebook", "Dune", False)]

    # An ebook is one file, so it can be left where it is (a loose audio file can't).
    r = client.post("/api/import", json={"folder": str(src), "mode": "inplace", "books": [{"path": scan[0]["path"]}]})
    assert r.json()["added"] == 1
    (b,) = db.list_books()
    assert (b["kind"], b["status"], b["path"]) == ("ebook", "completed", scan[0]["path"])
    assert client.post("/api/import/scan", json={"folder": str(src)}).json()["books"][0]["in_library"]


# ---- wanted ------------------------------------------------------------------------------------------


@pytest.fixture
def indexers(monkeypatch):
    """A fake Prowlarr that records which categories it was asked to search."""
    seen, releases = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "prowlarr":
            seen.append(request.url.params.get_list("categories"))
            return httpx.Response(200, json=releases)
        if "archive.org" in request.url.host:
            seen.append("librivox")
            return httpx.Response(200, json={"response": {"numFound": 0, "docs": []}})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real(*a, **{**kw, "transport": transport}))
    config.update({"prowlarr_url": PROWLARR, "prowlarr_api_key": "K", "sabnzbd_url": "http://sab", "sabnzbd_api_key": "S"})
    return seen, releases


async def test_wanted_ebooks_search_the_ebook_categories(indexers):
    seen, releases = indexers
    releases += [
        {"guid": "https://n.example/1", "title": "Frank Herbert - Dune (2006) [M4B]", "protocol": "usenet", "indexer": "N",
         "downloadUrl": f"{PROWLARR}/1/download?link=1", "size": 1},
        {"guid": "https://n.example/2", "title": "Frank Herbert - Dune (retail) [EPUB]", "protocol": "usenet",
         "indexer": "N", "downloadUrl": f"{PROWLARR}/1/download?link=2", "size": 1},
        {"guid": "https://n.example/3", "title": "Frank Herbert - Dune [MOBI]", "protocol": "usenet", "indexer": "N",
         "downloadUrl": f"{PROWLARR}/1/download?link=3", "size": 1},
    ]
    audio = wanted.add_wanted(DUNE)
    ebook = wanted.add_wanted(DUNE, "ebook")
    assert audio and ebook and wanted.add_wanted(DUNE, "ebook") is None

    found = await wanted.find_releases(ebook)
    assert seen == [["7020"]]  # ebook categories only, and no LibriVox
    assert [f["format"] for f in found] == ["EPUB", "MOBI"]  # the audiobook release isn't offered; EPUB first

    seen.clear()
    assert [f["format"] for f in await wanted.find_releases(audio)] == ["M4B"]
    assert sorted(map(str, seen)) == ["['3030']", "librivox"]

    assert await wanted.auto_search() == 2
    got = db.get(ebook["id"])
    assert (got["status"], got["kind"], got["format"], got["release_url"]) == ("queued", "ebook", "EPUB", "https://n.example/2")
    assert db.get(audio["id"])["format"] == "M4B"


async def test_open_library_as_a_catalog():
    work = {"title": "Project Hail Mary", "key": "/works/OL21745884W", "covers": [11200092],
            "description": {"type": "/type/text", "value": "Ryland Grace wakes up.\r\n\r\nHe's alone.\n\n----------\nSee also [The Martian][1]\n\n[1]: https://openlibrary.org/works/OL17091839W"},
            "subjects": ["Science fiction", "nyt:hardcover-fiction=2021-05-23"]}
    doc = {"key": "/works/OL21745884W", "title": "Project Hail Mary", "author_name": ["Andy Weir"],
           "cover_i": 11200092, "first_publish_year": 2021, "number_of_pages_median": 496}

    def handler(request):
        assert request.headers["user-agent"].startswith("Narratarr/")
        if request.url.path == "/works/OL21745884W.json":
            return httpx.Response(200, json=work)
        if request.url.path == "/search.json":
            knockoff = {**doc, "key": "/works/OL1W", "title": "Summary of Project Hail Mary"}
            return httpx.Response(200, json={"numFound": 2, "docs": [doc, knockoff, {"key": "/works/OL2W", "title": "No author"}]})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        ol = openlibrary.OpenLibrary(client=c)
        page = await ol.search("project hail mary")
        (item,) = page.items  # no summaries of other people's books, nothing without an author
        assert (item.url, item.author, item.posted, item.length) == (
            "https://openlibrary.org/works/OL21745884W", "Andy Weir", "2021", "496 pages")
        assert item.cover_url == "https://covers.openlibrary.org/b/id/11200092-L.jpg"
        d = await ol.details(item.url + "#ebook")
    assert d.description == "Ryland Grace wakes up.\n\nHe's alone." and d.categories == ["Science fiction"]
    assert ol.owns("https://openlibrary.org/works/OL21745884W") and not ol.owns("https://evil.example/works/OL1W")


# ---- never at the cost of someone else's files ------------------------------------------------------


def catalog_row(title, author, kind):
    return db.insert({"source": "audible", "source_url": f"https://www.audible.com/pd/{title}-{kind}", "title": title,
                      "author": author, "status": "organizing", "kind": kind})


async def organize(book, files):
    settings = config.load()
    tmp = worker.tmp_dir(book, settings)
    tmp.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (tmp / name).write_bytes(data)
    dest = await Worker()._organize(db.get(book["id"]), tmp, settings)
    db.update(book["id"], status="completed", path=str(dest))
    return dest


async def test_a_folder_of_the_users_own_files_is_never_shared(client):
    mine = library() / "Frank Herbert" / "Dune"
    mine.mkdir(parents=True)
    (mine / "my-notes.txt").write_text("not Narratarr's")
    ebook = await organize(catalog_row("Dune", "Frank Herbert", "ebook"), {"Dune.epub": b"PK"})
    assert ebook.parent.name == "Dune (2)"  # not in with the notes
    audio = catalog_row("Dune", "Frank Herbert", "audiobook")
    folder = await organize(audio, {"01.mp3": b"ID3", "notes.pdf": b"%PDF"})
    assert folder == ebook.parent  # the ebook's own folder: the audiobook joins it

    (folder / ".DS_Store").write_bytes(b"junk")
    client.delete(f"/api/library/{audio['id']}", params={"delete_files": True})
    assert (mine / "my-notes.txt").exists() and ebook.exists()
    assert sorted(p.name for p in folder.iterdir()) == ["Dune.epub"]  # all of the audiobook went, the ebook stayed


async def test_an_audiobook_never_joins_a_folder_imported_in_place(client):
    author = library() / "Frank Herbert"
    author.mkdir(parents=True)
    (author / "Dune.epub").write_bytes(b"PK")
    (author / "Dune.mobi").write_bytes(b"x")
    (author / "Heretics of Dune.rtf").write_text("x")
    for f in importer.scan(library()):
        db.insert(importer.as_book(f, "inplace", "completed"))
    audio = catalog_row("Dune", "Frank Herbert", "audiobook")
    folder = await organize(audio, {"01.mp3": b"ID3"})
    assert folder == author / "Dune"  # its own folder, not the author folder the ebook sits in
    client.delete(f"/api/library/{audio['id']}", params={"delete_files": True})
    assert sorted(p.name for p in author.iterdir()) == ["Dune.epub", "Dune.mobi", "Heretics of Dune.rtf"]


async def test_different_books_with_the_same_series_name_stay_apart():
    thrawn = await organize(catalog_row("Star Wars: Thrawn", "Timothy Zahn", "audiobook"), {"01.mp3": b"ID3"})
    alliances = catalog_row("Star Wars: Thrawn: Alliances", "Timothy Zahn", "ebook")
    assert wanted.companion(db.get(alliances["id"])) is None
    path = await organize(alliances, {"x.epub": b"PK"})
    assert path.parent != thrawn
    # The same ASIN is the same book, whatever the titles say; different ASINs never are.
    a = {"asin": "B0001", "title": "Dune", "author": "Frank Herbert"}
    assert wanted.same_book(a, {**a, "title": "Dune (Unabridged)"})
    assert not wanted.same_book(a, {**a, "asin": "B0002"})


async def test_an_ebook_is_never_filed_inside_another_books_folder():
    other = library() / "Frank Herbert"  # audio straight in the author folder, from an older "{author}" template
    other.mkdir(parents=True)
    (other / "01.mp3").write_bytes(b"ID3")
    db.insert({"source": "local", "source_url": "local:stories", "title": "Collected Stories",
               "author": "Frank Herbert", "status": "completed", "path": str(other)})
    path = await organize(catalog_row("Dune", "Frank Herbert", "ebook"), {"Dune.epub": b"PK"})
    assert path == library() / "Frank Herbert - Dune" / "Dune.epub"  # beside it, not in it


async def test_system_clutter_doesnt_keep_the_formats_apart(client):
    ebook = await organize(catalog_row("Dune", "Frank Herbert", "ebook"), {"Dune.epub": b"PK"})
    (ebook.parent / ".DS_Store").write_bytes(b"junk")
    (ebook.parent / "@eaDir").mkdir()
    audio = await organize(catalog_row("Dune", "Frank Herbert", "audiobook"), {"01.mp3": b"ID3"})
    assert audio == ebook.parent


async def test_removing_an_audiobook_whose_ebook_file_went_missing(client):
    audio = audiobook_in_library()
    folder = Path(audio["path"])
    db.insert({"source": "local", "source_url": "local:dune.epub", "title": "Dune", "author": "Frank Herbert",
               "status": "completed", "path": str(folder / "Dune.epub"), "kind": "ebook"})  # gone from disk
    client.delete(f"/api/library/{audio['id']}", params={"delete_files": True})
    assert sorted(p.name for p in folder.iterdir()) == ["cover.jpg", "desc.txt"]


def test_same_book_titles():
    assert wanted.in_library("", "Dune (Unabridged)", "Frank Herbert", [{"title": "Dune", "author": "Frank Herbert"}])
    assert wanted.in_library("", "Dune: Deluxe Edition", "Frank Herbert", [{"title": "Dune", "author": "Frank Herbert"}])
    assert not wanted.in_library("", "Star Wars: Thrawn: Alliances", "Timothy Zahn",
                                 [{"title": "Star Wars: Thrawn", "author": "Timothy Zahn"}])
    assert not wanted.in_library("", "Dune", "Brian Herbert", [{"title": "Dune", "author": "Frank Herbert"}])


def test_audiobook_releases_with_a_companion_pdf_are_still_audiobooks():
    for name in ("Frank Herbert - Dune (PDF + M4B)", "Frank.Herbert-Dune.epub+mp3"):
        d = prowlarr.to_details({"title": name, "protocol": "usenet", "guid": "g", "downloadUrl": "http://x/1"})
        assert d.format in ("M4B", "MP3") and wanted.release_kind(d.to_dict()) == "audiobook"
    epub = prowlarr.to_details({"title": "Frank Herbert - Dune [EPUB]", "protocol": "usenet", "guid": "h"})
    assert wanted.release_kind(epub.to_dict()) == "ebook"
    opus = prowlarr.to_details({"title": "Jim Butcher - Magnum Opus [EPUB]", "protocol": "usenet", "guid": "i"})
    assert opus.format == "EPUB" and wanted.release_kind(opus.to_dict()) == "ebook"  # a title word, not a format


def test_covers_are_only_ever_plain_images(client, tmp_path):
    folder = library() / "Evil" / "Book"
    path = make_epub(folder / "Book.epub", cover=b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
                     cover_type="image/svg+xml")
    ebook = db.insert({"source": "local", "source_url": "local:evil", "title": "Book", "author": "Evil",
                       "status": "completed", "path": str(path), "kind": "ebook"})
    assert client.get(f"/api/library/{ebook['id']}/cover").status_code == 404
    (folder / "cover.svg").write_text("<svg/>")
    assert client.get(f"/api/library/{ebook['id']}/cover").status_code == 404
    (folder / "cover.png").write_bytes(PNG)
    r = client.get(f"/api/library/{ebook['id']}/cover")
    assert r.content == PNG and r.headers["content-type"] == "image/png" and r.headers["x-content-type-options"] == "nosniff"


def test_wanting_a_format_you_have_doesnt_search_again(client, monkeypatch):
    searched = []
    monkeypatch.setattr(api, "_search_in_background", lambda book_id: searched.append(book_id) or _noop())
    audio = audiobook_in_library("The Hobbit", "J.R.R. Tolkien")
    other = client.post(f"/api/library/{audio['id']}/other-format").json()
    assert searched == [other["id"]]
    db.update(other["id"], status="completed")
    client.post(f"/api/library/{audio['id']}/other-format")
    assert searched == [other["id"]] and db.get(other["id"])["status"] == "completed"


async def _noop():
    return None


# ---- reading in Narratarr ---------------------------------------------------------------------------


def make_readable_epub(path: Path) -> Path:
    """An EPUB 3 with a nav document, two chapters (one with a hostile script), a picture and a stylesheet."""
    path.parent.mkdir(parents=True, exist_ok=True)
    chapter = ('<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>{t}</title>'
               '<link rel="stylesheet" href="../styles/book.css"/></head><body><h1 id="{id}">{t}</h1>{extra}</body></html>')
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", (
            '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OPS/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'))
        zf.writestr("OPS/package.opf", (
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Dune</dc:title></metadata><manifest>'
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
            '<item id="c1" href="text/one%20two.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="c2" href="text/two.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="css" href="styles/book.css" media-type="text/css"/>'
            '<item id="pic" href="images/map.png" media-type="image/png"/>'
            '<item id="js" href="scripts/evil.js" media-type="application/javascript"/>'
            '</manifest><spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>'))
        zf.writestr("OPS/nav.xhtml", (
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>'
            '<nav epub:type="landmarks"><ol><li><a href="text/two.xhtml">Not the contents</a></li></ol></nav>'
            '<nav epub:type="toc"><ol><li><a href="text/one%20two.xhtml#start">Book One&nbsp;&amp; Arrakis</a>'
            '<ol><li><a href="text/two.xhtml#muaddib">Muad&#8217;Dib</a></li></ol></li></ol></nav></body></html>'))
        zf.writestr("OPS/text/one two.xhtml", chapter.format(t="Book One", id="start", extra=""))
        zf.writestr("OPS/text/two.xhtml", chapter.format(t="Muad'Dib", id="muaddib", extra="<script>steal()</script>"))
        zf.writestr("OPS/styles/book.css", "h1 { color: red }")
        zf.writestr("OPS/images/map.png", PNG)
        zf.writestr("OPS/scripts/evil.js", "steal()")
    return path


def readable(client, name="Dune.epub", maker=make_readable_epub) -> dict:
    path = maker(library() / "Frank Herbert" / "Dune" / name) if maker else library() / "Frank Herbert" / "Dune" / name
    return db.insert({"source": "local", "source_url": f"local:{name}", "title": "Dune", "author": "Frank Herbert",
                      "status": "completed", "path": str(path), "kind": "ebook"})


def test_the_reader_gets_the_reading_order_and_contents(client):
    book = readable(client)
    r = client.get(f"/api/library/{book['id']}/read").json()
    assert r["title"] == "Dune" and not r["rtl"]
    assert [s["href"] for s in r["spine"]] == ["OPS/text/one two.xhtml", "OPS/text/two.xhtml"]
    assert all(s["size"] > 0 for s in r["spine"])
    assert r["toc"] == [  # only the table of contents, not the landmarks; entities decoded; nesting kept
        {"title": "Book One & Arrakis", "href": "OPS/text/one two.xhtml", "fragment": "start", "depth": 0},
        {"title": "Muad’Dib", "href": "OPS/text/two.xhtml", "fragment": "muaddib", "depth": 1},
    ]


def test_book_pages_are_locked_down(client):
    book = readable(client)
    r = client.get(f"/api/library/{book['id']}/read/OPS/text/two.xhtml")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xhtml+xml")
    csp = r.headers["content-security-policy"]
    for rule in ("script-src 'none'", "default-src 'none'", "sandbox allow-same-origin", "frame-ancestors 'self'", "form-action 'none'"):
        assert rule in csp
    assert "allow-scripts" not in csp and r.headers["x-content-type-options"] == "nosniff"
    assert client.get(f"/api/library/{book['id']}/read/OPS/text/one%20two.xhtml").status_code == 200
    assert client.get(f"/api/library/{book['id']}/read/OPS/images/map.png").headers["content-type"] == "image/png"
    assert client.get(f"/api/library/{book['id']}/read/OPS/styles/book.css").headers["content-type"].startswith("text/css")
    js = client.get(f"/api/library/{book['id']}/read/OPS/scripts/evil.js")  # never served as something to run
    assert js.headers["content-type"] == "application/octet-stream" and js.headers["content-disposition"] == "attachment"
    for nope in ("OPS/missing.xhtml", "../../../../etc/passwd", "OPS/", "META-INF/../../library.db"):
        assert client.get(f"/api/library/{book['id']}/read/{nope}").status_code == 404


def test_only_epubs_open_in_the_reader(client, isolated_data):
    folder = library() / "Frank Herbert" / "Dune"
    folder.mkdir(parents=True)
    (folder / "Dune.mobi").write_bytes(b"BOOKMOBI")
    mobi = readable(client, "Dune.mobi", maker=None)
    assert client.get(f"/api/library/{mobi['id']}/read").status_code == 415
    assert client.get(f"/api/library/{mobi['id']}/read/anything").status_code == 404
    assert client.get(f"/api/library/{mobi['id']}/ebook", params={"inline": True}).headers["content-disposition"].startswith("attachment")

    (folder / "Dune.pdf").write_bytes(b"%PDF-1.4")
    pdf = db.insert({"source": "local", "source_url": "local:pdf", "title": "Dune", "author": "Frank Herbert",
                     "status": "completed", "path": str(folder / "Dune.pdf"), "kind": "ebook"})
    shown = client.get(f"/api/library/{pdf['id']}/ebook", params={"inline": True})
    assert shown.headers["content-disposition"].startswith("inline") and shown.headers["content-type"] == "application/pdf"

    (folder / "Broken.epub").write_bytes(b"not a zip")
    broken = readable(client, "Broken.epub", maker=None)
    assert client.get(f"/api/library/{broken['id']}/read").status_code == 422
    audio = audiobook_in_library("Hyperion", "Dan Simmons")
    assert client.get(f"/api/library/{audio['id']}/read").status_code == 404


def test_epub2_contents_come_from_the_ncx(tmp_path):
    path = tmp_path / "old.epub"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/container.xml", (
            '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'))
        zf.writestr("content.opf", (
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0"><metadata/><manifest>'
            '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
            '<item id="a" href="a.html" media-type="application/xhtml+xml"/></manifest>'
            '<spine toc="ncx" page-progression-direction="rtl"><itemref idref="a"/><itemref idref="gone"/></spine></package>'))
        zf.writestr("toc.ncx", (
            '<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>'
            '<navPoint><navLabel><text>One</text></navLabel><content src="a.html#x"/>'
            '<navPoint><navLabel><text>One, part two</text></navLabel><content src="a.html#y"/></navPoint></navPoint>'
            '<navPoint><navLabel><text>Elsewhere</text></navLabel><content src="missing.html"/></navPoint></navMap></ncx>'))
        zf.writestr("a.html", "<html><body><p id='x'>x</p><p id='y'>y</p></body></html>")
    index = ebooks.epub_index(path)
    assert index["rtl"] and [s["href"] for s in index["spine"]] == ["a.html"]
    assert [(t["title"], t["fragment"], t["depth"]) for t in index["toc"]] == [("One", "x", 0), ("One, part two", "y", 1)]


# ---- ebooks on the Prowlarr tab, and which indexers have them -------------------------------------------

INDEXERS = [
    {"name": "1337x", "enable": True, "capabilities": {"categories": [
        {"id": 3000, "name": "Audio", "subCategories": [{"id": 3030}]}, {"id": 7000, "name": "Books", "subCategories": [{"id": 7020}]}]}},
    {"name": "AudioOnly", "enable": True, "capabilities": {"categories": [{"id": 3000, "subCategories": [{"id": 3030}]}]}},
    {"name": "Switched off", "enable": False, "capabilities": {"categories": [{"id": 7000, "subCategories": [{"id": 7020}]}]}},
]


@pytest.fixture
def prowlarr_api(monkeypatch):
    """A fake Prowlarr with a list of indexers and search results; records which categories were searched."""
    state = {"indexers": list(INDEXERS), "releases": [], "searched": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "prowlarr":
            return httpx.Response(404)
        if request.url.path == "/api/v1/indexer":
            return httpx.Response(200, json=state["indexers"])
        if request.url.path == "/api/v1/system/status":
            return httpx.Response(200, json={"version": "1.30.2"})
        if request.url.path == "/api/v1/search":
            state["searched"].append(request.url.params.get_list("categories"))
            return httpx.Response(200, json=state["releases"])
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real(*a, **{**kw, "transport": transport}))
    config.update({"prowlarr_url": PROWLARR, "prowlarr_api_key": "K", "smart_search": False,
                   "qbittorrent_url": "http://qbt", "torrent_client": "qbittorrent"})
    prowlarr._offering.clear()
    return state


def test_settings_says_which_indexers_have_ebooks(client, prowlarr_api):
    r = client.post("/api/settings/test-prowlarr").json()
    assert (r["version"], r["indexers"], r["ebook_indexers"], r["ebook_categories"]) == ("1.30.2", 2, ["1337x"], ["7020"])
    prowlarr_api["indexers"] = INDEXERS[1:]
    assert client.post("/api/settings/test-prowlarr").json()["ebook_indexers"] == []
    assert prowlarr.offers(INDEXERS[0], ["7000"]) and not prowlarr.offers(INDEXERS[1], ["7020"])


def test_the_prowlarr_tab_searches_ebooks(client, prowlarr_api):
    prowlarr_api["releases"] = [{"guid": "https://1337x.example/t/1", "title": "Frank Herbert - Dune [EPUB]",
                                 "protocol": "torrent", "indexer": "1337x", "seeders": 40,
                                 "magnetUrl": "magnet:?xt=urn:btih:" + "a" * 40}]
    r = client.get("/api/discover", params={"q": "dune", "source": "prowlarr", "kind": "ebook"}).json()
    assert prowlarr_api["searched"] == [["7020"]]
    assert [(i["format"], i["indexer"]) for i in r["items"]] == [("EPUB", "1337x")] and "note" not in r
    client.get("/api/discover", params={"q": "dune", "source": "prowlarr"})
    assert prowlarr_api["searched"][-1] == ["3030"]  # audiobooks, as before

    # Nothing found, and no indexer has ebooks at all: say so, instead of "nothing found".
    prowlarr_api["releases"], prowlarr_api["indexers"] = [], INDEXERS[1:]
    empty = client.get("/api/discover", params={"q": "hyperion", "source": "prowlarr", "kind": "ebook"}).json()
    assert empty["items"] == [] and "None of your Prowlarr indexers offer ebooks (category 7020)" in empty["note"]


def test_releases_from_the_ebook_search_join_the_library_as_ebooks(client, prowlarr_api):
    prowlarr_api["releases"] = [
        {"guid": "https://1337x.example/t/1", "title": "Frank Herbert - Dune", "protocol": "torrent",
         "indexer": "1337x", "magnetUrl": "magnet:?xt=urn:btih:" + "a" * 40},
        {"guid": "https://1337x.example/t/2", "title": "Dan Simmons - Hyperion (retail) [MOBI]", "protocol": "torrent",
         "indexer": "1337x", "magnetUrl": "magnet:?xt=urn:btih:" + "b" * 40},
    ]
    client.get("/api/discover", params={"q": "books", "source": "prowlarr", "kind": "ebook"})
    dune = client.post("/api/library", json={"url": "https://1337x.example/t/1", "source": "prowlarr",
                                             "download": False, "format": "ebook"}).json()
    hyperion = client.post("/api/library", json={"url": "https://1337x.example/t/2", "source": "prowlarr",
                                                 "download": False}).json()
    assert dune["kind"] == "ebook"  # no format in its name, but it came from the ebook search
    assert hyperion["kind"] == "ebook"  # a MOBI is an ebook whichever way it was found


async def test_a_wanted_ebook_says_when_no_indexer_has_ebooks(prowlarr_api):
    prowlarr_api["indexers"] = INDEXERS[1:]
    book = wanted.add_wanted(DUNE, "ebook")
    assert await wanted.search_and_grab(book) is None
    assert "None of your Prowlarr indexers offer ebooks" in db.get(book["id"])["search_note"]
    prowlarr_api["indexers"] = list(INDEXERS)
    prowlarr._offering.clear()
    await wanted.search_and_grab(db.get(book["id"]))
    assert db.get(book["id"])["search_note"] == "Nothing found yet."
