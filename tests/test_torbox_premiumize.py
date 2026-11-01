"""TorBox and Premiumize: a torrent goes up, the files come down, and their errors read like people wrote them."""

import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from narratarr import api, config, db, torbox, worker
from narratarr.premiumize import Premiumize, PremiumizeError
from narratarr.torbox import TorBox, TorBoxError
from narratarr.worker import Worker

HASH = "061850ead3eb6f1c5c6d8420211b4bbf2d4ee3e2"


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(worker, "POLL_SECONDS", 0)


def make_book(**overrides):
    book = {"source": "audiobookbay", "source_url": "https://audiobookbay.lu/abss/dune-frank-herbert-2/", "title": "Dune",
            "author": "Frank Herbert", "info_hash": HASH, "trackers": [], "status": "queued"}
    return db.insert({**book, **overrides})


def multipart_field(request: httpx.Request, name: str) -> str:
    body = request.content.decode(errors="ignore")
    marker = f'name="{name}"'
    if marker not in body:
        return ""
    return body.split(marker, 1)[1].split("\r\n\r\n", 1)[1].split("\r\n--", 1)[0]


# ---- TorBox ---------------------------------------------------------------------------------


def tb(data=None, error=None, detail="", status=200, success=None):
    return httpx.Response(status, json={"success": not error if success is None else success, "error": error,
                                        "detail": detail, "data": data})


class FakeTorBox:
    def __init__(self, files: dict[str, bytes], create_error=None, final_state="completed"):
        self.files, self.create_error, self.final_state = files, create_error, final_state
        self.polls, self.magnet, self.deleted, self.tokens = 0, "", False, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, host = request.url.path, request.url.host
        if host == "cdn.torbox.example":
            return httpx.Response(200, content=self.files[path.rsplit("/", 1)[-1]])
        if path == "/v1/api/torrents/requestdl":
            q = request.url.params
            self.tokens.append(q["token"])
            name = list(self.files)[int(q["file_id"])]
            return tb(f"https://cdn.torbox.example/{name}")
        assert request.headers["authorization"] == "Bearer tb-key"
        if path == "/v1/api/torrents/createtorrent":
            if self.create_error:
                return tb(error=self.create_error, detail="No.", status=400)
            self.magnet = multipart_field(request, "magnet")
            return tb({"torrent_id": 5150, "hash": HASH, "auth_id": "x"})
        if path == "/v1/api/torrents/mylist":
            if "id" not in request.url.params:  # the whole list
                return tb([{"id": 5150, "hash": HASH}])
            assert request.url.params["id"] == "5150" and request.url.params["bypass_cache"] == "true"
            self.polls += 1
            files = [{"id": i, "name": f"Dune/{n}", "short_name": n, "size": len(d)} for i, (n, d) in enumerate(self.files.items())]
            if self.polls == 1:
                return tb({"id": 5150, "download_state": "downloading", "progress": 0.25, "download_speed": 1200, "seeds": 4,
                           "download_finished": False, "download_present": False, "files": []})
            done = self.final_state == "completed"
            return tb({"id": 5150, "download_state": self.final_state, "progress": 1 if done else 0.3,
                       "download_finished": done, "download_present": done, "files": files})
        if path == "/v1/api/torrents/controltorrent":
            assert json.loads(request.content) == {"torrent_id": 5150, "operation": "delete"}
            self.deleted = True
            return tb({})
        return tb(error="ITEM_NOT_FOUND", status=404)


@pytest.fixture
def use_torbox():
    config.update({"torrent_client": "torbox", "tb_api_key": "tb-key"})


async def test_full_pipeline_through_torbox(isolated_data, use_torbox):
    fake = FakeTorBox({"01.mp3": b"one" * 50, "02.mp3": b"two" * 50})
    book = make_book()
    await Worker(transport=httpx.MockTransport(fake)).process(book["id"])
    done = db.get(book["id"])
    assert done["status"] == "completed", done["error"]
    assert (Path(done["path"]) / "01.mp3").read_bytes() == b"one" * 50  # the torrent's own folder is dropped
    assert HASH in fake.magnet and fake.deleted and fake.tokens == ["tb-key", "tb-key"]
    assert done["rd_torrent_id"] == ""


async def test_torbox_keeps_its_job_when_asked(isolated_data, use_torbox):
    config.update({"delete_from_tb_after": False})
    fake = FakeTorBox({"Dune.m4b": b"audio"})
    book = make_book()
    await Worker(transport=httpx.MockTransport(fake)).process(book["id"])
    assert db.get(book["id"])["rd_torrent_id"] == "tb:5150" and not fake.deleted


