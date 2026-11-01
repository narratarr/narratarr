"""Minimal async client for the TorBox API, shaped like the Real-Debrid and AllDebrid ones.

Docs: https://api-docs.torbox.app/
"""

from __future__ import annotations

from urllib.parse import urlparse

import httpx

from .alldebrid import file_names

API = "https://api.torbox.app"
APP_NAME = "Narratarr"  # shown to the user on TorBox's sign-in page

# download_state values that mean TorBox has given up on a torrent.
FAILED_STATES = ("error", "missingfiles", "failed")
WORKING = {
    "metadl": "TorBox is reading the torrent",
    "checkingresumedata": "TorBox is checking the files",
    "queued": "Queued on TorBox",
    "paused": "Paused on TorBox",
    "stalled (no seeds)": "TorBox is waiting for someone to share it",
}

_LIMIT = "TorBox says your plan's limit is reached"
ERRORS = {
    "NO_AUTH": "Add your TorBox API key in Settings first.",
    "BAD_TOKEN": "TorBox rejected the API key. Connect again in Settings.",
    "AUTH_ERROR": "TorBox rejected the API key. Connect again in Settings.",
    "ACTIVE_LIMIT": "TorBox's limit on active downloads is reached. Remove some from your TorBox account and try again.",
    "MONTHLY_LIMIT": f"{_LIMIT} for this month.",
    "COOLDOWN_LIMIT": f"{_LIMIT} for now. It'll lift shortly; Narratarr can try again then.",
    "DOWNLOAD_TOO_LARGE": "This download is too big for your TorBox plan.",
    "PLAN_RESTRICTED_FEATURE": "Your TorBox plan doesn't include this.",
    "BOZO_TORRENT": "TorBox couldn't read this torrent.",
    "DOWNLOAD_SERVER_ERROR": "TorBox had a problem with this download. Try again, or pick another release.",
    "DIFF_ISSUE": "TorBox's download queue is full.",
    "ITEM_NOT_FOUND": "TorBox no longer has this torrent.",
    "DATABASE_ERROR": "TorBox is having trouble right now. Try again in a minute.",
    "SERVER_ERROR": "TorBox is having trouble right now. Try again in a minute.",
}


class TorBoxError(Exception):
    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


def _data(resp: httpx.Response):
    """The `data` of a TorBox answer, or its error in plain words. Errors can come with any status code."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        if resp.status_code in (429, 502, 503, 504):
            raise TorBoxError("TorBox is busy right now. Try again in a minute.", str(resp.status_code))
        raise TorBoxError(f"TorBox answered with an error ({resp.status_code}).", str(resp.status_code))
    code = body.get("error")
    if code or body.get("success") is False or resp.status_code >= 400:
        code = str(code or resp.status_code)
        if resp.status_code == 429:
            raise TorBoxError("TorBox is busy right now. Try again in a minute.", code)
        detail = str(body.get("detail") or "").strip()
        raise TorBoxError(ERRORS.get(code) or (f"TorBox: {detail}" if detail else f"TorBox error: {code}"), code)
    return body.get("data")


def _percent(value) -> float:
    """TorBox's progress: a fraction (0 to 1). Anything bigger is already a percentage."""
    try:
        p = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(100.0, p * 100 if p <= 1 else p))


