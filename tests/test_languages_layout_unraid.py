"""0.8.6: preferred languages, folder layout presets, and the Unraid template."""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from narratarr import config, languages, layout, wanted

ROOT = Path(__file__).resolve().parents[1]


# ---- languages ---------------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "lang"), [
    ("Dune [German]", "German"), ("Dune.German.MP3", "German"), ("Dune (GER) M4B", "German"),
    ("Der Wüstenplanet - Hörbuch ungekürzt", "German"), ("Dune - English - 64kbps", "English"),
    ("Le Petit Prince livre audio", "French"),
    ("The German Girl", ""), ("Hyperion.Dan.Simmons", ""), ("Dune [Unabridged]", ""),  # no false alarms
])
def test_detect(text, lang):
    assert languages.detect(text) == lang


def test_the_books_own_words_dont_count():
    assert languages.detect("The German Girl [English] M4B", ignore="The German Girl Armando Lucas Correa") == "English"


def test_names_and_preferences():
    assert languages.parse("german, English;de, ") == ["German", "English"]
    assert languages.name("ger") == languages.name("Deutsch") == "German"
    assert languages.allowed("deu", ["German"]) and not languages.allowed("English", ["German"])
    assert languages.allowed("", ["German"])  # can't tell: never hidden
    assert languages.allowed("Klingon", [])  # no preference: anything goes


def test_settings_tidy_the_list(isolated_data):
    assert config.update({"languages": "de, english, German"}).languages == "German, English"
    assert config.update({"languages": ""}).languages == ""


async def test_automatic_search_skips_other_languages(isolated_data, monkeypatch):
    from narratarr.sources import Listing, Page

    config.update({"languages": "German", "prowlarr_url": "http://prowlarr", "prowlarr_api_key": "k"})
    hits = [
        Listing(url="u1", title="Dune [English] Frank Herbert", author="Frank Herbert", format="M4B"),
        Listing(url="u2", title="Dune Frank Herbert Hörbuch", author="Frank Herbert", format="MP3"),
    ]

    class Fake:
        async def search(self, q):
            return Page(items=hits, page=1, total_pages=1)

    async def nothing(*a, **k):
        return Page(items=[], page=1, total_pages=1)

    monkeypatch.setattr(wanted, "get_source", lambda name: Fake() if name == "prowlarr" else type("L", (), {"find": nothing})())
    found = await wanted.find_releases({"title": "Dune", "author": "Frank Herbert", "kind": "audiobook"})
    by_url = {f["url"]: f for f in found}
    assert by_url["u1"]["language"] == "English" and not by_url["u1"]["language_ok"]
    assert by_url["u2"]["language"] == "German" and by_url["u2"]["language_ok"]
    assert found[0]["url"] == "u2"  # yours first
    pick = wanted.best([{**by_url["u1"], "usable": True, "match": 100}])
    assert pick is None  # never grabbed automatically


# ---- folder layout presets ----------------------------------------------------------------------


ABS = "{author}/{series}/Vol {series_number} - {year} - {title} {{narrator}}"


def test_the_audiobookshelf_preset():
    dune = {"author": "Frank Herbert", "title": "Dune", "narrators": ["Scott Brick"],
            "series": [{"title": "Dune Chronicles", "sequence": "1"}], "released": "1965-08-01"}
    solo = {"author": "Andy Weir", "title": "Project Hail Mary", "narrators": [], "series": [], "released": "2021-05-04"}
    assert layout.unknown(ABS) == []
    assert layout.folders(ABS, dune) == ["Frank Herbert", "Dune Chronicles", "Vol 1 - 1965 - Dune {Scott Brick}"]
    assert layout.folders(ABS, solo) == ["Andy Weir", "2021 - Project Hail Mary"]


def test_presets_in_the_page_match():
    js = (ROOT / "narratarr/static/app.js").read_text()
    presets = re.search(r"const LAYOUT_PRESETS = \[(.*?)\];", js, re.S).group(1)
    for template in re.findall(r'\["[^"]+", "([^"]+)"\]', presets):
        assert layout.unknown(template) == [], template
    assert ABS in presets


# ---- Unraid --------------------------------------------------------------------------------------


def test_unraid_template_matches_the_image():
    tree = ET.parse(ROOT / "unraid/narratarr.xml").getroot()
    assert tree.tag == "Container" and tree.findtext("Repository") == "ghcr.io/narratarr/narratarr:latest"
    dockerfile = (ROOT / "Dockerfile").read_text()
    port = re.search(r"EXPOSE (\d+)", dockerfile).group(1)
    volumes = set(re.findall(r'"(/[^"]+)"', re.search(r"VOLUME \[(.*?)\]", dockerfile).group(1)))
    configs = {c.get("Target"): c for c in tree.iter("Config")}
    assert configs[port].get("Type") == "Port" and port in tree.findtext("WebUI")
    assert volumes <= {t for t, c in configs.items() if c.get("Type") == "Path"}
    entrypoint = (ROOT / "docker/entrypoint.sh").read_text()
    for t, c in configs.items():
        if c.get("Type") == "Variable":
            assert t in entrypoint or t in dockerfile, f"{t} isn't something the image reads"
    icon = tree.findtext("Icon").split("/main/", 1)[1]
    assert (ROOT / icon).is_file()
