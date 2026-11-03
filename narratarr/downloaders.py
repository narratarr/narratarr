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
DEBRID = ("realdebrid", "alldebrid", "torbox", "premiumize")  # cloud services: they fetch the torrent, we download the files
TORRENT_CLIENTS = {
    "realdebrid": "Real-Debrid", "alldebrid": "AllDebrid", "torbox": "TorBox", "premiumize": "Premiumize",
    "qbittorrent": "qBittorrent", "transmission": "Transmission", "deluge": "Deluge",
}


# ---- client entries ------------------------------------------------------------------------
#
# A client used to be inferred from flat settings: `torrent_client` said which one, and its fields
# sat next to it. That allows exactly one client per protocol. Routing needs a list, so a client is
# now a record with an id, and the flat settings become a one-time migration into it.
#
# DELIVERY decides which pipeline a grab takes: usenet hands an NZB to a downloader, torrent hands a
# magnet or .torrent to one, and a debrid service fetches the torrent and gives us the files.

DELIVERY = {
    "sabnzbd": "usenet", "nzbget": "usenet",
    "qbittorrent": "torrent", "transmission": "torrent", "deluge": "torrent",
    "realdebrid": "torrent", "alldebrid": "torrent", "torbox": "torrent", "premiumize": "torrent",
}
LABELS = {**USENET_CLIENTS, **TORRENT_CLIENTS}
# Where a debrid service keeps its "delete after" preference, so one client entry can differ from another.
DELETE_SETTINGS = {
    "realdebrid": "delete_from_rd_after", "alldebrid": "delete_from_ad_after",
    "torbox": "delete_from_tb_after", "premiumize": "delete_from_pm_after",
}


@dataclass
class ClientEntry:
    """One download client the user has configured."""

    id: str
    type: str
    url: str = ""
    username: str = ""
    password: str = ""
    api_key: str = ""
    category: str = ""
    save_path: str = ""  # as the client sees it; "" means the client's own default

    @property
    def label(self) -> str:
        return LABELS.get(self.type, self.type)

    @property
    def delivery(self) -> str:
        return DELIVERY.get(self.type, "")

    @property
    def is_debrid(self) -> bool:
        return self.type in DEBRID

    @property
    def configured(self) -> bool:
        """Enough to build a client. A missing url is the usual reason nothing works."""
        if self.type == "sabnzbd":
            return bool(self.url and self.api_key)
        if self.type in DEBRID:
            return bool(self.api_key)
        return bool(self.url)

    def public(self) -> dict:
        return {
            "id": self.id, "type": self.type, "label": self.label, "delivery": self.delivery,
            "url": self.url, "username": self.username,
            "password": config._mask(self.password, "x_password"), "api_key": config._mask(self.api_key, "x_api_key"),
            "category": self.category, "save_path": self.save_path,
            "configured": self.configured,
        }


def entry_from_dict(raw: dict) -> ClientEntry | None:
    """A stored client record, or None if it names a type Narratarr does not speak."""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("type") or "").strip().lower()
    if kind not in DELIVERY:
        return None
    return ClientEntry(
        id=str(raw.get("id") or kind).strip() or kind,
        type=kind,
        url=str(raw.get("url") or "").strip(),
        username=str(raw.get("username") or ""),
        password=str(raw.get("password") or ""),
        api_key=str(raw.get("api_key") or ""),
        category=str(raw.get("category") or ""),
        save_path=str(raw.get("save_path") or ""),
    )


def entries(settings: config.Settings) -> list[ClientEntry]:
    return [e for e in (entry_from_dict(r) for r in (settings.clients or [])) if e]


def client_for(entry: ClientEntry, transport: httpx.AsyncBaseTransport | None = None):
    """The client object behind an entry."""
    if entry.type == "sabnzbd":
        from .sabnzbd import SABnzbd

        return SABnzbd(entry.url, entry.api_key, transport=transport)
    if entry.type == "nzbget":
        from .nzbget import NZBGet

        return NZBGet(entry.url, entry.username, entry.password, transport=transport)
    if entry.type == "qbittorrent":
        from .qbittorrent import QBittorrent

        return QBittorrent(entry.url, entry.username, entry.password, transport=transport)
    if entry.type == "transmission":
        from .transmission import Transmission

        return Transmission(entry.url, entry.username, entry.password, transport=transport)
    if entry.type == "deluge":
        from .deluge import Deluge

        return Deluge(entry.url, entry.password, transport=transport)
    if entry.type == "alldebrid":
        from .alldebrid import AllDebrid

        return AllDebrid(entry.api_key, transport=transport)
    if entry.type == "torbox":
        from .torbox import TorBox

        return TorBox(entry.api_key, transport=transport)
    if entry.type == "premiumize":
        from .premiumize import Premiumize

        return Premiumize(entry.api_key, transport=transport)
    if entry.type == "realdebrid":
        from .realdebrid import RealDebrid

        return RealDebrid(entry.api_key, transport=transport)
    raise ClientError(f"{entry.label} isn't a client Narratarr can hand a release to.")


def _any(settings: config.Settings, delivery: str) -> bool:
    return any(e.delivery == delivery and e.configured for e in entries(settings))


def usenet_configured(s: config.Settings, name: str | None = None) -> bool:
    if name is None and _any(s, "usenet"):
        return True
    name = name or s.usenet_client
    if name == "nzbget":
        return bool(s.nzbget_url)
    return bool(s.sabnzbd_url and s.sabnzbd_api_key)


def torrent_configured(s: config.Settings, name: str | None = None) -> bool:
    if name is None and _any(s, "torrent"):
        return True
    name = name or s.torrent_client
    if name == "qbittorrent":
        return bool(s.qbittorrent_url)
    if name == "transmission":
        return bool(s.transmission_url)
    if name == "deluge":
        return bool(s.deluge_url)
    if name == "alldebrid":
        return bool(s.ad_api_key)
    if name == "torbox":
        return bool(s.tb_api_key)
    if name == "premiumize":
        return bool(s.pm_api_key)
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
    """The cloud service torrents go through: the one chosen, or Real-Debrid."""
    if s.torrent_client == "alldebrid":
        from .alldebrid import AllDebrid

        return AllDebrid(s.ad_api_key, transport=transport)
    if s.torrent_client == "torbox":
        from .torbox import TorBox

        return TorBox(s.tb_api_key, transport=transport)
    if s.torrent_client == "premiumize":
        from .premiumize import Premiumize

        return Premiumize(s.pm_api_key, transport=transport)
    from .realdebrid import RealDebrid

    return RealDebrid(s.rd_api_key, transport=transport)


def category_for(s: config.Settings, name: str) -> str:
    """The category (or label) a client files Narratarr's downloads under."""
    return {
        "sabnzbd": s.sabnzbd_category, "nzbget": s.nzbget_category,
        "qbittorrent": s.qbittorrent_category, "transmission": s.transmission_label,
    }.get(name, "")


def folder_for(s: config.Settings, name: str) -> str:
    """Where a torrent client should save Narratarr's downloads, as the client sees it ("" = its default)."""
    return {"qbittorrent": s.qbittorrent_folder, "transmission": s.transmission_folder, "deluge": s.deluge_folder}.get(name, "")
