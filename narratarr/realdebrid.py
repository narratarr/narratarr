"""Minimal async client for the Real-Debrid REST API.

Docs: https://api.real-debrid.com/
"""

from __future__ import annotations

import httpx

API = "https://api.real-debrid.com/rest/1.0"

# Torrent statuses that mean Real-Debrid will never finish this one.
DEAD = {"magnet_error", "error", "virus", "dead"}


class RealDebridError(Exception):
    pass


class RealDebrid:
    label = "Real-Debrid"
    prefix = ""  # its job ids are stored as they are (AllDebrid's carry "ad:")

    def __init__(self, api_key: str, transport: httpx.AsyncBaseTransport | None = None):
        if not api_key:
            raise RealDebridError("Add your Real-Debrid API key in Settings first.")
        self._client = httpx.AsyncClient(
            base_url=API,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
            transport=transport,
        )

    async def __aenter__(self) -> RealDebrid:
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, **kw) -> dict | list | None:
        resp = await self._client.request(method, path, **kw)
        if resp.status_code == 401:
            raise RealDebridError("Real-Debrid rejected the API key (401). Check it in Settings.")
        if resp.status_code == 403:
            raise RealDebridError("Real-Debrid refused the request (403). Is your account premium?")
        if resp.status_code >= 400:
            try:
                msg = resp.json().get("error", resp.text)
            except ValueError:
                msg = resp.text
            raise RealDebridError(f"Real-Debrid error {resp.status_code}: {msg}")
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    async def user(self) -> dict:
        return await self._call("GET", "/user")

    async def add_magnet(self, magnet: str) -> str:
        data = await self._call("POST", "/torrents/addMagnet", data={"magnet": magnet})
        return data["id"]

    async def add_torrent(self, torrent: bytes) -> str:
        data = await self._call("PUT", "/torrents/addTorrent", content=torrent)
        return data["id"]

    async def torrent_info(self, torrent_id: str) -> dict:
        return await self._call("GET", f"/torrents/info/{torrent_id}")

    async def select_all_files(self, torrent_id: str) -> None:
        await self._call("POST", f"/torrents/selectFiles/{torrent_id}", data={"files": "all"})

    async def unrestrict(self, link: str) -> dict:
        """Turn a hoster link into a direct download: {'filename', 'filesize', 'download', ...}."""
        return await self._call("POST", "/unrestrict/link", data={"link": link})

    async def delete_torrent(self, torrent_id: str) -> None:
        await self._call("DELETE", f"/torrents/delete/{torrent_id}")

    # ---- the steps a download goes through (the same ones AllDebrid has) ----

    async def add(self, kind: str, payload: str | bytes) -> str:
        return await (self.add_torrent(payload) if kind == "torrent" else self.add_magnet(payload))

    async def exists(self, torrent_id: str) -> bool:
        try:
            await self.torrent_info(torrent_id)
        except RealDebridError:
            return False
        return True

    async def poll(self, torrent_id: str) -> dict:
        info = await self.torrent_info(torrent_id)
        status = info.get("status", "")
        if status == "downloaded":
            return {"done": True, "progress": 100.0, "speed": 0, "detail": "Real-Debrid has it"}
        if status in DEAD:
            raise RealDebridError(f"Real-Debrid couldn't fetch this torrent ({status}).")
        if status == "waiting_files_selection":
            await self.select_all_files(torrent_id)
        detail = {
            "magnet_conversion": "Real-Debrid is reading the magnet…",
            "waiting_files_selection": "Selecting files…",
            "queued": "Queued on Real-Debrid",
            "downloading": f"Real-Debrid is caching · {info.get('seeders') or 0} seeders",
            "compressing": "Real-Debrid is packaging files…",
            "uploading": "Real-Debrid is finishing up…",
        }.get(status, f"Real-Debrid: {status}")
        return {"done": False, "progress": float(info.get("progress") or 0), "speed": int(info.get("speed") or 0), "detail": detail}

    async def direct_files(self, torrent_id: str) -> list[dict]:
        """The finished torrent's files as direct downloads: [{'filename', 'filesize', 'download'}]."""
        links = (await self.torrent_info(torrent_id)).get("links") or []
        if not links:
            raise RealDebridError("Real-Debrid finished but returned no files.")
        return [await self.unrestrict(link) for link in links]

    async def remove(self, torrent_id: str) -> None:
        await self.delete_torrent(torrent_id)
