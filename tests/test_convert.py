"""MP3 → M4B conversion before filing.

The rules that matter, in order: a folder of MP3s becomes one M4B; anything else is left alone; a
failure never fails the download; and the MP3s are removed by name only, because for a torrent
those names are hard links to files the client is still seeding.
"""

import subprocess
from pathlib import Path

import pytest

from narratarr import config, convert, media

HAS_FFMPEG = convert.available()


def mp3(path: Path, seconds: float = 1.0, title: str = "") -> Path:
    """A real, tiny MP3 — ffmpeg's lavfi source, no fixtures needed."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
           "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "libmp3lame", str(path)]
    if title:
        cmd = cmd[:-1] + ["-metadata", f"title={title}", str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


# ---- decisions ----------------------------------------------------------------------------


def test_mp3_only_payload_converts(tmp_path):
    mp3(tmp_path / "01.mp3")
    mp3(tmp_path / "02.mp3")
    assert convert.needs_conversion(tmp_path)


def test_a_payload_that_already_has_an_m4b_is_left_alone(tmp_path):
    mp3(tmp_path / "01.mp3")
    (tmp_path / "book.m4b").write_bytes(b"not really")
    assert not convert.needs_conversion(tmp_path)


@pytest.mark.parametrize("suffix", [".flac", ".m4a", ".aac", ".ogg", ".opus"])
def test_other_formats_are_not_converted(tmp_path, suffix):
    mp3(tmp_path / "01.mp3")
    (tmp_path / f"02{suffix}").write_bytes(b"x")
    assert not convert.needs_conversion(tmp_path)


def test_an_empty_folder_is_left_alone(tmp_path):
    assert not convert.needs_conversion(tmp_path)


def test_sidecar_files_do_not_stop_conversion(tmp_path):
    mp3(tmp_path / "01.mp3")
    (tmp_path / "cover.jpg").write_bytes(b"jpeg")
    (tmp_path / "desc.txt").write_text("a book")
    assert convert.needs_conversion(tmp_path)


# ---- chapters -----------------------------------------------------------------------------


def test_chapters_run_from_track_boundaries_in_cumulative_ms(tmp_path):
    files = [Path("/tmp/01.mp3"), Path("/tmp/02.mp3"), Path("/tmp/03.mp3")]
    listing, meta = convert.build_inputs(files, [600.0, 30.5, 90.0], {"title": "Dune", "author": "Frank Herbert"})

    assert listing.splitlines() == [
        "file '/tmp/01.mp3'", "file '/tmp/02.mp3'", "file '/tmp/03.mp3'",
    ]
    assert meta.splitlines()[:5] == [";FFMETADATA1", "title=Dune", "artist=Frank Herbert",
                                     "album=Dune", "album_artist=Frank Herbert"]
    starts = [int(line.split("=")[1]) for line in meta.splitlines() if line.startswith("START=")]
    ends = [int(line.split("=")[1]) for line in meta.splitlines() if line.startswith("END=")]
    assert starts == [0, 600_000, 630_500]
    assert ends == [600_000, 630_500, 720_500]
    assert meta.count("[CHAPTER]") == 3
    assert "TIMEBASE=1/1000" in meta


def test_a_track_with_no_title_is_named_by_position(tmp_path):
    files = [Path("/tmp/01.mp3"), Path("/tmp/02.mp3")]
    _, meta = convert.build_inputs(files, [1.0, 1.0], {"title": "Book"})
    # No tags on these paths, so both fall back.
    assert "title=Chapter 1" in meta and "title=Chapter 2" in meta


def test_quotes_and_newlines_are_escaped(tmp_path):
    files = [Path("/tmp/It's here.mp3")]
    listing, meta = convert.build_inputs(files, [1.0], {"title": "A\nB", "author": "C"})
    assert listing.strip() == "file '/tmp/It'\\''s here.mp3'"
    assert "title=A B" in meta, "a newline in a tag would otherwise end the field"


def test_a_duration_series_of_the_wrong_length_is_refused():
    with pytest.raises(ValueError):
        convert.build_inputs([Path("/tmp/01.mp3")], [], {})


# ---- settings -----------------------------------------------------------------------------


def test_conversion_is_off_by_default():
    s = config.Settings()
    assert s.convert_mp3_to_m4b is False
    assert s.convert_bitrate_kbps == 128
    assert s.convert_channels == 2


@pytest.mark.parametrize("value", [48, 128, 320])
def test_bitrate_inside_the_range_is_accepted(value):
    assert config.update({"convert_bitrate_kbps": value}).convert_bitrate_kbps == value


@pytest.mark.parametrize("value", [40, 0, 321, 1000])
def test_bitrate_outside_the_range_is_a_400(value):
    with pytest.raises(ValueError):
        config.update({"convert_bitrate_kbps": value})


@pytest.mark.parametrize("value", [0, 3, -1])
def test_channels_outside_one_or_two_is_a_400(value):
    with pytest.raises(ValueError):
        config.update({"convert_channels": value})


@pytest.mark.parametrize("value", [1, 2])
def test_channels_one_or_two_is_accepted(value):
    assert config.update({"convert_channels": value}).convert_channels == value


# ---- the real conversion ------------------------------------------------------------------


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg is not installed")
async def test_a_folder_of_mp3s_becomes_one_tagged_m4b_with_chapters(tmp_path):
    mp3(tmp_path / "01 - Opening.mp3", seconds=1.0, title="Opening")
    mp3(tmp_path / "02 - Closing.mp3", seconds=1.0, title="Closing")
    (tmp_path / "cover.jpg").write_bytes(b"not a real jpeg, and ffmpeg will say so")
    book = {"id": 1, "kind": "audiobook", "title": "Dune", "author": "Frank Herbert"}

    out = await convert.to_m4b(book, tmp_path, config.update({"convert_mp3_to_m4b": True}))

    assert out is not None and out.suffix == ".m4b" and out.is_file()
    assert not list(tmp_path.glob("*.mp3")), "the MP3 names are gone"
    assert out in media.audio_files(tmp_path)
    chapters = media.qt_chapters(out) if hasattr(media, "qt_chapters") else []
    assert len(chapters) >= 2, f"expected two chapters, got {chapters}"
    titles = [t for _, t in chapters]
    assert any("Opening" in t for t in titles) or titles[0].startswith("Chapter")


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg is not installed")
async def test_the_converted_file_carries_the_book_tags(tmp_path):
    mp3(tmp_path / "01.mp3", seconds=1.0)
    book = {"id": 2, "kind": "audiobook", "title": "Dune", "author": "Frank Herbert",
            "narrators": ["Simon Vance"], "series": []}

    out = await convert.to_m4b(book, tmp_path, config.update({"convert_mp3_to_m4b": True}))

    from narratarr import importer
    tags = importer._tags(out)
    assert tags.get("album") == "Dune"
    assert tags.get("artist") == "Frank Herbert"


async def test_switched_off_does_nothing(tmp_path, monkeypatch):
    mp3(tmp_path / "01.mp3")
    called = []
    monkeypatch.setattr(convert, "_encode", lambda *a, **k: called.append(a))
    assert await convert.to_m4b({"kind": "audiobook", "title": "x"}, tmp_path,
                          config.update({"convert_mp3_to_m4b": False})) is None
    assert called == []


async def test_without_ffmpeg_the_setting_is_inert_and_says_so(tmp_path, monkeypatch):
    mp3(tmp_path / "01.mp3")
    monkeypatch.setattr(convert, "available", lambda: False)
    assert await convert.to_m4b({"kind": "audiobook", "title": "x"}, tmp_path,
                          config.update({"convert_mp3_to_m4b": True})) is None
    assert list(tmp_path.glob("*.mp3")), "nothing is touched when ffmpeg is missing"


async def test_a_failed_encode_leaves_the_download_untouched(tmp_path, monkeypatch):
    mp3(tmp_path / "01.mp3")
    monkeypatch.setattr(convert, "available", lambda: True)
    monkeypatch.setattr(convert, "_encode", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    out = await convert.to_m4b({"id": 3, "kind": "audiobook", "title": "x"}, tmp_path,
                         config.update({"convert_mp3_to_m4b": True}))

    assert out is None
    assert list(tmp_path.glob("*.mp3")), "the MP3s stay for the normal filing path"
    assert not list(tmp_path.glob("*.m4b")), "a half-written M4B must not reach the library"


# ---- hardlink safety ----------------------------------------------------------------------


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg is not installed")
async def test_the_seeding_copy_survives_and_is_not_modified(tmp_path):
    """The tmp names are hard links to the torrent's files. Unlinking a name is safe; rewriting
    the file is not. This is the test that says so."""
    seeding = tmp_path / "download"
    seeding.mkdir()
    original = mp3(seeding / "01 - Opening.mp3", seconds=1.0, title="Opening")
    before = (original.read_bytes(), original.stat().st_mtime_ns)

    work = tmp_path / "tmp"
    work.mkdir()
    (work / original.name).hardlink_to(original)

    out = await convert.to_m4b({"id": 4, "kind": "audiobook", "title": "Dune", "author": "A"},
                         work, config.update({"convert_mp3_to_m4b": True}))

    assert out is not None
    assert original.exists(), "the client's file must still be there to seed"
    after = (original.read_bytes(), original.stat().st_mtime_ns)
    assert after == before, "the seeding file must be byte-identical and untouched"
    assert not list(work.glob("*.mp3")), "only the temp name is dropped"
    assert original.stat().st_nlink == 1


# ---- through the worker: the library gets what the setting decides ------------------------


def _local_book_with_mp3s(isolated_data, count=2):
    from narratarr import db, importer

    src = isolated_data / "inbox" / "Frank Herbert - Dune"
    src.mkdir(parents=True)
    for i in range(1, count + 1):
        mp3(src / f"{i:02d}.mp3", seconds=1.0)
    return db.insert(importer.as_book(importer.read_meta(src, src.parent), "move", "queued"))


async def test_the_worker_files_the_m4b_and_not_the_mp3s(tmp_path, isolated_data):
    from narratarr import db
    from narratarr.worker import Worker

    book = _local_book_with_mp3s(isolated_data)
    config.update({"convert_mp3_to_m4b": True})

    await Worker().process(book["id"])

    done = db.get(book["id"])
    dest = Path(done["path"])
    assert done["status"] == "completed", done["error"]
    assert list(dest.glob("*.m4b")), f"expected an M4B in {dest}: {list(dest.iterdir())}"
    assert not list(dest.glob("*.mp3")), "the tracks belong in the download folder, not the library"


async def test_switched_off_files_the_mp3s_as_before(isolated_data):
    from narratarr import db
    from narratarr.worker import Worker

    book = _local_book_with_mp3s(isolated_data)
    config.update({"convert_mp3_to_m4b": False})

    await Worker().process(book["id"])

    done = db.get(book["id"])
    dest = Path(done["path"])
    assert done["status"] == "completed", done["error"]
    assert len(list(dest.glob("*.mp3"))) == 2
    assert not list(dest.glob("*.m4b"))


async def test_a_conversion_that_blows_up_never_fails_the_download(isolated_data, monkeypatch):
    """The worst case is the old behaviour, not a failed book."""
    from narratarr import convert as conv
    from narratarr import db
    from narratarr.worker import Worker

    book = _local_book_with_mp3s(isolated_data)
    config.update({"convert_mp3_to_m4b": True})

    async def boom(*a, **k):
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr(conv, "to_m4b", boom)

    await Worker().process(book["id"])

    done = db.get(book["id"])
    assert done["status"] == "completed", done["error"]
    assert len(list(Path(done["path"]).glob("*.mp3"))) == 2


async def test_without_ffmpeg_books_say_why_in_the_log_and_still_file_the_mp3s(
    isolated_data, monkeypatch, caplog
):
    """`detail` is progress text and is cleared on completion, so the durable record is the log."""
    import logging

    from narratarr import convert as conv
    from narratarr import db
    from narratarr.worker import Worker

    book = _local_book_with_mp3s(isolated_data)
    config.update({"convert_mp3_to_m4b": True})
    monkeypatch.setattr(conv, "available", lambda: False)
    caplog.set_level(logging.INFO, logger="narratarr.worker")

    await Worker().process(book["id"])

    done = db.get(book["id"])
    assert done["status"] == "completed", done["error"]
    assert len(list(Path(done["path"]).glob("*.mp3"))) == 2
    assert any("ffmpeg isn't installed" in r.message for r in caplog.records), caplog.text
