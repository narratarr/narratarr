"""Minimal async client for Deluge's Web JSON-RPC API.

Docs: https://deluge.readthedocs.io/en/latest/reference/webapi.html

Two things about this API are worth knowing before reading the code:

* A failed call is **not** an HTTP error. Deluge answers ``200`` with
  ``{"result": ..., "error": {...}}``, so the status code alone tells you nothing.
* The web UI is a proxy in front of the daemon. Until ``web.connected`` is true every
  ``core.*`` call fails with an unhelpful error, so the connection is established first.
"""

from __future__ import annotations

import base64
import posixpath

import httpx

from .downloaders import ClientError, Job

# get_torrent_status returns only the keys asked for.
KEYS = [
    "name", "state", "progress", "download_payload_rate", "download_location", "is_finished", "message",
]
CHECKING = {"Allocating", "Checking", "Moving"}


class DelugeError(ClientError):
    pass


class Deluge:
    label = "Deluge"

    def __init__(self, url: str, password: str = "", transport=None):
        if not url:
            raise DelugeError("Add your Deluge address in Settings first.")
        base = url.rstrip("/")
        self._rpc = base if base.endswith("/json") else f"{base}/json"
        # The session is a cookie; the client carries it once auth.login has set it.
        self._client = httpx.AsyncClient(timeout=30, transport=transport)
        self._password = password
        self._ready = False
        self._seq = 0
        self._labels = False

    async def __aenter__(self) -> Deluge:
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def _call(self, method: str, params: list | None = None):
        self._seq += 1
        body = {"method": method, "params": params or [], "id": self._seq}
        try:
            resp = await self._client.post(self._rpc, json=body)
        except httpx.HTTPError:
            raise DelugeError("Couldn't reach Deluge. Check the address in Settings.")
        if resp.status_code == 401:
            raise DelugeError("Deluge rejected the password. Check it in Settings.")
        if resp.status_code == 404:
            raise DelugeError("That address doesn't look like Deluge's web UI. Check it in Settings.")
        if resp.status_code >= 400:
            raise DelugeError(f"Deluge returned an error ({resp.status_code}). Check the address in Settings.")
        try:
            data = resp.json()
        except ValueError:
            raise DelugeError("That address doesn't look like Deluge's web UI. Check it in Settings.")
        error = data.get("error")
        if error:  # a 200 can still be a failure
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise DelugeError(f"Deluge: {message or 'request failed'}")
        return data.get("result")

    async def _login(self) -> None:
        if self._ready:
            return
        if not await self._call("auth.login", [self._password]):
            raise DelugeError("Deluge rejected the password. Check it in Settings.")
        if not await self._call("web.connected"):
            await self._connect_daemon()
        self._ready = True
        # core.get_enabled_plugins returns the daemon's plugin list, and it is what governs whether
        # the label option is honoured. The web method is `web.get_plugins` (a dict), and a bare
        # `get_plugins` — which the reference docs list — is "Unknown method" on a real server.
        plugins = await self._call("core.get_enabled_plugins") or []
        self._labels = any(str(p).lower() == "label" for p in plugins)

    async def _connect_daemon(self) -> None:
        """Point Deluge's web UI at its daemon; the web UI is a proxy and does nothing until it is."""
        hosts = await self._call("web.get_hosts") or []
        if hosts:
            await self._call("web.connect", [hosts[0][0]])
        if not await self._call("web.connected"):
            raise DelugeError(
                "Deluge's web UI isn't connected to its daemon. Start the daemon and connect it in Deluge's "
                "Connection Manager, then test again."
            )

    async def version(self) -> str:
        await self._login()
        return str(await self._call("daemon.get_version") or "")

    async def add(self, kind: str, payload: str | bytes, tag: str = "", category: str = "", folder: str = "") -> str:
        await self._login()
        options: dict = {}
        if folder:
            options["download_location"] = folder
        if kind == "magnet":
            found = await self._call("core.add_torrent_magnet", [payload, options])
        else:
            dump = base64.b64encode(payload).decode()
            found = await self._call("core.add_torrent_file", ["book.torrent", dump, options])
        if not found:
            raise DelugeError("Deluge didn't accept the torrent.")
        torrent_hash = str(found).lower()  # Deluge reports hashes in upper case; ours are lower
        if tag and self._labels:
            await self._attach_label(torrent_hash, tag)
        return torrent_hash

    async def _attach_label(self, torrent_hash: str, tag: str) -> None:
        """Label the torrent so we can tell it apart from the user's own.

        A label is not a torrent option in Deluge 2.x — passing ``options["label"]`` is accepted and
        silently dropped. Labels come from the Label plugin, have their own RPC methods, and can only
        be set on a torrent that already exists.
        """
        try:
            await self._call("label.add", [tag])
        except DelugeError as e:
            if "already exists" not in str(e).lower():  # created on an earlier run; anything else is real
                raise
        await self._call("label.set_torrent", [torrent_hash, tag])

    async def job(self, torrent_hash: str) -> Job | None:
        await self._login()
        t = await self._call("core.get_torrent_status", [torrent_hash, KEYS])
        if not t:
            return None
        state = str(t.get("state") or "")
        progress = float(t.get("progress") or 0)  # already a percentage
        if state == "Error":
            return Job("failed", error=f"Deluge: {t.get('message') or 'local error'}")
        if (t.get("is_finished") or progress >= 100) and state not in CHECKING:
            path = posixpath.join(t.get("download_location") or "", t.get("name") or "")
            return Job("completed", 100, path=path)
        if state in CHECKING:
            return Job("processing", progress, detail="Deluge is checking the files…")
        if state == "Paused":
            return Job("paused", progress, detail="Paused in Deluge")
        if state == "Queued":
            return Job("queued", progress, detail="Queued in Deluge")
        speed = int(t.get("download_payload_rate") or 0)
        detail = "Waiting for peers…" if not speed else "Downloading in Deluge"
        return Job("downloading", progress, speed, detail)

    async def delete(self, torrent_hash: str, files: bool = True, tag: str = "") -> None:
        """Remove a torrent, but only one Narratarr added for this book (it carries the book's label).

        A torrent the user already had, or that another book is waiting on, is left alone.
        """
        await self._login()
        label = str((await self._call("core.get_torrent_status", [torrent_hash, ["label"]]) or {}).get("label") or "")
        if tag and tag != label:
            return
        files = files and label.startswith("narratarr-")
        await self._call("core.remove_torrent", [torrent_hash, files])
