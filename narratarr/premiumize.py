"""Minimal async client for the Premiumize.me API, shaped like the Real-Debrid and AllDebrid ones.

Docs: https://www.premiumize.me/api
"""

from __future__ import annotations

import time

import httpx

from .alldebrid import file_names

API = "https://www.premiumize.me/api"
MAX_DEPTH = 8  # folders inside a transfer's folder

_LIMIT = "Your Premiumize fair-use limit is used up for now"
ERRORS = {
    "authentication_failed": "Premiumize rejected the API key. Check it in Settings.",
    "permission_denied": "Premiumize says this account can't do that. Is it a premium account?",
    "account_limit_reached": f"{_LIMIT}, or too many transfers are running. Try again later.",
    "service_limit_reached": "Premiumize's limit for this kind of download is reached. Try again later.",
    "service_down": "Premiumize is down right now. Try again later.",
    "rate_limit_reached": "Premiumize is busy right now. Try again in a minute.",
    "not_found": "Premiumize no longer has this transfer.",
    "invalid_request": "Premiumize couldn't read this torrent.",
    "service_unsupported": "Premiumize doesn't support this download.",
    "link_generation_failed": "Premiumize couldn't make a download link for a file. Try again.",
}


class PremiumizeError(Exception):
    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


def _data(resp: httpx.Response) -> dict:
    """A Premiumize answer, or its error in plain words. Premiumize reports most errors with a 200."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        if resp.status_code in (429, 502, 503, 504):
            raise PremiumizeError(ERRORS["rate_limit_reached"], str(resp.status_code))
        raise PremiumizeError(f"Premiumize answered with an error ({resp.status_code}).", str(resp.status_code))
    if body.get("status") == "error" or resp.status_code >= 400:
        code = str(body.get("code") or resp.status_code)
        message = str(body.get("message") or "").strip()
        raise PremiumizeError(ERRORS.get(code) or (f"Premiumize: {message}" if message else f"Premiumize error: {code}"), code)
    return body


class Premiumize:
    label = "Premiumize"
    prefix = "pm:"  # how its job ids are told apart from other services' in the database
    delete_setting = "delete_from_pm_after"

    def __init__(self, api_key: str, transport: httpx.AsyncBaseTransport | None = None):
        if not api_key:
            raise PremiumizeError("Add your Premiumize API key in Settings first.", "authentication_failed")
        self._client = httpx.AsyncClient(
            base_url=API,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
            transport=transport,
        )

    async def __aenter__(self) -> Premiumize:
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, **kw) -> dict:
        return _data(await self._client.request(method, path, **kw))

    async def user(self) -> dict:
        info = await self._call("GET", "/account/info")
        until = info.get("premium_until")
        premium = isinstance(until, (int, float)) and until > time.time()
        return {"customer_id": info.get("customer_id"), "premium": premium,
                "premium_until": int(until) if premium else None, "limit_used": info.get("limit_used")}

    # ---- the steps a download goes through (the same ones RealDebrid has) ----

    async def add(self, kind: str, payload: str | bytes) -> str:
        if kind == "torrent":
            data = await self._call("POST", "/transfer/create",
                                    files={"src": ("release.torrent", payload, "application/x-bittorrent")})
        else:
            data = await self._call("POST", "/transfer/create", data={"src": payload})
        if data.get("id") in (None, ""):
            raise PremiumizeError("Premiumize didn't say which transfer it added.")
        return str(data["id"])

    async def _transfer(self, transfer_id: str) -> dict:
        items = (await self._call("GET", "/transfer/list")).get("transfers")
        for t in items if isinstance(items, list) else []:  # there's no way to ask for just one
            if isinstance(t, dict) and str(t.get("id")) == str(transfer_id):
                return t
        raise PremiumizeError(ERRORS["not_found"], "not_found")

    async def exists(self, transfer_id: str) -> bool:
        try:
            await self._transfer(transfer_id)
        except PremiumizeError:
            return False
        return True

    async def poll(self, transfer_id: str) -> dict:
        t = await self._transfer(transfer_id)
        status = str(t.get("status") or "").lower()
        if status in ("finished", "seeding"):
            return {"done": True, "progress": 100.0, "speed": 0, "detail": "Premiumize has it"}
        if status in ("error", "deleted", "banned", "timeout"):
            why = str(t.get("message") or "").strip()
            raise PremiumizeError(f"Premiumize couldn't fetch this torrent{f': {why}' if why else '.'}")
        try:
            progress = max(0.0, min(100.0, float(t.get("progress") or 0) * 100))
        except (TypeError, ValueError):
            progress = 0.0
        message = str(t.get("message") or "").strip()
        detail = "Queued on Premiumize" if status == "queued" else "Premiumize is downloading"
        return {"done": False, "progress": progress, "speed": 0, "detail": f"{detail} · {message}"[:200] if message else detail}

    async def direct_files(self, transfer_id: str) -> list[dict]:
        """The finished transfer's files as direct downloads: [{'filename', 'filesize', 'download'}]."""
        t = await self._transfer(transfer_id)
        entries: list[tuple[tuple[str, ...], int, str]] = []
        if t.get("file_id"):  # one file (its folder_id is just where it was put, maybe the top of the cloud)
            item = await self._call("GET", "/item/details", params={"id": t["file_id"]})
            if isinstance(item.get("link"), str) and item["link"].startswith("https://"):
                entries = [((str(item.get("name") or "file"),), int(item.get("size") or 0), item["link"])]
        elif t.get("folder_id"):
            entries = await self._walk(await self._own_folder(t), (), 0)
        if not entries:
            raise PremiumizeError("Premiumize finished but returned no files.")
        names = file_names([path for path, _, _ in entries])
        return [{"filename": name, "filesize": size, "download": link} for name, (_, size, link) in zip(names, entries)]

    async def _own_folder(self, t: dict) -> str:
        """The folder this transfer made: its folder_id, or the folder of the same name inside it.

        Never anything else, so Narratarr can't download (or delete) the rest of someone's cloud."""
        listing = await self._call("GET", "/folder/list", params={"id": t["folder_id"]})
        name = str(t.get("name") or "")
        if name and listing.get("name") == name and listing.get("parent_id"):
            return str(t["folder_id"])
        for item in listing.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "folder" and name and item.get("name") == name and item.get("id"):
                return str(item["id"])
        raise PremiumizeError("Premiumize finished, but Narratarr couldn't tell which folder holds this book's files.")

    async def _walk(self, folder_id: str, parents: tuple[str, ...], depth: int) -> list[tuple[tuple[str, ...], int, str]]:
        content = (await self._call("GET", "/folder/list", params={"id": folder_id})).get("content")
        out = []
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            path = (*parents, str(item["name"]))
            if item.get("type") == "folder" and item.get("id") and depth < MAX_DEPTH:
                out += await self._walk(str(item["id"]), path, depth + 1)
            elif item.get("type") == "file" and isinstance(item.get("link"), str) and item["link"].startswith("https://"):
                out.append((path, int(item.get("size") or 0), item["link"]))
        return out

    async def remove(self, transfer_id: str) -> None:
        """Remove the transfer and the copy it left in the Premiumize cloud."""
        try:
            t = await self._transfer(transfer_id)
        except PremiumizeError:
            return
        if t.get("file_id"):
            await self._call("POST", "/item/delete", data={"id": t["file_id"]})
        elif t.get("folder_id"):
            try:
                await self._call("POST", "/folder/delete", data={"id": await self._own_folder(t)})
            except PremiumizeError:
                pass  # not sure which folder it is: leave the files be rather than risk others
        await self._call("POST", "/transfer/delete", data={"id": transfer_id})
