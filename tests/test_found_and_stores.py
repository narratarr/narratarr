"""Issue #8: a release automatic search finds can wait for approval. Issue #9: more Audible stores."""

import pytest
from fastapi.testclient import TestClient

from narratarr import api, audible, config, db, notify, wanted
from test_import_wanted import AUDIBLE_BOOK, PROWLARR, fast, prowlarr_net  # noqa: F401  (fixtures)

FINAL_EMPIRE = "Brandon.Sanderson-Mistborn.01.The.Final.Empire.2008.M4B"


def release(n: int, title: str = FINAL_EMPIRE) -> dict:
    return {"guid": f"https://n.example/{n}", "title": title, "protocol": "usenet", "indexer": "N",
            "downloadUrl": f"{PROWLARR}/1/download?apikey=K&link={n}", "size": 1}


@pytest.fixture
def client():
    with TestClient(api.app) as c:
        c.app.state.worker.poke = lambda: None
        yield c


# ---- #8: hold what searching finds ------------------------------------------------


async def test_off_by_default_a_found_release_downloads(prowlarr_net):
    prowlarr_net.append(release(2))
    book = wanted.add_wanted(AUDIBLE_BOOK)
    assert config.load().found_needs_approval is False
    assert await wanted.auto_search() == 1
    assert db.get(book["id"])["status"] == "queued"


async def test_a_found_release_waits_for_approval_and_says_so(prowlarr_net):
    config.update({"found_needs_approval": True})
    prowlarr_net.append(release(2))
    book = wanted.add_wanted({**AUDIBLE_BOOK})
    db.update(book["id"], requested_by=None)

    assert await wanted.auto_search() == 1
    got = db.get(book["id"])
    assert got["status"] == "requested" and got["found_by_search"] == 1
    assert got["release_url"] == "https://n.example/2" and got["release"]  # the pick is attached, ready to approve
    assert wanted.due_for_search() == []  # held, not searched again

    found = db.list_events("found")
    assert len(found) == 1
    assert "waiting for your approval" in found[0]["message"] and "The Final Empire" in found[0]["message"]
    ctx = notify.context("found", got)
    assert ctx["release"] == got["release"] and ctx["eventLabel"] == "Release found"


async def test_approving_a_found_release_downloads_exactly_it(prowlarr_net, client):
    config.update({"found_needs_approval": True})
    prowlarr_net.append(release(2))
    book = wanted.add_wanted(AUDIBLE_BOOK)
    await wanted.auto_search()

    out = client.post(f"/api/library/{book['id']}/approve").json()
    assert out["status"] == "queued" and out["found_by_search"] == 0
    assert out["release_url"] == "https://n.example/2"


async def test_keep_looking_turns_the_release_down_for_good(prowlarr_net, client):
    config.update({"found_needs_approval": True})
    prowlarr_net += [release(2), release(3, "Brandon Sanderson - The Final Empire (Unabridged) [M4B]")]
    book = wanted.add_wanted(AUDIBLE_BOOK)
    await wanted.auto_search()
    first = db.get(book["id"])["release_url"]

    out = client.post(f"/api/library/{book['id']}/keep-looking")
    assert out.status_code == 200, out.text
    back = out.json()
    assert back["status"] == "wanted" and back["source"] == "audible" and back["release_url"] == ""
    assert back["skipped_releases"] == [first] and back["found_by_search"] == 0

    db.update(book["id"], last_searched=0)
    await wanted.auto_search()
    again = db.get(book["id"])
    assert again["status"] == "requested" and again["release_url"] not in ("", first)  # the other one, never the first


async def test_keep_looking_is_only_for_what_searching_found(client):
    book = wanted.add_wanted(AUDIBLE_BOOK)
    db.update(book["id"], status="requested")  # a listener's request, no release found by a search
    assert client.post(f"/api/library/{book['id']}/keep-looking").status_code == 409


@pytest.mark.real_auth
def test_listeners_cannot_turn_releases_down(accounts):
    book = wanted.add_wanted(AUDIBLE_BOOK)
    db.update(book["id"], status="requested", found_by_search=1)
    assert accounts.post("/api/auth/login", json={"username": "listener", "password": "listen-pass"}).status_code == 200
    assert accounts.post(f"/api/library/{book['id']}/keep-looking").status_code == 403
    assert accounts.get("/api/settings").json()["found_needs_approval"] is False  # they can see it, not change it