async def test_torbox_errors_are_readable(isolated_data, use_torbox):
    book = make_book()
    await Worker(transport=httpx.MockTransport(FakeTorBox({}, create_error="ACTIVE_LIMIT")))._run(book["id"])
    assert "active downloads" in db.get(book["id"])["error"]

    db.update(book["id"], status="queued", rd_torrent_id="")
    await Worker(transport=httpx.MockTransport(FakeTorBox({"a.mp3": b"x"}, final_state="error")))._run(book["id"])
    assert db.get(book["id"])["error"].startswith("TorBox couldn't fetch this torrent")


async def test_torbox_already_has_it(isolated_data):
    async with TorBox("tb-key", transport=httpx.MockTransport(FakeTorBox({}, create_error="DUPLICATE_ITEM"))) as t:
        assert await t.add("magnet", f"magnet:?xt=urn:btih:{HASH.upper()}&dn=Dune") == "5150"


async def test_torbox_bad_key():
    def handler(request):
        return tb(error="BAD_TOKEN", detail="Your token is invalid or has expired.", status=403)

    async with TorBox("nope", transport=httpx.MockTransport(handler)) as t:
        with pytest.raises(TorBoxError, match="rejected the API key"):
            await t.user()


async def test_torbox_sign_in_with_a_code():
    asked = []

    def handler(request):
        if request.url.path.endswith("/device/start"):
            assert request.url.params["app"] == "Narratarr"
            return tb({"device_code": "DEV", "code": "123456", "verification_url": "https://torbox.app/oauth/device",
                       "friendly_verification_url": "https://tor.box.evil.example/x", "interval": 5})
        asked.append(json.loads(request.content)["device_code"])
        if len(asked) == 1:
            return tb(error="DEVICE_CODE_NOT_USED", status=400)
        return tb({"access_token": "new-key", "token_type": "Bearer"})

    t = httpx.MockTransport(handler)
    code = await torbox.device_start(t)
    assert code["code"] == "123456" and code["url"] == "https://torbox.app/oauth/device"  # the look-alike was skipped
    assert await torbox.device_check("DEV", t) == ""
    assert await torbox.device_check("DEV", t) == "new-key" and asked == ["DEV", "DEV"]


# ---- Premiumize ------------------------------------------------------------------------------


def pm(**data):
    return httpx.Response(200, json={"status": "success", **data})


def pm_error(code, message="nope"):
    return httpx.Response(200, json={"status": "error", "code": code, "message": message})  # errors come with a 200


class FakePremiumize:
    def __init__(self, files: dict[str, bytes], single=False, final="finished", folder_name="Dune"):
        self.files, self.single, self.final, self.folder_name = files, single, final, folder_name
        self.lists, self.src, self.deleted = 0, "", []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, host = request.url.path, request.url.host
        if host == "cdn.pm.example":
            return httpx.Response(200, content=self.files[path.rsplit("/", 1)[-1]])
        assert request.headers["authorization"] == "Bearer pm-key"
        form = {k: v[0] for k, v in parse_qs(request.content.decode(errors="ignore")).items()}
        if path == "/api/transfer/create":
            self.src = form["src"]
            return pm(id="T1", name="Dune")
        if path == "/api/transfer/list":
            self.lists += 1
            if self.lists == 1:
                return pm(transfers=[{"id": "other", "status": "running", "progress": 0.9},
                                     {"id": "T1", "name": "Dune", "status": "running", "progress": 0.4, "message": "12 MB/s"}])
            t = {"id": "T1", "name": "Dune", "status": self.final, "progress": 1.0, "message": "",
                 "folder_id": "ROOT" if self.single else "F1", "file_id": "FILE1" if self.single else None}
            return pm(transfers=[t])
        if path == "/api/folder/list":
            fid = request.url.params["id"]
            if fid == "F1":
                return pm(name=self.folder_name, parent_id="ROOT", folder_id="F1", content=[
                    {"id": "F2", "name": "CD1", "type": "folder"},
                    *[{"id": n, "name": n, "type": "file", "size": len(d), "link": f"https://cdn.pm.example/{n}"}
                      for n, d in list(self.files.items())[1:]]])
            if fid == "F2":
                n, d = next(iter(self.files.items()))
                return pm(name="CD1", parent_id="F1", content=[
                    {"id": n, "name": n, "type": "file", "size": len(d), "link": f"https://cdn.pm.example/{n}"}])
            return pm(name="My Files", content=[{"id": "F1", "name": "Not Dune", "type": "folder"}])
        if path == "/api/item/details":
            n, d = next(iter(self.files.items()))
            return pm(id="FILE1", name=n, size=len(d), link=f"https://cdn.pm.example/{n}")
        if path in ("/api/folder/delete", "/api/item/delete", "/api/transfer/delete"):
            self.deleted.append((path.rsplit("/", 2)[-2], form["id"]))
            return pm()
        if path == "/api/account/info":
            return pm(customer_id=1, premium_until=4102444800, limit_used=0.1)
        return pm_error("not_found")


