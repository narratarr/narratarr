"""Several Torznab indexers, each with a name.

One Torznab endpoint was the whole model: Jackett's all-indexer address, or Prowlarr's. That is no
use to someone with, say, Jackett for audiobooks and a private unit3d indexer's own Torznab endpoint
alongside it — they could only ever have one, and routing rules could not tell the two apart because
both were called "Torznab".

The list is deliberately shaped like the download-client list: a flat setting migrated into it once,
the old fields still honoured, and a key that never follows its address to another host.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from narratarr import api, config, wanted
from narratarr.sources import Page, Torznab, all_sources, get_source, torznab_sources


def two_indexers() -> list[dict]:
    return [
        {"id": "mp3flix", "name": "MP3Flix", "url": "http://a.example/torznab/api", "api_key": "key-one"},
        {"id": "jackett", "name": "Jackett", "url": "http://b.example/torznab/api", "api_key": "key-two",
         "categories": "3030", "ebook_categories": "7020"},
    ]


def test_an_existing_install_is_migrated_and_keeps_working():
    """An address and key that worked keep working, under the name the old screen used."""
    url = "http://10.2.3.144:9117/api/v2.0/indexers/all/results/torznab/api"
    config.update({"torznab_url": url, "torznab_api_key": "KEY", "torznab_categories": "3030,7020"})

    # The settings file before this change: no list, only the flat fields.
    raw = json.loads(config.SETTINGS_FILE.read_text())
    raw.pop("torznab_indexers", None)
    config.SETTINGS_FILE.write_text(json.dumps(raw))
    config._settings = None

    s = config.load()
    assert [(e["id"], e["name"], e["url"], e["api_key"]) for e in s.torznab_indexers] == [
        ("torznab", "Torznab", url, "KEY")]
    assert s.torznab_indexers[0]["categories"] == "3030,7020"
    # A book grabbed before the list existed stores `torznab` as its source and must still resolve.
    assert get_source("torznab").base == url


def test_each_indexer_is_its_own_source_with_its_own_name():
    config.update({"torznab_indexers": two_indexers()})
    found = torznab_sources()
    assert set(found) == {"torznab:mp3flix", "torznab:jackett"}
    assert found["torznab:mp3flix"].label == "MP3Flix"
    assert found["torznab:jackett"].base == "http://b.example/torznab/api"
    # The bare name still resolves, to the first indexer, for older books.
    assert all_sources()["torznab"].base == "http://a.example/torznab/api"


def test_a_blank_card_is_dropped_and_an_unnamed_one_takes_its_host():
    config.update({"torznab_indexers": [
        {"url": "", "api_key": ""},
        {"url": "http://jackett:9117/torznab/api", "api_key": "k"},
    ]})
    got = config.load().torznab_indexers
    assert len(got) == 1
    assert got[0]["name"] == "jackett:9117"   # two indexers called "Torznab" tell nobody anything


def test_a_saved_key_never_follows_its_address_to_another_host():
    config.update({"torznab_indexers": [{"id": "a", "name": "A", "url": "http://one.example/api", "api_key": "secret"}]})
    masked = "••••••••cret"  # what the settings form posts back
    config.update({"torznab_indexers": [{"id": "a", "name": "A", "url": "http://one.example/api", "api_key": masked}]})
    assert config.load().torznab_indexers[0]["api_key"] == "secret"

    config.update({"torznab_indexers": [{"id": "a", "name": "A", "url": "http://two.example/api", "api_key": masked}]})
    assert config.load().torznab_indexers[0]["api_key"] == ""


def test_auto_search_runs_with_only_a_named_indexer():
    """An install set up under the new screen has no flat fields; automatic search must still run."""
    config.update({"torznab_indexers": two_indexers()})
    assert wanted.has_indexer(config.load()) is True

    config.update({"torznab_indexers": [], "torznab_url": "", "torznab_api_key": ""})
    assert wanted.has_indexer(config.load()) is False


def test_the_settings_page_gets_keys_masked_and_never_the_flat_pair():
    config.update({"torznab_indexers": two_indexers()})
    public = config.load().public()
    assert [e["name"] for e in public["torznab_indexers"]] == ["MP3Flix", "Jackett"]
    assert all(e["api_key"].startswith("•") for e in public["torznab_indexers"])
    assert public["torznab_configured"] is True
    assert "key-one" not in json.dumps(public)


def test_every_indexer_is_searched_and_results_say_which_one():
    """The result carries the indexer's own source name, which is what routing rules match on."""
    config.update({"torznab_indexers": two_indexers()})
    searched = []

    class Empty:
        def find(self, title, author):
            async def nothing():
                return Page(items=[], page=1, total_pages=1)
            return nothing()

        async def search(self, query, page=1):
            return Page(items=[], page=1, total_pages=1)

    async def spy(self, query, page=1):
        searched.append((self.id, self.base))
        return Page(items=[], page=1, total_pages=1)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wanted, "get_source", lambda name: Empty())
        mp.setattr(Torznab, "search", spy)
        asyncio.run(wanted.find_releases({"id": 1, "title": "Dune", "author": "Frank Herbert",
                                          "source": "audible", "source_url": "https://www.audible.com/pd/B0X",
                                          "kind": "audiobook", "protocol": "torrent", "language": ""}))

    assert sorted(base for _, base in searched) == ["http://a.example/torznab/api", "http://b.example/torznab/api"]


def test_the_api_lists_one_tab_per_indexer():
    config.update({"torznab_indexers": two_indexers()})
    with TestClient(api.app) as client:
        client.app.state.worker.poke = lambda: None
        listed = {s["name"]: s["label"] for s in client.get("/api/sources").json()}
    assert listed["torznab:mp3flix"] == "MP3Flix"
    assert listed["torznab:jackett"] == "Jackett"
    assert "torznab" not in listed          # no duplicate tab for the compatibility alias


def test_the_test_button_tests_the_indexer_it_belongs_to(monkeypatch):
    config.update({"torznab_indexers": two_indexers()})
    asked = []

    class Stub:
        def __init__(self, url, api_key, categories="3030", **kw):
            asked.append(url)

        async def status(self):
            return {"title": "ok", "book_search": True, "categories": 1}

    monkeypatch.setattr(api, "Torznab", Stub)
    with TestClient(api.app) as client:
        client.app.state.worker.poke = lambda: None
        assert client.post("/api/settings/test-torznab", json={"id": "jackett"}).status_code == 200
    assert asked == ["http://b.example/torznab/api"]