class TorBox:
    label = "TorBox"
    prefix = "tb:"  # how its job ids are told apart from other services' in the database
    delete_setting = "delete_from_tb_after"

    def __init__(self, api_key: str, transport: httpx.AsyncBaseTransport | None = None):
        if not api_key:
            raise TorBoxError("Connect TorBox in Settings first.", "NO_AUTH")
        self._key = api_key
        self._client = httpx.AsyncClient(
            base_url=API,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
            transport=transport,
        )

    async def __aenter__(self) -> TorBox:
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, **kw):
        return _data(await self._client.request(method, path, **kw))

    async def user(self) -> dict:
        data = await self._call("GET", "/v1/api/user/me")
        return data if isinstance(data, dict) else {}

    # ---- the steps a download goes through (the same ones RealDebrid has) ----

    async def add(self, kind: str, payload: str | bytes) -> str:
        if kind == "torrent":
            files = {"file": ("release.torrent", payload, "application/x-bittorrent")}
        else:
            files = {"magnet": (None, payload)}  # TorBox wants a multipart form, even for a magnet
        try:
            data = await self._call("POST", "/v1/api/torrents/createtorrent", files=files)
        except TorBoxError as e:
            if e.code != "DUPLICATE_ITEM" or kind == "torrent":
                raise
            data = {"torrent_id": await self._find(_hash_of(str(payload)))}  # already on TorBox: use that one
        tid = data.get("torrent_id") if isinstance(data, dict) else None
        if tid in (None, ""):
            raise TorBoxError("TorBox didn't say which torrent it added.")
        return str(tid)

    async def _find(self, info_hash: str) -> str:
        items = await self._call("GET", "/v1/api/torrents/mylist", params={"bypass_cache": "true"})
        for t in items if isinstance(items, list) else []:
            if info_hash and isinstance(t, dict) and str(t.get("hash") or "").lower() == info_hash:
                return str(t.get("id"))
        raise TorBoxError("TorBox already has this torrent, but Narratarr couldn't find it there.", "DUPLICATE_ITEM")

    async def _torrent(self, torrent_id: str) -> dict:
        data = await self._call("GET", "/v1/api/torrents/mylist", params={"id": torrent_id, "bypass_cache": "true"})
        if isinstance(data, list):  # asked for one, but sent a list
            data = next((t for t in data if isinstance(t, dict) and str(t.get("id")) == str(torrent_id)), None)
        if not isinstance(data, dict):
            raise TorBoxError(ERRORS["ITEM_NOT_FOUND"], "ITEM_NOT_FOUND")
        return data

    async def exists(self, torrent_id: str) -> bool:
        try:
            await self._torrent(torrent_id)
        except TorBoxError:
            return False
        return True

    async def poll(self, torrent_id: str) -> dict:
        t = await self._torrent(torrent_id)
        state = str(t.get("download_state") or "").strip().lower()
        if t.get("download_present") or (t.get("download_finished") and state in ("completed", "cached", "uploading")):
            return {"done": True, "progress": 100.0, "speed": 0, "detail": "TorBox has it"}
        if any(state.startswith(f) for f in FAILED_STATES):
            raise TorBoxError(f"TorBox couldn't fetch this torrent ({t.get('download_state')}).")
        detail = WORKING.get(state, "TorBox is downloading")
        if state == "downloading" or state not in WORKING:
            detail += f" · {int(t.get('seeds') or 0)} seeders"
        return {
            "done": False,
            "progress": _percent(t.get("progress")),
            "speed": int(t.get("download_speed") or 0),
            "detail": detail,
        }

    async def direct_files(self, torrent_id: str) -> list[dict]:
        """The finished torrent's files as direct downloads: [{'filename', 'filesize', 'download'}]."""
        files = [f for f in (await self._torrent(torrent_id)).get("files") or [] if isinstance(f, dict) and "id" in f]
        if not files:
            raise TorBoxError("TorBox finished but returned no files.")
        names = file_names([tuple(p for p in str(f.get("name") or f.get("short_name") or f["id"]).split("/") if p)
                            for f in files])
        out = []
        for name, f in zip(names, files):
            out.append({"filename": name, "filesize": int(f.get("size") or 0), "download": await self.link(torrent_id, f["id"])})
        return out

    async def link(self, torrent_id: str, file_id) -> str:
        # requestdl takes the key as a query parameter (TorBox's design); httpx doesn't log URLs at our log level.
        url = await self._call("GET", "/v1/api/torrents/requestdl",
                               params={"token": self._key, "torrent_id": torrent_id, "file_id": str(file_id)})
        if not isinstance(url, str) or not url.startswith("https://"):
            raise TorBoxError("TorBox didn't give a download link for a file.")
        return url

    async def remove(self, torrent_id: str) -> None:
        await self._call("POST", "/v1/api/torrents/controltorrent",
                         json={"torrent_id": int(torrent_id), "operation": "delete"})


def _hash_of(magnet: str) -> str:
    """The info hash in a magnet link, lower-case hex (or "" when it isn't one)."""
    for part in magnet.split("?", 1)[-1].split("&"):
        if part.lower().startswith("xt=urn:btih:"):
            return part[12:].lower()
    return ""


# ---- connecting with a code (no API key to copy) -----------------------------


def _trusted(url: str) -> bool:
    """Only ever send someone to TorBox itself."""
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (host == "torbox.app" or host.endswith(".torbox.app"))


async def device_start(transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """A short code for the user to enter on TorBox: {'code', 'device_code', 'url', 'interval', 'expires_in'}."""
    async with httpx.AsyncClient(base_url=API, timeout=20, transport=transport) as client:
        data = _data(await client.get("/v1/api/user/auth/device/start", params={"app": APP_NAME}))
    data = data if isinstance(data, dict) else {}
    code, device = str(data.get("code") or ""), str(data.get("device_code") or "")
    if not code or not device:
        raise TorBoxError("TorBox didn't hand out a code. Try again in a moment.")
    url = next((u for u in (data.get("friendly_verification_url"), data.get("verification_url"))
                if isinstance(u, str) and _trusted(u)), "")
    if not url:
        raise TorBoxError("TorBox sent a sign-in page Narratarr doesn't recognise, so it wasn't opened.")
    return {"code": code, "device_code": device, "url": url,
            "interval": max(3, int(data.get("interval") or 5)), "expires_in": 600}


async def device_check(device_code: str, transport: httpx.AsyncBaseTransport | None = None) -> str:
    """The new API key once the user has entered the code, or "" while we're still waiting."""
    async with httpx.AsyncClient(base_url=API, timeout=20, transport=transport) as client:
        try:
            data = _data(await client.post("/v1/api/user/auth/device/token", json={"device_code": device_code}))
        except TorBoxError as e:
            if e.code == "DEVICE_CODE_NOT_USED":  # not entered yet
                return ""
            if e.code in ("ITEM_NOT_FOUND", "DEVICE_CODE_USED"):
                raise TorBoxError("That code has expired. Choose Connect TorBox to get a new one.", "EXPIRED")
            raise
    token = data.get("access_token") if isinstance(data, dict) else None
    return token if isinstance(token, str) else ""
