"""Download clients as a list, routing rules, and the migration that carries an install over.

No test here talks to a daemon: every client is built with a MockTransport, and the resolver is a
pure function of settings plus a book row.
"""

import json
from pathlib import Path

import httpx
import pytest

import logging

from narratarr import config, db, downloaders, routing
from narratarr.downloaders import ClientEntry, client_for, entries, entry_from_dict


@pytest.fixture
def flat(tmp_path, monkeypatch):
    """A settings file in the old shape, written before the client list existed."""
    def write(raw: dict):
        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
        monkeypatch.setattr(config, "_settings", None)
        (tmp_path / "settings.json").write_text(json.dumps(raw))
        return config.load()
    return write


FLAT_BOTH = {
    "sabnzbd_url": "http://nas:8080", "sabnzbd_api_key": "SABKEY1234", "sabnzbd_category": "books",
    "usenet_client": "sabnzbd",
    "qbittorrent_url": "http://nas:8081", "qbittorrent_username": "dave", "qbittorrent_password": "qbtpass",
    "qbittorrent_category": "audiobooks", "qbittorrent_folder": "/downloads/audio",
    "torrent_client": "qbittorrent",
}


# ---- migration ------------------------------------------------------------------------------

def test_migration_carries_every_value_over(flat):
    """An install with Usenet and torrents configured has two clients, and both must survive. A
    single entry would silently drop one of them, which is exactly what the upgrade promise forbids."""
    s = flat(FLAT_BOTH)
    by_id = {c["id"]: c for c in s.clients}

    assert by_id["sabnzbd"]["url"] == "http://nas:8080"
    assert by_id["sabnzbd"]["api_key"] == "SABKEY1234"
    assert by_id["sabnzbd"]["category"] == "books"

    assert by_id["qbittorrent"]["url"] == "http://nas:8081"
    assert by_id["qbittorrent"]["username"] == "dave"
    assert by_id["qbittorrent"]["password"] == "qbtpass"
    assert by_id["qbittorrent"]["category"] == "audiobooks"
    assert by_id["qbittorrent"]["save_path"] == "/downloads/audio"  # the folder setting follows it
    assert s.default_client == "qbittorrent"


def test_a_transmission_label_and_folder_become_category_and_save_path(flat):
    s = flat({"transmission_url": "http://nas:9091", "transmission_label": "audio",
              "transmission_folder": "/data/audio", "torrent_client": "transmission"})
    entry = s.clients[0]
    assert entry["type"] == "transmission"
    assert entry["category"] == "audio"
    assert entry["save_path"] == "/data/audio"


def test_a_debrid_key_migrates_without_a_url(flat):
    s = flat({"rd_api_key": "RDKEY999", "torrent_client": "realdebrid"})
    assert s.clients == [{"id": "realdebrid", "type": "realdebrid", "api_key": "RDKEY999"}]
    assert s.default_client == "realdebrid"


def test_nothing_configured_migrates_to_an_empty_list(flat):
    """No client yet, so no invented one. This is a fresh install, not a broken upgrade."""
    s = flat({"library_dir": "/audiobooks"})
    assert s.clients == []
    assert s.default_client == ""


def test_an_unconfigured_protocol_does_not_become_an_empty_client(flat):
    s = flat({"sabnzbd_url": "http://nas:8080", "sabnzbd_api_key": "k", "usenet_client": "sabnzbd",
              "torrent_client": "qbittorrent"})  # qBittorrent never filled in
    assert [c["id"] for c in s.clients] == ["sabnzbd"]


def test_migration_does_not_repeat_once_a_list_exists(flat):
    s = flat(FLAT_BOTH | {"clients": [{"id": "deluge", "type": "deluge", "url": "http://nas:8112"}]})
    assert [c["id"] for c in s.clients] == ["deluge"], "an existing list must be left alone"


# ---- the entry model -------------------------------------------------------------------------