async def test_picking_another_release_clears_the_found_flag(prowlarr_net, client):
    config.update({"found_needs_approval": True})
    prowlarr_net += [release(2), release(3, "Brandon Sanderson - The Final Empire (Unabridged) [M4B]")]
    book = wanted.add_wanted(AUDIBLE_BOOK)
    await wanted.auto_search()
    out = client.post(f"/api/library/{book['id']}/grab", json={"url": "https://n.example/3", "source": "prowlarr"}).json()
    assert out["release_url"] == "https://n.example/3" and out["found_by_search"] == 0 and out["status"] == "queued"


# ---- #9: more Audible stores --------------------------------------------------------


def product(asin: str, title: str, language: str) -> dict:
    return {"asin": asin, "title": title, "authors": [{"name": "Antoine de Saint-Exupéry"}], "language": language,
            "content_delivery_type": "SinglePartBook", "product_images": {}}


@pytest.fixture
def stores(monkeypatch):
    """Audible's catalog per store: what each one returns for a search, and which hosts were asked."""
    shelves = {"com": [], "fr": [], "ca": []}
    asked = []

    async def fake_get(url, params=None, client=None):
        tld = url.split("api.audible.")[1].split("/")[0]
        asked.append(tld)
        items = shelves.get(tld, [])
        if params and "asins" in params:
            items = [p for p in items if p["asin"] in params["asins"].split(",")]
        return {"products": items, "total_results": len(items)}

    monkeypatch.setattr(audible, "_get", fake_get)
    config.update({"ratings_enabled": False})
    return shelves, asked


def test_store_from_a_book_link():
    assert audible.region_from_url("https://www.audible.fr/pd/B0ABCDEFGH") == "fr"
    assert audible.region_from_url("https://www.audible.com.au/pd/Title/B0ABCDEFGH") == "au"
    assert audible.region_from_url("https://www.audible.com/pd/B0ABCDEFGH") == "us"
    assert audible.region_from_url("https://openlibrary.org/works/OL1W") == ""


def test_extra_stores_are_validated_and_kept_in_order(client):
    config.update({"audible_extra_regions": "fr, ca, FR"})
    assert config.load().audible_extra_regions == ["fr", "ca"]
    assert client.put("/api/settings", json={"audible_extra_regions": "fr, xx"}).status_code == 400
    config.update({"audible_region": "fr"})
    assert audible.regions() == ["fr", "ca"]  # the main store isn't listed twice


def test_searching_another_store(client, stores):
    shelves, asked = stores
    shelves["fr"].append(product("B0FRENCH01", "Le Petit Prince", "french"))
    assert client.get("/api/discover", params={"source": "audible", "q": "petit prince", "region": "fr"}).status_code == 400

    config.update({"audible_extra_regions": ["fr"]})
    out = client.get("/api/discover", params={"source": "audible", "q": "petit prince", "region": "fr"}).json()
    assert asked[-1] == "fr" and out["region"] == "fr"
    assert [i["url"] for i in out["items"]] == ["https://www.audible.fr/pd/B0FRENCH01"]
    assert audible.region() == "us"  # the main store is untouched for everyone else


def test_nothing_in_the_main_store_tries_the_others(client, stores):
    shelves, asked = stores
    shelves["ca"].append(product("B0FRENCH01", "Le Petit Prince", "english"))
    shelves["fr"].append(product("B0FRENCH02", "Le Petit Prince", "french"))
    config.update({"audible_extra_regions": ["fr", "ca"]})
    out = client.get("/api/discover", params={"source": "audible", "q": "petit prince"}).json()
    assert asked == ["com", "fr"]  # in the admin's order, stopping at the first that has it
    assert out["region"] == "fr" and "United States" in out["region_note"] and "France" in out["region_note"]


def test_a_book_from_another_store_is_looked_up_there(client, stores):
    shelves, asked = stores
    shelves["fr"].append(product("B0FRENCH01", "Le Petit Prince", "french"))
    config.update({"audible_extra_regions": ["fr"]})
    out = client.post("/api/library", json={"url": "https://www.audible.fr/pd/B0FRENCH01", "source": "audible",
                                            "download": False})
    assert out.status_code == 200, out.text
    assert out.json()["title"] == "Le Petit Prince" and asked[-1] == "fr"


@pytest.mark.real_auth
def test_listeners_see_which_stores_they_can_search(accounts):
    accounts.put("/api/settings", json={"audible_extra_regions": ["fr"]})
    assert accounts.post("/api/auth/login", json={"username": "listener", "password": "listen-pass"}).status_code == 200
    assert accounts.get("/api/settings").json()["audible_extra_regions"] == ["fr"]
    assert accounts.put("/api/settings", json={"audible_extra_regions": ["ca"]}).status_code == 403
