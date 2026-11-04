"""Minimal async client for NZBGet's JSON-RPC API.

Docs: https://nzbget.com/documentation/api/
"""

from __future__ import annotations

import base64

import httpx

from . import reach
from .downloaders import ClientError, Job

QUEUE_LABELS = {
    "QUEUED": "Queued in NZBGet",
    "FETCHING": "NZBGet is reading the NZB…",
    "PP_QUEUED": "Waiting for NZBGet to unpack it…",
    "LOADING_PARS": "NZBGet is checking the files…",
    "VERIFYING_SOURCES": "NZBGet is checking the files…",
    "REPAIRING": "NZBGet is repairing the files…",
    "VERIFYING_REPAIRED": "NZBGet is checking the repair…",
    "RENAMING": "NZBGet is renaming the files…",
    "UNPACKING": "NZBGet is unpacking…",
    "MOVING": "NZBGet is moving the files…",
    "EXECUTING_SCRIPT": "NZBGet is running its post-processing script…",
    "PP_FINISHED": "NZBGet is finishing up…",
}


class NZBGetError(ClientError):
    pass


class NZBGet:
    label = "NZBGet"

    def __init__(self, url: str, username: str = "", password: str = "", transport=None):
        if not url:
            raise NZBGetError("Add your NZBGet address in Settings first.")
        auth = (username, password) if username or password else None
        self._client = httpx.AsyncClient(base_url=url.rstrip("/"), auth=auth, timeout=30, transport=transport)

    async def __aenter__(self) -> NZBGet:
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def _call(self, method: str, *params):
        try:
            resp = await self._client.post("/jsonrpc", json={"method": method, "params": list(params), "id": 1})
        except httpx.HTTPError as e:
            raise NZBGetError(reach.explain(e, str(self._client.base_url), "NZBGet", "http://192.168.1.10:6789")) from e
        if resp.status_code in (401, 403):
            raise NZBGetError("NZBGet rejected the username or password. Check them in Settings.")
        if resp.status_code >= 400:
            raise NZBGetError(f"NZBGet returned an error ({resp.status_code}).")
        try:
            data = resp.json()
        except ValueError:
            raise NZBGetError("That address doesn't look like NZBGet. Check it in Settings.")
        if data.get("error"):
            raise NZBGetError(f"NZBGet: {(data['error'] or {}).get('message', 'request failed')}")
        return data.get("result")

    async def version(self) -> str:
        return str(await self._call("version"))

    async def add_nzb(self, nzb: bytes, filename: str, name: str, category: str = "") -> str:
        content = base64.b64encode(nzb).decode()
        nzb_id = await self._call("append", filename, content, category, 0, False, False, "", 0, "SCORE", [])
        if not isinstance(nzb_id, int) or nzb_id <= 0:
            raise NZBGetError("NZBGet didn't accept the NZB.")
        return str(nzb_id)

    async def job(self, job_id: str) -> Job | None:
        nzb_id = int(job_id)
        for g in await self._call("listgroups", 0) or []:
            if g.get("NZBID") != nzb_id:
                continue
            status = g.get("Status", "")
            total = float(g.get("FileSizeMB") or 0)
            left = float(g.get("RemainingSizeMB") or 0)
            progress = (total - left) * 100 / total if total else 0
            if status == "PAUSED":
                return Job("paused", progress, detail="Paused in NZBGet")
            if status == "DOWNLOADING":
                info = await self._call("status") or {}
                if info.get("DownloadPaused"):
                    return Job("paused", progress, detail="NZBGet is paused")
                return Job("downloading", progress, int(info.get("DownloadRate") or 0), "Downloading in NZBGet")
            if status in ("QUEUED", "FETCHING"):
                return Job("queued", progress, detail=QUEUE_LABELS[status])
            return Job("processing", 100, detail=QUEUE_LABELS.get(status, "NZBGet is finishing up…"))
        for h in await self._call("history", False) or []:
            if (h.get("NZBID") or h.get("ID")) != nzb_id:
                continue
            status = h.get("Status", "")
            if status.startswith("SUCCESS") or status.startswith("WARNING/SCRIPT"):
                return Job("completed", 100, path=h.get("FinalDir") or h.get("DestDir") or "")
            if status.startswith("DELETED"):
                return None
            return Job("failed", error=f"NZBGet couldn't finish this download ({status.lower()}).")
        return None

    async def forget(self, job_id: str) -> None:
        await self._call("editqueue", "HistoryDelete", "", [int(job_id)])

    async def delete(self, job_id: str) -> None:
        await self._call("editqueue", "GroupFinalDelete", "", [int(job_id)])
        await self._call("editqueue", "HistoryFinalDelete", "", [int(job_id)])
