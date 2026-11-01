"""Minimal async client for the AllDebrid API (v4), shaped like the Real-Debrid one.

Docs: https://docs.alldebrid.com/
"""

from __future__ import annotations

import asyncio

import httpx

API = "https://api.alldebrid.com"
SITE = "https://alldebrid.com"
DELAYED_POLL_SECONDS = 5  # AllDebrid asks for no faster than this
DELAYED_TRIES = 120  # ten minutes for a link that's still being generated

# A magnet's statusCode: 0-3 still working, 4 ready, 5 and up failed for good.
READY = 4
WORKING = {
    0: "Queued on AllDebrid",
    1: "AllDebrid is caching",
    2: "AllDebrid is packaging files…",
    3: "AllDebrid is finishing up…",
}
FAILED = {
    5: "the upload failed",
    6: "it couldn't unpack the files",
    7: "it didn't start downloading within 20 minutes",
    8: "the files are too big",
    10: "it took more than 72 hours",
    11: "it was removed from the source",
    14: "it couldn't reach the tracker",
    15: "no one is sharing it",
}

_PREMIUM = "AllDebrid needs a premium account for this."
_NO_SERVER = (
    "AllDebrid won't download to this server's address, because it looks like a data centre or VPN. "
    "See alldebrid.com/vpn, or pick another download client."
)
_BAD_TORRENT = "AllDebrid couldn't read this torrent."
_PIN_GONE = "That code has expired. Choose Connect AllDebrid to get a new one."
ERRORS = {
    "AUTH_MISSING_APIKEY": "Add your AllDebrid API key in Settings first.",
    "AUTH_BAD_APIKEY": "AllDebrid rejected the API key. Connect again in Settings.",
    "AUTH_BLOCKED": (
        "AllDebrid hasn't approved this server yet. It has emailed you a link to allow the new location: "
        "open it, then try again."
    ),
    "AUTH_USER_BANNED": "AllDebrid says this account is banned.",
    "MUST_BE_PREMIUM": _PREMIUM,
    "MAGNET_MUST_BE_PREMIUM": _PREMIUM,
    "FREE_TRIAL_LIMIT_REACHED": "Your AllDebrid free trial has run out.",
    "NO_SERVER": _NO_SERVER,
    "MAGNET_NO_SERVER": _NO_SERVER,
    "MAGNET_TOO_MANY_ACTIVE": (
        "AllDebrid allows 30 active torrents at once. Remove some from your AllDebrid account and try again."
    ),
    "MAGNET_INVALID_URI": _BAD_TORRENT,
    "MAGNET_INVALID_FILE": _BAD_TORRENT,
    "PIN_EXPIRED": _PIN_GONE,
    "PIN_INVALID": _PIN_GONE,
}


class AllDebridError(Exception):
    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


def _error(code: str, message: str) -> AllDebridError:
    return AllDebridError(ERRORS.get(code) or f"AllDebrid error: {message or code}", code)


def _data(resp: httpx.Response) -> dict:
    """The `data` of an AllDebrid answer, or its error in plain words."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        if resp.status_code in (429, 503):
            raise AllDebridError("AllDebrid is busy right now. Try again in a minute.", str(resp.status_code))
        raise AllDebridError(f"AllDebrid answered with an error ({resp.status_code}).", str(resp.status_code))
    if body.get("status") == "error" or resp.status_code >= 400:
        err = body.get("error") if isinstance(body.get("error"), dict) else {}
        code = str(err.get("code") or resp.status_code)
        if code in ("429", "503"):
            raise AllDebridError("AllDebrid is busy right now. Try again in a minute.", code)
        raise _error(code, str(err.get("message") or ""))
    data = body.get("data")
    return data if isinstance(data, dict) else {}


def _one(items, what: str) -> dict:
    """The first entry of a list AllDebrid answered with, raising its error if it has one."""
    if isinstance(items, dict):  # some answers about a single id are an object, not a list
        items = [items]
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        raise AllDebridError(f"AllDebrid sent back no {what}.")
    item = items[0]
    if isinstance(item.get("error"), dict):
        raise _error(str(item["error"].get("code") or ""), str(item["error"].get("message") or ""))
    return item


def flatten(tree, parents: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], int, str]]:
    """magnet/files' tree ({n, e: [...]} folders, {n, s, l} files) as (path, size, link)."""
    out = []
    for node in tree if isinstance(tree, list) else []:
        if not isinstance(node, dict) or not node.get("n"):
            continue
        path = (*parents, str(node["n"]))
        if isinstance(node.get("e"), list):
            out += flatten(node["e"], path)
        elif isinstance(node.get("l"), str) and node["l"]:
            out.append((path, int(node.get("s") or 0), node["l"]))
    return out


def file_names(paths: list[tuple[str, ...]]) -> list[str]:
    """Flat names that keep sub-folders ("CD1 - 01.mp3"), minus the one folder everything sits in."""
    while paths and all(len(p) > 1 for p in paths) and len({p[0] for p in paths}) == 1:
        paths = [p[1:] for p in paths]
    return [" - ".join(p) for p in paths]


