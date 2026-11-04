"""Minimal async client for the SABnzbd API.

Docs: https://sabnzbd.org/wiki/configuration/4.3/api
"""

from __future__ import annotations

import httpx

from . import reach
from .downloaders import ClientError, Job


class SABnzbdError(ClientError):
    pass


QUEUE_LABELS = {
    "Queued": "Queued in SABnzbd",
    "Grabbing": "SABnzbd is reading the NZB…",
    "Fetching": "SABnzbd is reading the NZB…",
    "Propagating": "Waiting for the post to reach your Usenet server…",
}
HISTORY_LABELS = {
    "QuickCheck": "SABnzbd is checking the files…",
    "Verifying": "SABnzbd is checking the files…",
    "Repairing": "SABnzbd is repairing the files…",
    "Extracting": "SABnzbd is unpacking…",
    "Moving": "SABnzbd is moving the files…",
    "Running": "SABnzbd is running its post-processing script…",
}


class SABnzbd:
    label = "SABnzbd"

    def __init__(self, url: str, api_key: str, transport: httpx.AsyncBaseTransport | None = None):
        if not (url and api_key):
            raise SABnzbdError("Add your SABnzbd address and API key in Settings first.")
        self._key = api_key
        self._client = httpx.AsyncClient(base_url=url.rstrip("/"), timeout=30, transport=transport)

    async def __aenter__(self) -> SABnzbd:
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def _call(self, mode: str, files: dict | None = None, **params) -> dict:
        params = {"mode": mode, "apikey": self._key, "output": "json", **params}
        try:
            if files:
                resp = await self._client.post("/api", params=params, files=files)
            else:
                resp = await self._client.get("/api", params=params)
        except httpx.HTTPError as e:
            raise SABnzbdError(reach.explain(e, str(self._client.base_url), "SABnzbd", "http://192.168.1.10:8080")) from e
        if resp.status_code == 403 and "hostname" in resp.text.lower():
            # SABnzbd only answers names on its own list, and says so with a 403 that is easy to read
            # as a bad key. It is the address, not the key: the IP always works.
            host = self._client.base_url.host
            raise SABnzbdError(
                f"SABnzbd refused the name “{host}”: it only answers to names on its own list. Use its IP "
                f"address instead, or in SABnzbd add {host} under Config → Special → host_whitelist, save, "
                "and restart SABnzbd.")
        if resp.status_code in (401, 403):
            raise SABnzbdError("SABnzbd rejected the API key. Check it in Settings.")
        if resp.status_code >= 400:
            raise SABnzbdError(f"SABnzbd returned an error ({resp.status_code}).")
        try:
            data = resp.json()
        except ValueError:
            data = {"status": False, "error": resp.text.strip()[:200]}  # e.g. a plain "API Key Incorrect"
        if not isinstance(data, dict):
            raise SABnzbdError("That address doesn't look like SABnzbd. Check it in Settings.")
        if data.get("status") is False:
            error = data.get("error") or "request failed"
            if "api key" in error.lower():
                raise SABnzbdError("SABnzbd rejected the API key. Check it in Settings.")
            raise SABnzbdError(f"SABnzbd: {error}")
        return data

    async def version(self) -> str:
        # The queue needs a valid key, unlike mode=version, so this also checks the key.
        await self._call("queue", limit=0)
        return (await self._call("version")).get("version", "")

    async def add_nzb(self, nzb: bytes, filename: str, name: str, category: str = "") -> str:
        params = {"nzbname": name}
        if category:
            params["cat"] = category
        data = await self._call("addfile", files={"name": (filename, nzb, "application/x-nzb")}, **params)
        ids = data.get("nzo_ids") or []
        if not ids:
            raise SABnzbdError("SABnzbd didn't accept the NZB.")
        return ids[0]

    async def job(self, nzo_id: str) -> Job | None:
        """Where a job is, or None if SABnzbd has forgotten it."""
        queue = (await self._call("queue", nzo_ids=nzo_id)).get("queue", {})
        for slot in queue.get("slots", []):
            if slot.get("nzo_id") != nzo_id:
                continue
            status = slot.get("status", "")
            progress = float(slot.get("percentage") or 0)
            if queue.get("paused") or status == "Paused":
                return Job("paused", progress, detail="SABnzbd is paused")
            if status == "Downloading":
                speed = int(float(queue.get("kbpersec") or 0) * 1024)
                left = slot.get("timeleft") or ""
                detail = f"Downloading in SABnzbd · {left} left" if speed and left else "Downloading in SABnzbd"
                return Job("downloading", progress, speed, detail)
            return Job("queued", progress, detail=QUEUE_LABELS.get(status, "Queued in SABnzbd"))
        history = (await self._call("history", nzo_ids=nzo_id)).get("history", {})
        for slot in history.get("slots", []):
            if slot.get("nzo_id") != nzo_id:
                continue
            status = slot.get("status", "")
            if status == "Completed":
                return Job("completed", 100, path=slot.get("storage") or "")
            if status == "Failed":
                reason = slot.get("fail_message") or "no reason given"
                return Job("failed", error=f"SABnzbd couldn't finish this download: {reason}")
            return Job("processing", 100, detail=HISTORY_LABELS.get(status, "SABnzbd is finishing up…"))
        return None

    async def forget(self, nzo_id: str) -> None:
        """Clear a finished job from history once its files are in the library."""
        await self._call("history", name="delete", value=nzo_id)

    async def delete(self, nzo_id: str) -> None:
        """Remove a job from the queue (deleting its partial files) and from history."""
        await self._call("queue", name="delete", value=nzo_id, del_files=1)
        await self._call("history", name="delete", value=nzo_id)