def test_client_for_builds_the_right_object_for_each_type():
    for kind in ("sabnzbd", "nzbget", "qbittorrent", "transmission", "deluge",
                 "realdebrid", "alldebrid", "torbox", "premiumize"):
        entry = ClientEntry(id=kind, type=kind, url="http://nas:1", api_key="k", password="p")
        client = client_for(entry, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        assert client is not None, kind
        assert client.label, kind


def test_configured_is_about_the_fields_that_client_needs():
    assert ClientEntry("a", "sabnzbd", url="http://x", api_key="k").configured
    assert not ClientEntry("a", "sabnzbd", url="http://x").configured       # no key
    assert not ClientEntry("a", "qbittorrent").configured                    # no address
    assert ClientEntry("a", "realdebrid", api_key="k").configured            # a key is enough
    assert not ClientEntry("a", "realdebrid").configured


def test_delivery_tells_the_pipeline_which_path_a_grab_takes():
    assert ClientEntry("a", "sabnzbd").delivery == "usenet"
    assert ClientEntry("a", "nzbget").delivery == "usenet"
    assert ClientEntry("a", "qbittorrent").delivery == "torrent"
    assert ClientEntry("a", "realdebrid").delivery == "torrent"
    assert ClientEntry("a", "realdebrid").is_debrid
    assert not ClientEntry("a", "deluge").is_debrid


def test_an_unknown_type_is_ignored_rather_than_crashing_a_load():
    assert entry_from_dict({"id": "x", "type": "transmission-4"}) is None
    assert entry_from_dict("not a dict") is None


def test_entries_skips_records_it_cannot_build(flat):
    s = flat({"clients": [{"id": "q", "type": "qbittorrent", "url": "http://x"},
                          {"id": "junk", "type": "nonsense"}]})
    assert [e.id for e in entries(s)] == ["q"]


# ---- validation ------------------------------------------------------------------------------

def test_a_masked_secret_posted_back_keeps_the_stored_one(flat):
    s = flat(FLAT_BOTH)
    masked = s.public()["clients"]
    posted = [dict(c) for c in masked]
    config.update({"clients": posted})
    by_id = {c["id"]: c for c in config.load().clients}
    assert by_id["qbittorrent"]["password"] == "qbtpass", "a masked password must not overwrite the real one"
    assert by_id["sabnzbd"]["api_key"] == "SABKEY1234"


def test_a_new_secret_replaces_the_stored_one(flat):
    flat(FLAT_BOTH)
    config.update({"clients": [{"id": "qbittorrent", "type": "qbittorrent", "url": "http://nas:8081",
                                "password": "newpass"}]})
    assert config.load().clients[0]["password"] == "newpass"


def test_an_unknown_client_type_is_refused_with_a_message(flat):
    flat(FLAT_BOTH)
    with pytest.raises(ValueError) as err:
        config.update({"clients": [{"id": "x", "type": "utorrent"}]})
    assert "utorrent" in str(err.value)


def test_two_clients_cannot_share_an_id(flat):
    flat(FLAT_BOTH)
    with pytest.raises(ValueError):
        config.update({"clients": [{"id": "same", "type": "deluge", "url": "http://a"},
                                   {"id": "same", "type": "qbittorrent", "url": "http://b"}]})


def test_removing_the_default_client_repoints_it(flat):
    flat(FLAT_BOTH)
    config.update({"clients": [{"id": "sabnzbd", "type": "sabnzbd", "url": "http://nas:8080", "api_key": "k"}]})
    s = config.load()
    assert s.default_client == "sabnzbd"
    assert s.default_client in [c["id"] for c in s.clients]


def test_client_secrets_never_reach_the_browser(flat):
    s = flat(FLAT_BOTH)
    blob = json.dumps(s.public())
    assert "qbtpass" not in blob
    assert "SABKEY1234" not in blob


# ---- the resolver ----------------------------------------------------------------------------

TWO_CLIENTS = [
    {"id": "qbit", "type": "qbittorrent", "url": "http://qbit:8080", "category": "audiobooks"},
    {"id": "deluge", "type": "deluge", "url": "http://deluge:8112", "password": "pw"},
]


@pytest.fixture
def routed(flat):
    """Two clients and a default, before any rules."""
    def make(rules=None):
        return config.update({"clients": TWO_CLIENTS, "default_client": "qbit",
                              "client_routes": {"version": 1, "rules": rules or []}})
    return make


def test_no_routes_behaves_exactly_as_today(routed):
    """The upgrade promise: with no rules the flat choice still decides, per protocol."""
    s = routed()
    assert routing.routes(s) == []
    torrent = routing.resolve(s, "The Pirate Bay", "audiobook", "torrent")
    assert torrent.client_id == "qbit" and torrent.rule is None
    assert "no rule matched" in torrent.reason

    # the ids match the flat client names, which is what the migration produces
    s2 = config.update({"clients": TWO_CLIENTS + [{"id": "sabnzbd", "type": "sabnzbd",
                                                   "url": "http://sab:8080", "api_key": "k"}],
                        "usenet_client": "sabnzbd", "torrent_client": "deluge"})
    usenet = routing.resolve(s2, "NZBGeek", "audiobook", "usenet")
    assert usenet.client_id == "sabnzbd", "Usenet still goes to the Usenet client"
    torrent = routing.resolve(s2, "The Pirate Bay", "audiobook", "torrent")
    assert torrent.client_id == "deluge", "torrents still go to the torrent client"


def test_the_first_matching_rule_wins(routed):
    s = routed([
        {"match": {"indexers": ["The Pirate Bay"]}, "client": "deluge", "label": "first"},
        {"match": {"indexers": ["The Pirate Bay"]}, "client": "qbit", "label": "second"},
    ])
    got = routing.resolve(s, "The Pirate Bay", "audiobook", "torrent")
    assert got.client_id == "deluge" and got.label == "first"
    assert got.rule == 0
    assert "rule 1 matched" in got.reason


def test_order_is_precedence_and_reordering_changes_the_answer(routed):
    rules = [
        {"match": {"indexers": ["The Pirate Bay"]}, "client": "deluge"},
        {"match": {"indexers": ["The Pirate Bay"]}, "client": "qbit"},
    ]
    assert routing.resolve(routed(rules), "The Pirate Bay", "audiobook").client_id == "deluge"
    swapped = list(reversed(rules))
    assert routing.resolve(routed(swapped), "The Pirate Bay", "audiobook").client_id == "qbit"


def test_an_unmatched_release_falls_through_to_the_default(routed):
    s = routed([{"match": {"indexers": ["NZBGeek"]}, "client": "deluge"}])
    got = routing.resolve(s, "The Pirate Bay", "audiobook", "torrent")
    assert got.client_id == "qbit" and got.rule is None
    assert "no rule matched (The Pirate Bay" in got.reason


def test_a_category_rule_covers_every_indexer(routed):
    s = routed([{"match": {"categories": ["ebooks"]}, "client": "deluge"}])
    got = routing.resolve(s, "Any Indexer At All", "ebook")
    assert got.client_id == "deluge", "a category-only rule matches regardless of indexer"
    assert routing.resolve(s, "Any Indexer At All", "audiobook").client_id == "qbit"


def test_a_rule_can_name_both_and_then_both_must_agree(routed):
    s = routed([{"match": {"indexers": ["The Pirate Bay"], "categories": ["audiobooks"]}, "client": "deluge"}])
    assert routing.resolve(s, "The Pirate Bay", "audiobook").client_id == "deluge"
    assert routing.resolve(s, "The Pirate Bay", "ebook").client_id == "qbit"
    assert routing.resolve(s, "Other Tracker", "audiobook").client_id == "qbit"


def test_a_route_overrides_the_label_and_save_path(routed):
    s = routed([{"match": {"indexers": ["The Pirate Bay"]}, "client": "deluge",
                 "label": "kids", "save_path": "/audiobooks/kids"}])
    got = routing.resolve(s, "The Pirate Bay", "audiobook")
    assert (got.label, got.save_path) == ("kids", "/audiobooks/kids")


def test_a_route_with_no_label_or_path_leaves_the_clients_own(routed):
    s = routed([{"match": {"indexers": ["The Pirate Bay"]}, "client": "deluge"}])
    got = routing.resolve(s, "The Pirate Bay", "audiobook")
    assert (got.label, got.save_path) == ("", "")


def test_a_rule_naming_a_deleted_client_is_reported_not_skipped(routed):
    """Silent skipping is how someone concludes routing is broken: the release goes to the default
    and nothing says why."""
    s = routed([{"match": {"indexers": ["The Pirate Bay"]}, "client": "gone"}])
    got = routing.resolve(s, "The Pirate Bay", "audiobook")
    assert got.problems == ["Rule 1 sends to 'gone', which is not a client you have any more."]
    assert got.client_id == "qbit", "the grab still happens, on the default"


def test_coverage_names_the_indexers_that_fall_through(routed):
    s = routed([{"match": {"indexers": ["The Pirate Bay"]}, "client": "deluge"}])
    cov = routing.coverage(s, ["The Pirate Bay", "NZBGeek", "AudioBook Bay"])
    assert cov["covered"] == {"The Pirate Bay": 0}
    assert cov["uncovered"] == ["NZBGeek", "AudioBook Bay"]
    assert cov["unknown_clients"] == []


def test_coverage_reports_a_typo_d_indexer_as_uncovered(routed):
    s = routed([{"match": {"indexers": ["PirateBay"]}, "client": "deluge"}])  # missing "The "
    cov = routing.coverage(s, ["The Pirate Bay"])
    assert cov["covered"] == {} and cov["uncovered"] == ["The Pirate Bay"]


def test_an_indexer_match_tolerates_the_disambiguating_suffix(routed):
    """Prowlarr reports "The Pirate Bay (Prowlarr)" when two entries share a name."""
    s = routed([{"match": {"indexers": ["The Pirate Bay"]}, "client": "deluge"}])
    assert routing.resolve(s, "The Pirate Bay (Prowlarr)", "audiobook").client_id == "deluge"


# ---- validation --------------------------------------------------------------------------------

def test_a_route_that_matches_nothing_is_refused(routed):
    routed()
    with pytest.raises(ValueError):
        config.update({"client_routes": {"version": 1, "rules": [{"match": {}, "client": "qbit"}]}})


def test_a_route_without_a_client_is_refused(routed):
    routed()
    with pytest.raises(ValueError):
        config.update({"client_routes": {"version": 1, "rules": [{"match": {"indexers": ["x"]}}]}})


def test_the_stored_block_is_versioned(routed):
    s = routed([{"match": {"indexers": ["The Pirate Bay"]}, "client": "deluge"}])
    assert s.client_routes["version"] == routing.VERSION
    assert len(s.client_routes["rules"]) == 1


def test_a_bare_list_is_read_as_the_current_version(routed):
    s = routed([{"match": {"indexers": ["The Pirate Bay"]}, "client": "deluge"}])
    s.client_routes = [{"match": {"indexers": ["The Pirate Bay"]}, "client": "qbit"}]
    assert routing.routes(s)[0]["client"] == "qbit", "an older file shape still works"


# ---- what the settings page is allowed to do -------------------------------------------------
#
# These go through the API the UI calls, not the resolver directly: the UI posts the whole list and
# the whole rule list on every save, so the endpoints are where a reorder or a removed client has to
# behave.

def test_the_ui_can_reorder_and_the_answer_follows(accounts):
    """Saving the rules in a different order changes which client a release goes to. This is the
    whole precedence model, and it is the only test that proves the order survives a round trip."""
    c = accounts
    c.put("/api/settings", json={"clients": TWO_CLIENTS + [{"id": "transmission", "type": "transmission",
                                                           "url": "http://t:9091"}],
                                 "default_client": "qbit"})
    first = {"version": 1, "rules": [
        {"match": {"indexers": ["The Pirate Bay"]}, "client": "deluge", "label": "one"},
        {"match": {"indexers": ["The Pirate Bay"]}, "client": "qbit", "label": "two"},
    ]}
    assert c.put("/api/settings", json={"client_routes": first}).status_code == 200
    got = c.post("/api/routing/test", json={"indexer": "The Pirate Bay", "kind": "audiobook"}).json()
    assert (got["client"], got["rule"]) == ("deluge", 0)
    assert got["args"] == {"category": "one", "save_path": ""}  # the route's own label wins

    swapped = {"version": 1, "rules": list(reversed(first["rules"]))}
    assert c.put("/api/settings", json={"client_routes": swapped}).status_code == 200
    got = c.post("/api/routing/test", json={"indexer": "The Pirate Bay", "kind": "audiobook"}).json()
    assert (got["client"], got["rule"]) == ("qbit", 0), "the swabbed rule that matches first is now rule 1"


def test_a_rule_with_an_empty_indexer_list_does_not_restrict(accounts):
    """A category-only rule covers every indexer, which is how one rule replaces five."""
    c = accounts
    c.put("/api/settings", json={"clients": TWO_CLIENTS, "default_client": "qbit"})
    rules = {"version": 1, "rules": [{"match": {"indexers": [], "categories": ["ebooks"]}, "client": "deluge"}]}
    assert c.put("/api/settings", json={"client_routes": rules}).status_code == 200
    for indexer in ("The Pirate Bay", "NZBGeek", ""):
        got = c.post("/api/routing/test", json={"indexer": indexer, "kind": "ebook"}).json()
        assert got["client"] == "deluge", indexer
    # and it does not match the other kind
    assert c.post("/api/routing/test", json={"indexer": "The Pirate Bay", "kind": "audiobook"}).json()["client"] == "qbit"


def test_a_removed_client_is_not_selectable_and_shows_up_as_a_broken_rule(accounts):
    c = accounts
    c.put("/api/settings", json={"clients": TWO_CLIENTS, "default_client": "qbit"})
    c.put("/api/settings", json={"client_routes": {"version": 1, "rules": [
        {"match": {"indexers": ["The Pirate Bay"]}, "client": "deluge"}]}})

    # remove Deluge, the way the UI does: post the list without it
    saved = c.put("/api/settings", json={"clients": [{"id": "qbit", "type": "qbittorrent", "url": "http://qbit:8080"}],
                                        "default_client": "qbit"})
    assert saved.status_code == 200
    payload = c.get("/api/download-clients").json()
    assert [x["id"] for x in payload["clients"]] == ["qbit"], "a removed client is not offered"

    cov = c.get("/api/routing/coverage").json()
    assert cov["unknown_clients"] == ["Rule 1 -> deluge"]
    # and the grab still happens, on the default
    got = c.post("/api/routing/test", json={"indexer": "The Pirate Bay", "kind": "audiobook"}).json()
    assert got["client"] == "qbit" and got["problems"]


def test_the_test_button_reports_an_unsaved_entry_without_saving_it(accounts):
    c = accounts
    c.put("/api/settings", json={"clients": [{"id": "qbit", "type": "qbittorrent", "url": "http://qbit:8080"}]})
    r = c.post("/api/settings/test-download-client",
               json={"entry": {"id": "temp", "type": "deluge", "url": "http://deluge:8112", "password": "pw"}})
    assert r.status_code in (200, 400)  # whatever the daemon says, the entry was not stored
    assert [x["id"] for x in c.get("/api/download-clients").json()["clients"]] == ["qbit"]
    assert c.post("/api/settings/test-download-client", json={"entry": {"id": "x", "type": "nope"}}).status_code == 400


def test_the_json_the_settings_form_posts_is_accepted_as_text(accounts):
    """The form posts hidden inputs as text, so the endpoints have to parse them."""
    c = accounts
    r = c.put("/api/settings", json={"clients": json.dumps([{"id": "qbit", "type": "qbittorrent",
                                                            "url": "http://qbit:8080"}]),
                                     "client_routes": json.dumps({"version": 1, "rules": []})})
    assert r.status_code == 200, r.text
    assert [x["id"] for x in config.load().clients] == ["qbit"]


# ---- review fixes ---------------------------------------------------------------------------


@pytest.mark.real_auth
def test_a_listener_cannot_see_clients_indexers_or_rules(accounts):
    """Client addresses, usernames, save paths and the rules are the admin's, like the rest of Settings."""
    c = accounts
    assert c.post("/api/auth/login", json={"username": "admin", "password": "admin-pass"}).status_code == 200
    c.put("/api/settings", json={"clients": TWO_CLIENTS})
    assert c.get("/api/download-clients").status_code == 200
    assert c.post("/api/auth/login", json={"username": "listener", "password": "listen-pass"}).status_code == 200
    for method, path in [("get", "/api/download-clients"), ("get", "/api/indexers"),
                         ("post", "/api/routing/test"), ("get", "/api/routing/coverage")]:
        got = getattr(c, method)(path, **({"json": {"indexer": "x"}} if method == "post" else {}))
        assert got.status_code == 403, (path, got.status_code)


def test_a_saved_password_never_follows_the_address_to_another_machine(flat):
    """The rule the flat settings have always had: moving a client to a different host drops its
    saved secret unless a new one is typed, so it can't be sent somewhere it wasn't entered for."""
    flat({})
    config.update({"clients": [{"id": "qbit", "type": "qbittorrent", "url": "http://nas:8080", "password": "hunter2"}]})
    masked = config.load().public()["clients"][0]["password"]
    assert masked.startswith("•")

    config.update({"clients": [{"id": "qbit", "type": "qbittorrent", "url": "http://nas:8080/", "password": masked}]})
    assert config.load().clients[0]["password"] == "hunter2", "same machine: kept"

    config.update({"clients": [{"id": "qbit", "type": "qbittorrent", "url": "http://elsewhere:8080", "password": masked}]})
    assert config.load().clients[0]["password"] == "", "different machine: dropped"

    config.update({"clients": [{"id": "qbit", "type": "qbittorrent", "url": "http://elsewhere:8080", "password": "new-one"}]})
    assert config.load().clients[0]["password"] == "new-one", "a newly typed one is used"


def test_the_test_button_fills_a_masked_secret_only_for_the_same_machine(accounts, monkeypatch):
    seen = []

    class Probe:
        label = "qBittorrent"

        def __init__(self, entry):
            seen.append((entry.url, entry.password))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def version(self):
            return "v5"

    monkeypatch.setattr(downloaders, "client_for", lambda entry, transport=None: Probe(entry))
    accounts.put("/api/settings", json={"clients": [{"id": "qbit", "type": "qbittorrent", "url": "http://nas:8080", "password": "hunter2"}]})
    masked = accounts.get("/api/settings").json()["clients"][0]["password"]
    for url in ("http://nas:8080", "http://elsewhere:8080"):
        got = accounts.post("/api/settings/test-download-client",
                            json={"entry": {"id": "qbit", "type": "qbittorrent", "url": url, "password": masked}})
        assert got.status_code == 200, got.text
    assert seen == [("http://nas:8080", "hunter2"), ("http://elsewhere:8080", "")]


def test_a_category_rule_never_hands_a_usenet_release_to_a_torrent_client(flat):
    flat({})
    s = config.update({"clients": TWO_CLIENTS + [{"id": "sabnzbd", "type": "sabnzbd", "url": "http://sab:8080", "api_key": "k"}],
                       "client_routes": {"version": 1, "rules": [{"match": {"categories": ["audiobook"]}, "client": "deluge"}]}})
    torrent = routing.resolve(s, "Anything", "audiobook", "torrent")
    assert torrent.client_id == "deluge" and torrent.rule == 0
    usenet = routing.resolve(s, "NZBGeek", "audiobook", "usenet")
    assert usenet.client_id == "sabnzbd" and usenet.rule is None
    assert any("can't take a Usenet release" in p for p in usenet.problems)


def test_a_usenet_release_with_no_usenet_client_has_nowhere_to_go(flat):
    flat({})
    s = config.update({"clients": TWO_CLIENTS, "default_client": "qbit"})
    assert routing.resolve(s, "NZBGeek", "audiobook", "usenet").client_id == ""


def test_picking_a_default_is_picking_the_client_for_its_protocol(flat):
    flat({})
    config.update({"clients": TWO_CLIENTS, "default_client": "qbit"})
    s = config.update({"default_client": "deluge"})
    assert s.torrent_client == "deluge"
    assert routing.default_for(s, "torrent") == "deluge"
    # and the old way round still works: choosing a torrent client by name makes it the default
    s = config.update({"torrent_client": "qbittorrent"})
    assert routing.default_for(s, "torrent") == "qbit" and s.default_client == "qbit"


def test_a_save_that_repeats_the_default_does_not_undo_a_flat_choice(flat):
    """The settings form posts default_client on every save; that mustn't undo connecting AllDebrid."""
    flat({})
    config.update({"clients": TWO_CLIENTS + [{"id": "alldebrid", "type": "alldebrid", "api_key": "k"}], "default_client": "qbit"})
    config.update({"ad_api_key": "k2", "torrent_client": "alldebrid"})  # what the AllDebrid code sign-in does
    s = config.update({"default_client": config.load().default_client, "languages": ""})
    assert routing.default_for(s, "torrent") == "alldebrid"


def test_a_job_is_found_on_its_client_by_id(flat):
    flat({})
    s = config.update({"clients": [{"id": "qbittorrent", "type": "qbittorrent", "url": "http://a:1"},
                                   {"id": "qbittorrent-2", "type": "qbittorrent", "url": "http://b:2"}]})
    assert downloaders.entry_for_job(s, "qbittorrent-2").url == "http://b:2"
    assert downloaders.entry_for_job(s, "qbittorrent").url == "http://a:1"
    assert downloaders.entry_for_job(s, "deluge") is None


async def test_an_audiobookbay_grab_is_routed_by_the_source_name(flat, monkeypatch):
    """AudiobookBay names no indexer on its releases, so a rule on "AudiobookBay" has to fire at grab
    time, not only in the chip Discover shows."""
    from narratarr.worker import Worker

    flat({})
    config.update({"library_dir": str(config.DATA_DIR / "library"), "clients": TWO_CLIENTS,
                   "client_routes": {"version": 1, "rules": [{"match": {"indexers": ["AudiobookBay"]}, "client": "deluge"}]}})
    db.init()
    book = db.insert({"source": "audiobookbay", "source_url": "https://audiobookbay.lu/abss/dune/", "title": "Dune",
                      "author": "Frank Herbert", "info_hash": "a" * 40, "status": "queued"})
    picked = []

    async def stop_here(self, book, source, entry, decision):
        picked.append(entry.id)

    monkeypatch.setattr(Worker, "_process_torrent_client", stop_here)
    await Worker().process(book["id"])
    assert picked == ["deluge"]
