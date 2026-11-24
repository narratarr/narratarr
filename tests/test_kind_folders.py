"""From the Discord: audiobooks and ebooks in separate Audiobookshelf libraries.

A folder layout with {kind} files audiobooks under "Audiobooks" and ebooks under "Ebooks", inside the
one library folder, so every safety check that keeps files inside it still applies. An optional second
Audiobookshelf library is rescanned after an ebook download.
"""

from pathlib import Path

import pytest
from test_ebooks import audiobook_in_library, fast, make_epub, queue_import  # noqa: F401 - fixture reuse
from test_tidy import book, put, rows  # noqa: F401 - fixture reuse
from test_tidy import lib as lib

from narratarr import config, db, layout, tidy, worker
from narratarr.worker import Worker

APART = "{kind}/{author}/{title}"


def test_kind_names_the_folder():
    assert layout.folders(APART, {"title": "Dune", "author": "Frank Herbert", "kind": "ebook"}) == [
        "Ebooks",
        "Frank Herbert",
        "Dune",
    ]
    assert (
        layout.folders(APART, {"title": "Dune", "author": "Frank Herbert", "kind": "audiobook"})[0]
        == "Audiobooks"
    )
    assert layout.folders(APART, {"title": "Dune", "author": "Frank Herbert"})[0] == "Audiobooks"
    assert layout.unknown(APART) == []
    assert layout.separates_kinds(APART) and not layout.separates_kinds("{author}/{title}")


async def test_an_ebook_gets_its_own_folder_when_the_layout_says_so(isolated_data):
    config.update({"folder_template": APART})
    audiobook_in_library()  # Frank Herbert/Dune, from before the layout changed
    src = make_epub(isolated_data / "downloads" / "dune-retail.epub")
    ebook = queue_import(src, "Dune", "Frank Herbert", "ebook")
    await Worker().process(ebook["id"])

    ebook = db.get(ebook["id"])
    root = Path(config.load().library_dir).resolve()
    assert ebook["status"] == "completed", ebook["error"]
    assert Path(ebook["path"]) == root / "Ebooks" / "Frank Herbert" / "Dune" / "Dune.epub"


async def test_without_kind_an_ebook_still_joins_its_audiobook(isolated_data):
    audio = audiobook_in_library()
    src = make_epub(isolated_data / "downloads" / "dune-retail.epub")
    ebook = queue_import(src, "Dune", "Frank Herbert", "ebook")
    await Worker().process(ebook["id"])
    assert Path(db.get(ebook["id"])["path"]).parent == Path(audio["path"])


# ---- Tidy -------------------------------------------------------------------------------------


def test_tidy_moves_the_ebook_out_first_and_holds_its_audiobook(lib):
    folder = put(lib, "dune audiobook", files=("01.mp3", "Dune.epub"))
    audio = book(folder, series=[])
    ebook = book(folder / "Dune.epub", kind="ebook", series=[])
    config.update({"folder_template": APART})

    r = rows()
    assert r[ebook["id"]]["to"] == "Ebooks/Frank Herbert/Dune/Dune.epub"
    assert r[ebook["id"]]["state"] == "move"
    assert r[audio["id"]]["state"] == "held" and not r[audio["id"]].get("carries")

    tidy.apply(config.load(), [audio["id"], ebook["id"]])
    assert db.get(ebook["id"])["path"] == str(lib / "Ebooks/Frank Herbert/Dune/Dune.epub")
    assert (lib / "Ebooks/Frank Herbert/Dune/Dune.epub").exists()
    assert db.get(audio["id"])["path"] == str(folder), "held, so it didn't move"

    # The second run moves the audiobook, now its folder holds only its own files.
    assert rows()[audio["id"]]["state"] == "move"
    tidy.apply(config.load(), [audio["id"]])
    assert db.get(audio["id"])["path"] == str(lib / "Audiobooks/Frank Herbert/Dune")
    assert (lib / "Audiobooks/Frank Herbert/Dune/01.mp3").exists()
    assert Path(db.get(ebook["id"])["path"]).exists(), (
        "the ebook's recorded path still points at its file"
    )


