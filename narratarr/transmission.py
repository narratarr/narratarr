"""Minimal async client for Transmission's RPC API.

Docs: https://github.com/transmission/transmission/blob/main/docs/rpc-spec.md
"""

from __future__ import annotations

import base64
import posixpath

import httpx

from .downloaders import ClientError, Job

FIELDS = [
    "hashString", "name", "status", "percentDone", "leftUntilDone", "rateDownload", "error", "errorString",
    "downloadDir", "metadataPercentComplete", "peersSendingToUs",
]


class TransmissionError(ClientError):
    pass


class Transmission:
    label = "Transmission"

    def __init__(self, url: str, username: str = "", password: str = "", transport=None):
        if not url:
            raise TransmissionError("Add your Transmission address in Settings first.")
        base = url.rstrip("/")
        self._rpc = base if base.endswith("/rpc") else f"{base}/transmission/rpc"
        auth = (username, password) if username or password else None
        self._client = httpx.AsyncClient(timeout=30, auth=auth, transport=transport)
        self._session = ""

    async def __aenter__(self) -> Transmission:
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def _call(self, method: str, arguments: dict | None = None) -> dict:
        body = {"method": method, "arguments": arguments or {}}
        for _ in range(2):
            try:
                resp = await self._client.post(self._rpc, json=body, headers={"X-Transmission-Session-Id": self._session})
            except httpx.HTTPError:
                raise TransmissionError("Couldn't reach Transmission. Check the address in Settings.")
            if resp.status_code == 409:  # Transmission's CSRF handshake: retry with the session id it hands back
                self._session = resp.headers.get("X-Transmission-Session-Id", "")
                continue
            break
        if resp.status_code == 401:
            raise TransmissionError("Transmission rejected the username or password. Check them in Settings.")
        if resp.status_code == 403:
            raise TransmissionError("Transmission refused the connection. Add this computer to its RPC whitelist.")
        if resp.status_code >= 400:
            raise TransmissionError(f"Transmission returned an error ({resp.status_code}). Check the address in Settings.")
        try:
            data = resp.json()
        except ValueError:
            raise TransmissionError("That address doesn't look like Transmission. Check it in Settings.")
        if data.get("result") != "success":
            raise TransmissionError(f"Transmission: {data.get('result', 'request failed')}")
        return data.get("arguments") or {}

    async def version(self) -> str:
        return str((await self._call("session-get")).get("version", "")).split(" ")[0]

    async def add(self, kind: str, payload: str | bytes, tag: str = "", category: str = "", folder: str = "") -> str:
        """category is the user's label for Narratarr's torrents; folder is where to save, as Transmission sees it."""
        args = {"filename": payload} if kind == "magnet" else {"metainfo": base64.b64encode(payload).decode()}
        if folder:
            args["download-dir"] = folder
        res = await self._call("torrent-add", args)
        added = res.get("torrent-added")
        if added:  # label it so we know it's ours, and may delete its files if the user stops it
            label = category.replace(",", " ").strip()  # Transmission labels can't hold commas
            labels = [tag or "narratarr", *([label] if label and label != tag else [])]
            await self._call("torrent-set", {"ids": [added["id"]], "labels": labels})
        added = added or res.get("torrent-duplicate")
        if not added:
            raise TransmissionError("Transmission didn't accept the torrent.")
        return added["hashString"].lower()

    async def job(self, torrent_hash: str) -> Job | None:
        found = (await self._call("torrent-get", {"ids": [torrent_hash], "fields": FIELDS})).get("torrents") or []
        if not found:
            return None
        t = found[0]
        status = t.get("status")
        progress = float(t.get("percentDone") or 0) * 100
        if t.get("error") == 3:  # a local error (disk full, permissions…); 1 and 2 are tracker hiccups
            return Job("failed", error=f"Transmission: {t.get('errorString') or 'local error'}")
        if t.get("leftUntilDone") == 0 and progress >= 100 and status not in (1, 2):
            return Job("completed", 100, path=posixpath.join(t.get("downloadDir") or "", t.get("name") or ""))
        if status in (1, 2):
            return Job("processing", progress, detail="Transmission is checking the files…")
        if float(t.get("metadataPercentComplete") or 0) < 1:
            return Job("queued", 0, detail="Getting the torrent's details from peers…")
        if status == 0:
            return Job("paused", progress, detail="Paused in Transmission")
        if status == 3:
            return Job("queued", progress, detail="Queued in Transmission")
        speed = int(t.get("rateDownload") or 0)
        peers = t.get("peersSendingToUs") or 0
        detail = "Waiting for peers…" if not speed else f"Downloading in Transmission · {peers} peer{'s' * (peers != 1)}"
        return Job("downloading", progress, speed, detail)

    async def delete(self, torrent_hash: str, files: bool = True, tag: str = "") -> None:
        """Remove a torrent, but only one Narratarr added for this book (it carries the book's label).

        A torrent the user already had, or that another book is waiting on, is left alone.
        """
        found = (await self._call("torrent-get", {"ids": [torrent_hash], "fields": ["labels"]})).get("torrents") or []
        labels = {str(x) for x in ((found[0].get("labels") or []) if found else [])}
        if tag and tag not in labels:
            return
        files = files and any(x.startswith("narratarr") for x in labels)
        await self._call("torrent-remove", {"ids": [torrent_hash], "delete-local-data": files})
