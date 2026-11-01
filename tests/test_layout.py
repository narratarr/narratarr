"""The folder layout: every variable, and books that don't have a series or a year."""

from pathlib import Path

import pytest

from narratarr import config, layout
from narratarr.worker import destination

DUNE = {"author": "Frank Herbert", "title": "Dune", "narrators": ["Scott Brick", "Simon Vance", "Third"],
        "series": [{"asin": "B1", "title": "Dune Chronicles", "sequence": "1"}], "released": "1965-08-01"}
SOLO = {"author": "Andy Weir", "title": "Project Hail Mary", "narrators": [], "series": [], "released": ""}


@pytest.mark.parametrize(("template", "dune", "solo"), [
    ("{author}/{title}", ["Frank Herbert", "Dune"], ["Andy Weir", "Project Hail Mary"]),
    ("{author}/{series}/{title}", ["Frank Herbert", "Dune Chronicles", "Dune"], ["Andy Weir", "Project Hail Mary"]),
    ("{author}/{series}/Vol. {series_number} - {title}", ["Frank Herbert", "Dune Chronicles", "Vol. 1 - Dune"],
     ["Andy Weir", "Project Hail Mary"]),
    ("{author}/{title} ({year})", ["Frank Herbert", "Dune (1965)"], ["Andy Weir", "Project Hail Mary"]),
    ("{author} - {series} {series_number} - {title}", ["Frank Herbert - Dune Chronicles 1 - Dune"],
     ["Andy Weir - Project Hail Mary"]),
    ("{narrator}/{title}", ["Scott Brick, Simon Vance", "Dune"], ["Unknown Narrator", "Project Hail Mary"]),
    ("Audiobooks/{author}/{title}", ["Audiobooks", "Frank Herbert", "Dune"], ["Audiobooks", "Andy Weir", "Project Hail Mary"]),
])
def test_layouts(template, dune, solo):
    assert layout.folders(template, DUNE) == dune
    assert layout.folders(template, SOLO) == solo


def test_series_numbers_are_tidied():
    assert layout.values({**DUNE, "series": [{"title": "S", "sequence": "2.5, Dramatized"}]})["series_number"] == "2.5"
    assert layout.values({**DUNE, "series": [{"title": "S", "sequence": "Book 3"}]})["series_number"] == "Book 3"


def test_nothing_escapes_the_library(isolated_data):
    s = config.load()
    s.folder_template = "{author}/{series}/{title}"
    evil = {**DUNE, "author": "..", "series": [{"title": "../../etc", "sequence": ""}], "title": "passwd"}
    dest = destination(evil, s)
    assert dest.parts[-3:] == ("Unknown Author", "etc", "passwd")
    assert Path(s.library_dir).resolve() in dest.parents


def test_saving_checks_the_layout(isolated_data):
    with pytest.raises(ValueError, match=r"\{nope\}"):
        config.update({"folder_template": "{author}/{nope}"})
    with pytest.raises(ValueError, match="inside"):
        config.update({"folder_template": "../{author}"})
    assert config.update({"folder_template": "/{author}/{series}/{title}/"}).folder_template == "{author}/{series}/{title}"
    assert config.update({"folder_template": ""}).folder_template == "{author}/{title}"


def test_imports_read_series_and_year(tmp_path):
    from mutagen.id3 import TALB, TDRC, TPE1, TXXX

    from narratarr.importer import as_book, read_meta
    from tests.test_people import tagged_mp3

    book = tmp_path / "in" / "Words of Radiance"
    tagged_mp3(book / "01.mp3", album=TALB(text="Words of Radiance"), artist=TPE1(text="Brandon Sanderson"),
               series=TXXX(desc="SERIES", text="The Stormlight Archive"), part=TXXX(desc="SERIES-PART", text="2"),
               date=TDRC(text="2014-03-04"))
    row = as_book(read_meta(book, tmp_path / "in"), "copy", "queued")
    assert row["series"] == [{"asin": "", "title": "The Stormlight Archive", "sequence": "2"}]
    assert layout.folders("{author}/{series}/{series_number} - {title} ({year})", row) == [
        "Brandon Sanderson", "The Stormlight Archive", "2 - Words of Radiance (2014)"]