def test_tidy_doesnt_send_an_ebook_to_join_its_audiobook(lib):
    audio = book(put(lib, "Dune audio"), series=[])
    ebook = book(
        put(lib, "Books/Herbert", files=("Dune.epub", "Other.pdf")) / "Dune.epub",
        kind="ebook",
        series=[],
    )
    config.update({"folder_template": APART})
    r = rows()
    assert r[audio["id"]]["to"] == "Audiobooks/Frank Herbert/Dune"
    assert r[ebook["id"]]["to"] == "Ebooks/Frank Herbert/Dune/Dune.epub"


# ---- Audiobookshelf -----------------------------------------------------------------------------


class _Server:
    scanned: list = []

    def __init__(self, url, token):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def scan(self, library_id):
        _Server.scanned.append(library_id)


@pytest.fixture
def scans(monkeypatch):
    _Server.scanned = []
    monkeypatch.setattr(worker.audiobookshelf, "Audiobookshelf", _Server)
    config.update(
        {
            "audiobookshelf_url": "http://abs:13378",
            "audiobookshelf_token": "t",
            "audiobookshelf_library_id": "lib-audio",
        }
    )
    return _Server.scanned


@pytest.mark.parametrize(
    "ebook_library, kind, expected",
    [
        ("", "audiobook", ["lib-audio"]),
        ("", "ebook", ["lib-audio"]),
        ("lib-ebooks", "audiobook", ["lib-audio"]),
        ("lib-ebooks", "ebook", ["lib-ebooks"]),
        ("lib-ebooks", "all", ["lib-audio", "lib-ebooks"]),
        ("", "all", ["lib-audio"]),
    ],
)
async def test_the_right_library_is_rescanned(scans, ebook_library, kind, expected):
    config.update({"audiobookshelf_ebook_library_id": ebook_library})
    await Worker()._notify_audiobookshelf(config.load(), kind)
    assert scans == expected


@pytest.mark.parametrize("bad", ["../users", "lib/../../api", "a b", "x" * 65, "lib?x=1"])
def test_a_library_id_is_only_an_id(bad):
    with pytest.raises(ValueError):
        config.update({"audiobookshelf_ebook_library_id": bad})
    with pytest.raises(ValueError):
        config.update({"audiobookshelf_library_id": bad})


def test_real_library_ids_are_accepted():
    for good in ("lib_c1u6t4p45c35rf0nzd", "e2a7f6b4-1c3d-4e5f-8a9b-0c1d2e3f4a5b", ""):
        assert (
            config.update({"audiobookshelf_ebook_library_id": good}).audiobookshelf_ebook_library_id
            == good
        )


# ---- review fixes: matching when each library watches its own kind folder ----------------------


def test_check_matches_books_when_audiobookshelf_watches_the_kind_folder(isolated_data):
    from narratarr import audiobookshelf

    lib = str(Path(config.load().library_dir).resolve())
    books = [{"title": "Dune", "kind": "audiobook", "path": f"{lib}/Audiobooks/Frank Herbert/Dune"}]
    items = [
        {
            "path": "/abs/audiobooks/Frank Herbert/Dune",
            "rel": "Frank Herbert/Dune",
            "missing": False,
        }
    ]
    assert audiobookshelf.compare(books, lib, items)["seen"] == 1


def test_fill_matches_by_folder_below_the_kind_folder(isolated_data):
    from narratarr import wanted

    lib = config.load().library_dir
    b = db.insert(
        {
            "source": "local",
            "source_url": "local:x",
            "title": "dune rip",
            "author": "?",
            "status": "completed",
            "path": str(Path(lib) / "Ebooks/Frank Herbert/Dune/Dune.epub"),
            "kind": "ebook",
        }
    )
    item = {"rel": "Frank Herbert/Dune", "title": "Dune", "author": "Frank Herbert"}
    assert wanted.abs_match(b, [item], lib) is item


def test_tidy_says_why_when_an_unfinished_ebook_is_in_the_folder(lib):
    folder = put(lib, "dune audiobook", files=("01.mp3", "Dune.epub"))
    audio = book(folder, series=[])
    book(folder / "Dune.epub", kind="ebook", series=[], status="downloading")
    config.update({"folder_template": APART})
    r = rows()[audio["id"]]
    assert r["state"] == "held" and "isn't finished" in r["reason"]