@pytest.fixture
def use_premiumize():
    config.update({"torrent_client": "premiumize", "pm_api_key": "pm-key"})


async def test_full_pipeline_through_premiumize(isolated_data, use_premiumize):
    fake = FakePremiumize({"01.mp3": b"one" * 40, "02.mp3": b"two" * 40})
    book = make_book()
    await Worker(transport=httpx.MockTransport(fake)).process(book["id"])
    done = db.get(book["id"])
    assert done["status"] == "completed", done["error"]
    dest = Path(done["path"])
    assert (dest / "CD1 - 01.mp3").read_bytes() == b"one" * 40 and (dest / "02.mp3").read_bytes() == b"two" * 40
    assert HASH in fake.src
    assert fake.deleted == [("folder", "F1"), ("transfer", "T1")]  # its own folder, and nothing else


async def test_premiumize_single_file_never_touches_the_folder_its_in(isolated_data, use_premiumize):
    fake = FakePremiumize({"Dune.m4b": b"audio"}, single=True)
    book = make_book()
    await Worker(transport=httpx.MockTransport(fake)).process(book["id"])
    assert db.get(book["id"])["status"] == "completed"
    assert fake.deleted == [("item", "FILE1"), ("transfer", "T1")]  # never ("folder", "ROOT")


async def test_premiumize_wont_guess_at_a_folder(isolated_data, use_premiumize):
    fake = FakePremiumize({"01.mp3": b"x", "02.mp3": b"y"}, folder_name="Something else")
    book = make_book()
    await Worker(transport=httpx.MockTransport(fake))._run(book["id"])
    assert "couldn't tell which folder" in db.get(book["id"])["error"]
    async with Premiumize("pm-key", transport=httpx.MockTransport(fake)) as p:
        await p.remove("T1")
    assert ("folder", "F1") not in fake.deleted  # unsure, so the files stay


async def test_premiumize_errors_come_with_a_200():
    def handler(request):
        return pm_error("authentication_failed", "Not logged in.")

    async with Premiumize("bad", transport=httpx.MockTransport(handler)) as p:
        with pytest.raises(PremiumizeError, match="rejected the API key"):
            await p.user()


async def test_premiumize_failed_transfer(isolated_data, use_premiumize):
    book = make_book()
    await Worker(transport=httpx.MockTransport(FakePremiumize({"a": b""}, final="error")))._run(book["id"])
    assert db.get(book["id"])["error"].startswith("Premiumize couldn't fetch this torrent")


# ---- settings -----------------------------------------------------------------------------------


def test_new_services_are_choices_with_secret_keys(isolated_data):
    s = config.update({"torrent_client": "torbox", "tb_api_key": "tb-secret-1234", "pm_api_key": "pm-secret-5678"})
    shown = s.public()
    assert shown["torrent_client"] == "torbox" and shown["torrent_configured"] and shown["tb_configured"]
    assert "tb-secret" not in json.dumps(shown) and "pm-secret" not in json.dumps(shown)
    assert config.update({"torrent_client": "premiumize"}).public()["torrent_configured"]


def test_torbox_code_check_keeps_the_device_code_server_side(monkeypatch):
    async def start(transport=None):
        return {"code": "123456", "device_code": "SECRET-DEVICE", "url": "https://torbox.app/x", "interval": 5, "expires_in": 600}

    async def check(device_code, transport=None):
        assert device_code == "SECRET-DEVICE"
        return "new-key"

    async def user(self):
        return {"email": "me@example.com", "plan": 2, "is_subscribed": True}

    monkeypatch.setattr(torbox, "device_start", start)
    monkeypatch.setattr(torbox, "device_check", check)
    monkeypatch.setattr(TorBox, "user", user)
    with TestClient(api.app) as c:
        got = c.post("/api/settings/torbox/code").json()
        assert "SECRET" not in json.dumps(got) and got["code"] == "123456"
        assert c.post("/api/settings/torbox/code/check", json={"code": "999999"}).status_code == 400
        r = c.post("/api/settings/torbox/code/check", json={"code": "123456"}).json()
        assert r["connected"] and r["plan"] == "Pro" and r["settings"]["torrent_client"] == "torbox"
    assert config.load().tb_api_key == "new-key"
