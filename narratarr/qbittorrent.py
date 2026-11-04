"""Minimal async client for the qBittorrent Web API (v2).

Docs: https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-5.0)
"""

from __future__ import annotations

import asyncio

import httpx

from . import reach
from .downloaders import ClientError, Job

CHECKING = {"checkingUP", "checkingDL", "checkingResumeData", "moving", "allocating"}


class QBittorrentError(ClientError):
    pass


class QBittorrent:
    label = "qBittorrent"

    def __init__(self, url: str, username: str = "", password: str = "", transport=None):
        if not url:
            raise QBittorrentError("Add your qBittorrent address in Settings first.")
        base = url.rstrip("/")
        # qBittorrent's CSRF protection wants a Referer/Origin that matches its own address.
        self._client = httpx.AsyncClient(
            base_url=base, timeout=30, transport=transport, headers={"Referer": base, "Origin": base}
        )
        self._user, self._password = username, password
        self._logged_in = False

    async def __aenter__(self) -> QBittorrent:
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def _send(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            return await self._client.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise QBittorrentError(reach.explain(e, str(self._client.base_url), "qBittorrent", "http://192.168.1.10:8080")) from e

    async def _login(self) -> None:
        if self._logged_in or not (self._user or self._password):
            return
        resp = await self._send("POST", "/api/v2/auth/login", data={"username": self._user, "password": self._password})
        if resp.status_code == 403:
            raise QBittorrentError("qBittorrent has blocked this address after too many failed logins. Try again later.")
        # Older versions answer "Ok." / "Fails."; 5.x answers 204 on success and 401 on failure.
        if resp.status_code == 401 or resp.text.strip() == "Fails.":
            raise QBittorrentError(
                "qBittorrent rejected the login. Check the username and password. In Docker, keep qBittorrent's "
                "WEBUI_PORT the same as the port you open, or qBittorrent refuses the connection."
            )
        if resp.status_code >= 400:
            raise QBittorrentError(f"qBittorrent returned an error ({resp.status_code}).")
        self._logged_in = True

    async def _call(self, method: str, path: str, **kw) -> httpx.Response:
        await self._login()
        resp = await self._send(method, path, **kw)
        if resp.status_code in (401, 403):
            raise QBittorrentError("qBittorrent needs a username and password. Add them in Settings.")
        if resp.status_code == 404:
            raise QBittorrentError("That address doesn't look like qBittorrent's Web UI. Check it in Settings.")
        if resp.status_code >= 400:
            raise QBittorrentError(f"qBittorrent returned an error ({resp.status_code}).")
        return resp

    async def version(self) -> str:
        return (await self._call("GET", "/api/v2/app/version")).text.strip()

    async def add(self, kind: str, payload: str | bytes, tag: str, category: str = "", folder: str = "") -> str:
        fields = {"tags": (None, tag)}
        if category:
            fields["category"] = (None, category)
        if folder:  # a folder of its own wins over the category's (automatic management would move it)
            fields["savepath"] = (None, folder)
            fields["autoTMM"] = (None, "false")
        if kind == "magnet":
            fields["urls"] = (None, payload)
        else:
            fields["torrents"] = ("book.torrent", payload, "application/x-bittorrent")
        resp = await self._call("POST", "/api/v2/torrents/add", files=fields)
        if resp.text.strip() == "Fails.":
            raise QBittorrentError("qBittorrent didn't accept the torrent.")
        # Adding is asynchronous; find the new torrent by the tag we gave it.
        for _ in range(20):
            found = (await self._call("GET", "/api/v2/torrents/info", params={"tag": tag})).json()
            if found:
                return found[0]["hash"]
            await asyncio.sleep(0.5)
        raise QBittorrentError("qBittorrent accepted the torrent but it never showed up in its list.")

    async def job(self, torrent_hash: str) -> Job | None:
        found = (await self._call("GET", "/api/v2/torrents/info", params={"hashes": torrent_hash})).json()
        if not found:
            return None
        t = found[0]
        state = t.get("state", "")
        progress = float(t.get("progress") or 0) * 100
        if state in ("error", "missingFiles"):
            return Job("failed", error=f"qBittorrent reports a problem with this torrent ({state}).")
        if progress >= 100 and state not in CHECKING:
            path = t.get("content_path") or ""
            if path and path == t.get("save_path"):  # a bare single-file torrent: name the file, not the folder
                path = f"{path.rstrip('/')}/{t.get('name', '')}"
            return Job("completed", 100, path=path)
        if state in CHECKING:
            return Job("processing", progress, detail="qBittorrent is checking the files…")
        if state in ("pausedDL", "stoppedDL"):
            return Job("paused", progress, detail="Paused in qBittorrent")
        if state == "metaDL":
            return Job("queued", progress, detail="Getting the torrent's details from peers…")
        if state == "queuedDL":
            return Job("queued", progress, detail="Queued in qBittorrent")
        speed = int(t.get("dlspeed") or 0)
        seeds = t.get("num_seeds") or 0
        detail = "Waiting for peers…" if not speed else f"Downloading in qBittorrent · {seeds} seed{'s' * (seeds != 1)}"
        return Job("downloading", progress, speed, detail)

    async def delete(self, torrent_hash: str, files: bool = True, tag: str = "") -> None:
        """Remove a torrent, but only one Narratarr added for this book (it carries the book's tag).

        A torrent the user already had, or that another book is waiting on, is left alone.
        """
        found = (await self._call("GET", "/api/v2/torrents/info", params={"hashes": torrent_hash})).json()
        tags = {t.strip() for t in ((found[0].get("tags") or "") if found else "").split(",")}
        if tag and tag not in tags:
            return
        files = files and any(t.startswith("narratarr-") for t in tags)
        await self._call(
            "POST", "/api/v2/torrents/delete", data={"hashes": torrent_hash, "deleteFiles": str(files).lower()}
        )
