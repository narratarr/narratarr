"""Download clients the user runs themselves: Usenet (SABnzbd, NZBGet) and torrents (qBittorrent, Transmission, Deluge).

Each client reports a job in the same shape, so the download pipeline doesn't
care which one is in use. Real-Debrid and AllDebrid are handled separately (they're
services we download *from*, not clients that write files somewhere for us).
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from . import config


@dataclass
class Job:
    state: str  # "queued" | "downloading" | "paused" | "processing" | "completed" | "failed"
    progress: float = 0.0  # 0-100
    speed: int = 0  # bytes/s
    detail: str = ""
    path: str = ""  # where the finished files are, as the client sees them
    error: str = ""


class ClientError(Exception):
    pass


USENET_CLIENTS = {"sabnzbd": "SABnzbd", "nzbget": "NZBGet"}
DEBRID = ("realdebrid", "alldebrid")  # cloud services: they fetch the torrent, we download the files
TORRENT_CLIENTS = {
    "realdebrid": "Real-Debrid", "alldebrid": "AllDebrid", "qbittorrent": "qBittorrent", "transmission": "Transmission", "deluge": "Deluge",
}


def usenet_configured(s: config.Settings, name: str | None = None) -> bool:
    name = name or s.usenet_client
    if name == "nzbget":
        return bool(s.nzbget_url)
    return bool(s.sabnzbd_url and s.sabnzbd_api_key)


def torrent_configured(s: config.Settings, name: str | None = None) -> bool:
    name = name or s.torrent_client
    if name == "qbittorrent":
        return bool(s.qbittorrent_url)
    if name == "transmission":
        return bool(s.transmission_url)
    if name == "deluge":
        return bool(s.deluge_url)
    if name == "alldebrid":
        return bool(s.ad_api_key)
    return bool(s.rd_api_key)


def usenet_client(s: config.Settings, transport: httpx.AsyncBaseTransport | None = None, name: str | None = None):
    name = name or s.usenet_client
    if name == "nzbget":
        from .nzbget import NZBGet

        return NZBGet(s.nzbget_url, s.nzbget_username, s.nzbget_password, transport=transport)
    from .sabnzbd import SABnzbd

    return SABnzbd(s.sabnzbd_url, s.sabnzbd_api_key, transport=transport)


def torrent_client(s: config.Settings, transport: httpx.AsyncBaseTransport | None = None, name: str | None = None):
    name = name or s.torrent_client
    if name == "qbittorrent":
        from .qbittorrent import QBittorrent

        return QBittorrent(s.qbittorrent_url, s.qbittorrent_username, s.qbittorrent_password, transport=transport)
    if name == "transmission":
        from .transmission import Transmission

        return Transmission(s.transmission_url, s.transmission_username, s.transmission_password, transport=transport)
    if name == "deluge":
        from .deluge import Deluge

        return Deluge(s.deluge_url, s.deluge_password, transport=transport)
    raise ClientError(f"{TORRENT_CLIENTS.get(name, name)} isn't a torrent client Narratarr can hand torrents to.")


def debrid_client(s: config.Settings, transport: httpx.AsyncBaseTransport | None = None):
    """The cloud service torrents go through: AllDebrid if chosen, otherwise Real-Debrid."""
    if s.torrent_client == "alldebrid":
        from .alldebrid import AllDebrid

        return AllDebrid(s.ad_api_key, transport=transport)
    from .realdebrid import RealDebrid

    return RealDebrid(s.rd_api_key, transport=transport)


def category_for(s: config.Settings, name: str) -> str:
    return {"sabnzbd": s.sabnzbd_category, "nzbget": s.nzbget_category, "qbittorrent": s.qbittorrent_category}.get(name, "")