class AllDebrid:
    label = "AllDebrid"
    prefix = "ad:"  # how its job ids are told apart from Real-Debrid's in the database
    delete_setting = "delete_from_ad_after"

    def __init__(self, api_key: str, transport: httpx.AsyncBaseTransport | None = None):
        if not api_key:
            raise AllDebridError("Connect AllDebrid in Settings first.", "AUTH_MISSING_APIKEY")
        self._client = httpx.AsyncClient(
            base_url=API,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
            transport=transport,
        )

    async def __aenter__(self) -> AllDebrid:
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, **kw) -> dict:
        return _data(await self._client.request(method, path, **kw))

    async def user(self) -> dict:
        user = (await self._call("GET", "/v4/user")).get("user")
        return user if isinstance(user, dict) else {}

    # ---- the steps a download goes through (the same ones RealDebrid has) ----

    async def add(self, kind: str, payload: str | bytes) -> str:
        if kind == "torrent":
            files = {"files[]": ("release.torrent", payload, "application/x-bittorrent")}
            item = _one((await self._call("POST", "/v4/magnet/upload/file", files=files)).get("files"), "torrent")
        else:
            item = _one((await self._call("POST", "/v4/magnet/upload", data={"magnets[]": payload})).get("magnets"), "torrent")
        if item.get("id") in (None, ""):
            raise AllDebridError("AllDebrid didn't say which torrent it added.")
        return str(item["id"])

    async def _status(self, magnet_id: str) -> dict:
        return _one((await self._call("POST", "/v4.1/magnet/status", data={"id": magnet_id})).get("magnets"), "status")

    async def exists(self, magnet_id: str) -> bool:
        try:
            await self._status(magnet_id)
        except AllDebridError:
            return False
        return True

    async def poll(self, magnet_id: str) -> dict:
        m = await self._status(magnet_id)
        code = int(m.get("statusCode") or 0)
        if code == READY:
            return {"done": True, "progress": 100.0, "speed": 0, "detail": "AllDebrid has it"}
        if code > READY:
            why = FAILED.get(code) or str(m.get("status") or "it failed")
            raise AllDebridError(f"AllDebrid couldn't fetch this torrent: {why}.")
        size, got = int(m.get("size") or 0), int(m.get("downloaded") or 0)
        detail = WORKING.get(code, "AllDebrid is working on it")
        if code == 1:
            detail += f" · {int(m.get('seeders') or 0)} seeders"
        return {
            "done": False,
            "progress": min(100.0, got * 100 / size) if size else 0.0,
            "speed": int(m.get("downloadSpeed") or 0),
            "detail": detail,
        }

    async def direct_files(self, magnet_id: str) -> list[dict]:
        """The finished torrent's files as direct downloads: [{'filename', 'filesize', 'download'}]."""
        data = await self._call("POST", "/v4/magnet/files", data={"id[]": magnet_id})
        entries = flatten(_one(data.get("magnets"), "file list").get("files"))
        if not entries:
            raise AllDebridError("AllDebrid finished but returned no files.")
        names = file_names([path for path, _, _ in entries])
        return [
            {"filename": name, "filesize": size, "download": await self.unlock(link)}
            for name, (_, size, link) in zip(names, entries)
        ]

    async def unlock(self, link: str) -> str:
        data = await self._call("POST", "/v4/link/unlock", data={"link": link})
        if isinstance(data.get("link"), str) and data["link"]:
            return data["link"]
        if data.get("delayed") in (None, ""):
            raise AllDebridError("AllDebrid didn't give a download link for a file.")
        for _ in range(DELAYED_TRIES):  # still being generated on their side
            await asyncio.sleep(DELAYED_POLL_SECONDS)
            later = await self._call("POST", "/v4/link/delayed", data={"id": data["delayed"]})
            if later.get("status") == 2 and isinstance(later.get("link"), str) and later["link"]:
                return later["link"]
            if later.get("status") == 3:
                raise AllDebridError("AllDebrid couldn't prepare a download link for a file.")
        raise AllDebridError("Timed out waiting for AllDebrid to prepare a download link.")

    async def remove(self, magnet_id: str) -> None:
        await self._call("POST", "/v4/magnet/delete", data={"id": magnet_id})


# ---- connecting with a PIN (no API key to copy) ------------------------------


async def pin_start(transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """A short code for the user to enter on alldebrid.com: {'pin', 'check', 'user_url', 'expires_in'}."""
    async with httpx.AsyncClient(base_url=API, timeout=20, transport=transport) as client:
        data = _data(await client.get("/v4.1/pin/get"))
    pin, check = str(data.get("pin") or ""), str(data.get("check") or "")
    if not pin or not check:
        raise AllDebridError("AllDebrid didn't hand out a code. Try again in a moment.")
    url = str(data.get("user_url") or "")
    if not url.startswith(SITE + "/"):  # only ever send the user to AllDebrid itself
        url = f"{SITE}/pin/"
    return {"pin": pin, "check": check, "user_url": url, "expires_in": int(data.get("expires_in") or 600)}


async def pin_check(pin: str, check: str, transport: httpx.AsyncBaseTransport | None = None) -> str:
    """The new API key once the user has entered the code, or "" while we're still waiting."""
    async with httpx.AsyncClient(base_url=API, timeout=20, transport=transport) as client:
        data = _data(await client.post("/v4/pin/check", data={"pin": pin, "check": check}))
    key = data.get("apikey")
    return key if isinstance(key, str) and data.get("activated", True) else ""
