"""Settings, persisted as JSON in the data directory."""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from urllib.parse import urlparse

from . import languages, layout

DATA_DIR = Path(os.environ.get("NARRATARR_DATA", Path.home() / ".narratarr"))
SETTINGS_FILE = DATA_DIR / "settings.json"

# The app used to be called Audiobookeer; carry its data over on first run.
_OLD_DATA_DIR = Path.home() / ".audiobookeer"
if "NARRATARR_DATA" not in os.environ and not DATA_DIR.exists() and _OLD_DATA_DIR.is_dir():
    _OLD_DATA_DIR.rename(DATA_DIR)


@dataclass
class Settings:
    rd_api_key: str = ""
    library_dir: str = os.environ.get("NARRATARR_LIBRARY") or str(Path.home() / "Audiobooks")
    folder_template: str = "{author}/{title}"
    abb_base_url: str = "https://audiobookbay.lu"
    max_concurrent: int = 2
    delete_from_rd_after: bool = True
    ad_api_key: str = ""  # AllDebrid
    delete_from_ad_after: bool = True
    tb_api_key: str = ""  # TorBox
    delete_from_tb_after: bool = True
    pm_api_key: str = ""  # Premiumize
    delete_from_pm_after: bool = True
    smart_search: bool = True
    search_provider: str = "audiobookbay"  # or "brave"
    brave_api_key: str = ""
    audiobookshelf_url: str = ""
    audiobookshelf_token: str = ""
    audiobookshelf_library_id: str = ""
    prowlarr_url: str = ""
    prowlarr_api_key: str = ""
    prowlarr_categories: str = "3030"  # Newznab/Torznab "Audio/Audiobook"
    prowlarr_ebook_categories: str = "7020"  # "Books/EBook"
    torznab_url: str = ""
    torznab_api_key: str = ""
    torznab_categories: str = "3030"  # Newznab/Torznab "Audio/Audiobook"
    torznab_ebook_categories: str = "7020"  # "Books/EBook"
    sabnzbd_url: str = ""
    sabnzbd_api_key: str = ""
    sabnzbd_category: str = ""
    usenet_client: str = "sabnzbd"  # or "nzbget"
    nzbget_url: str = ""
    nzbget_username: str = ""
    nzbget_password: str = ""
    nzbget_category: str = ""
    torrent_client: str = "realdebrid"  # or "alldebrid" / "torbox" / "premiumize" / "qbittorrent" / "transmission" / "deluge"
    qbittorrent_url: str = ""
    qbittorrent_username: str = ""
    qbittorrent_password: str = ""
    qbittorrent_category: str = "audiobooks"
    qbittorrent_folder: str = ""  # where it saves, as qBittorrent sees it ("" = its own default)
    transmission_url: str = ""
    transmission_username: str = ""
    transmission_password: str = ""
    transmission_label: str = "audiobooks"
    transmission_folder: str = ""
    deluge_url: str = ""
    deluge_password: str = ""
    deluge_folder: str = ""
    # When a download client sees its downloads under a different path (e.g. another container).
    path_remote: str = ""
    path_local: str = ""
    # Books dropped in here are moved into the library automatically.
    import_folder: str = ""
    # Look for Wanted books on Prowlarr (and LibriVox) every few hours.
    auto_search: bool = True
    auto_search_hours: int = 12
    audible_region: str = "us"
    check_updates: bool = True  # one request a day to GitHub's releases API
    # On by default, and on for an existing settings file that predates the key: `load()` starts
    # from the dataclass defaults and only overwrites keys it finds, so an absent key means True.
    ratings_enabled: bool = True
    ratings_provider: str = "audnex"
    # Audible storefronts Audnex serves, in the order they are tried. `br` is not here: the API
    # rejects it as an unsupported code, and a code that can only fail should not be offered.
    audnex_regions: list[str] = field(default_factory=lambda: ["uk"])
    auth_local_bypass: bool = False  # skip sign-in for requests from this machine and the home network
    listener_approval: bool = True  # a listener's request waits for an admin before anything is searched or downloaded
    languages: str = ""  # the languages books should be in, e.g. "German, English" (blank: any)

    # Download clients, as a list. Empty means "not migrated yet": `load()` builds it from the flat
    # settings above the first time, so an existing install keeps working untouched.
    clients: list = field(default_factory=list)
    default_client: str = ""  # the client a grab falls through to when no route matches
    # Ordered routing rules, versioned so a future format change is a migration and not a repair.
    client_routes: dict = field(default_factory=lambda: {"version": 1, "rules": []})

    def public(self) -> dict:
        """Settings safe to send to the browser (secrets masked)."""
        d = asdict(self)
        for key in SECRETS:
            d[key] = _mask(d[key], key)
        from .downloaders import entry_from_dict

        d["clients"] = []
        for c in (asdict(self).get("clients") or []):
            if not isinstance(c, dict):
                continue
            parsed = entry_from_dict(c)
            d["clients"].append({
                **c,
                "label": parsed.label if parsed else c.get("type", ""),
                "configured": bool(parsed and parsed.configured),
                "password": _mask(c.get("password", ""), "x_password"),
                "api_key": _mask(c.get("api_key", ""), "x_api_key"),
            })
        d["rd_configured"] = bool(self.rd_api_key)
        d["ad_configured"] = bool(self.ad_api_key)
        d["tb_configured"] = bool(self.tb_api_key)
        d["pm_configured"] = bool(self.pm_api_key)
        d["brave_configured"] = bool(self.brave_api_key)
        d["prowlarr_configured"] = bool(self.prowlarr_url and self.prowlarr_api_key)
        d["torznab_configured"] = bool(self.torznab_url and self.torznab_api_key)
        d["indexer_configured"] = d["prowlarr_configured"] or d["torznab_configured"]
        d["sabnzbd_configured"] = bool(self.sabnzbd_url and self.sabnzbd_api_key)
        d["nzbget_configured"] = bool(self.nzbget_url)
        d["qbittorrent_configured"] = bool(self.qbittorrent_url)
        d["transmission_configured"] = bool(self.transmission_url)
        d["deluge_configured"] = bool(self.deluge_url)
        # A client in the list is what "configured" means now; the flat settings remain the answer
        # for an install that has not been migrated or was set up through the older API.
        from .downloaders import entries

        listed = entries(self)
        d["usenet_configured"] = any(e.delivery == "usenet" and e.configured for e in listed) \
            or d[f"{self.usenet_client}_configured"]
        short = {"realdebrid": "rd", "alldebrid": "ad", "torbox": "tb", "premiumize": "pm"}.get(self.torrent_client,
                                                                                                 self.torrent_client)
        d["torrent_configured"] = any(e.delivery == "torrent" and e.configured for e in listed) \
            or d[f"{short}_configured"]
        return d


SECRETS = (
    "rd_api_key", "ad_api_key", "tb_api_key", "pm_api_key", "audiobookshelf_token", "brave_api_key", "prowlarr_api_key", "sabnzbd_api_key",
    "nzbget_password", "qbittorrent_password", "transmission_password", "deluge_password", "torznab_api_key",
)
CHOICES = {
    "search_provider": ("audiobookbay", "brave"),
    "usenet_client": ("sabnzbd", "nzbget"),
    "torrent_client": ("realdebrid", "alldebrid", "torbox", "premiumize", "qbittorrent", "transmission", "deluge"),
    "audible_region": ("us", "uk", "ca", "au", "in", "de", "fr", "it", "es", "jp"),
}
# Each service's secrets. Changing the service's address to a different host clears them, so a
# saved key can never be sent to a machine the user didn't enter it for.
SERVICE_SECRETS = {
    "prowlarr_url": ("prowlarr_api_key",),
    "torznab_url": ("torznab_api_key",),
    "sabnzbd_url": ("sabnzbd_api_key",),
    "nzbget_url": ("nzbget_password",),
    "qbittorrent_url": ("qbittorrent_password",),
    "transmission_url": ("transmission_password",),
    "deluge_url": ("deluge_password",),
    "audiobookshelf_url": ("audiobookshelf_token",),
}
# Settings that were renamed; old settings files are read with the new names.
RENAMED = {"sabnzbd_remote_path": "path_remote", "sabnzbd_local_path": "path_local"}


def _mask(secret: str, key: str = "") -> str:
    if not secret:
        return ""
    if key.endswith("_password"):
        return "•" * 12  # the tail of an API key helps tell keys apart; a password's doesn't
    return "•" * 8 + secret[-4:]


_lock = threading.Lock()
_settings: Settings | None = None


def load() -> Settings:
    global _settings
    with _lock:
        if _settings is None:
            _settings = Settings()
            if SETTINGS_FILE.exists():
                raw = json.loads(SETTINGS_FILE.read_text())
                for old, new in RENAMED.items():
                    if old in raw and new not in raw:
                        raw[new] = raw[old]
                known = {f.name for f in fields(Settings)}
                for k, v in raw.items():
                    if k in known:
                        setattr(_settings, k, v)
            migrate_clients(_settings)
        return _settings


def update(changes: dict) -> Settings:
    """Apply changes and persist. Masked secrets sent back unchanged are ignored."""
    s = load()
    list_given = "clients" in changes
    prev_default = s.default_client
    touched_flat = any(k in _FLAT_PROTOCOL_FIELDS[p] for k in changes for p in _FLAT_PROTOCOL_FIELDS)
    with _lock:
        known = {f.name: f for f in fields(Settings)}
        fresh = {k for k, v in changes.items() if k in SECRETS and isinstance(v, str) and v and not v.startswith("•")}
        for k, v in changes.items():
            if k not in known:
                continue
            if k in SECRETS and isinstance(v, str) and v.startswith("•"):
                continue
            if k in SERVICE_SECRETS and isinstance(v, str):
                v = v.strip().rstrip("/")
                if v and not v.startswith(("http://", "https://")):
                    raise ValueError(f"The {k.split('_')[0]} address must start with http:// or https://")
                if _host(v) != _host(getattr(s, k)):
                    for secret in SERVICE_SECRETS[k]:
                        if secret not in fresh:
                            setattr(s, secret, "")
            if k == "audnex_regions":
                v = parse_regions(v)
            elif k == "clients":
                v = _merge_client_secrets(validate_clients(v), s.clients)
            elif k == "client_routes":
                from . import routing

                v = routing.validate(v, s)
            elif k == "default_client":
                v = str(v or "").strip().lower()
            if k == "max_concurrent":
                v = max(1, min(int(v), 8))
            elif k == "auto_search_hours":
                v = max(1, min(int(v), 168))
            elif k in CHOICES:
                v = v if v in CHOICES[k] else known[k].default
            elif k.endswith("_url") and isinstance(v, str):
                v = str(v).strip().rstrip("/")
            elif isinstance(v, str):
                v = v.strip()
            if k == "languages":
                v = ", ".join(languages.parse(str(v)))
            if k == "folder_template":
                v = v.strip("/") or "{author}/{title}"
                if bad := layout.unknown(v):
                    names = ", ".join("{" + n + "}" for n in layout.VARIABLES)
                    raise ValueError(f"The folder layout can't use {{{bad[0]}}}. You can use {names}.")
                if ".." in v.split("/"):
                    raise ValueError("The folder layout has to stay inside the library folder.")
            setattr(s, k, v)
        if touched_flat and not list_given:
            refresh_clients(s)  # a caller still using the flat fields
        # A removed client must not leave the default pointing at nothing.
        ids = [c["id"] for c in s.clients]
        if s.default_client not in ids:
            s.default_client = ids[0] if ids else ""
        from .downloaders import DELIVERY

        flat_for = {"usenet": "usenet_client", "torrent": "torrent_client"}
        if s.default_client != prev_default and "default_client" in changes:
            # Picking a default is picking the client for its protocol, which is what the flat choice
            # records and what the rest of the app (and routing's fall-through) reads.
            kind = next((c["type"] for c in s.clients if c["id"] == s.default_client), "")
            flat = flat_for.get(DELIVERY.get(kind, ""))
            if flat and kind in CHOICES[flat]:
                setattr(s, flat, kind)
        elif "default_client" not in changes:
            # And the other way round: choosing a client the old way (connecting AllDebrid, or
            # PUT {"torrent_client": ...}) makes it the default when the default was that protocol's.
            for proto, flat in flat_for.items():
                if flat not in changes:
                    continue
                current = next((c for c in s.clients if c["id"] == s.default_client), None)
                if current is None or DELIVERY.get(current["type"]) == proto:
                    pick = next((c["id"] for c in s.clients if c["type"] == getattr(s, flat)), "")
                    if pick:
                        s.default_client = pick
        if s.import_folder:
            lib, imp = Path(s.library_dir).expanduser().resolve(), Path(s.import_folder).expanduser().resolve()
            if lib == imp or lib in imp.parents or imp in lib.parents:
                raise ValueError("The import folder can't be inside the library folder, or contain it.")
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _write_private(SETTINGS_FILE, json.dumps(asdict(s), indent=2))
    return s


AUDNEX_REGIONS = ("us", "uk", "ca", "au", "in", "de", "fr", "it", "es", "jp")


def parse_regions(value) -> list[str]:
    """An ordered, de-duplicated list of storefront codes, or a ValueError naming the bad one.

    The order is the user's: availability is per item, so the list is a search order, not a set.
    A code the API does not serve is refused here rather than saved and quietly 400-ing later.
    """
    if isinstance(value, str):
        raw = [x for x in re.split(r"[,\s]+", value.strip()) if x]
    elif isinstance(value, (list, tuple)):
        raw = [str(x).strip() for x in value if str(x).strip()]
    else:
        raw = []
    out: list[str] = []
    for code in raw:
        code = code.lower()
        if code not in AUDNEX_REGIONS:
            raise ValueError(
                f"'{code}' isn't an Audible storefront Audnex serves. Use one of: {', '.join(AUDNEX_REGIONS)}.")
        if code not in out:
            out.append(code)
    return out or ["uk"]


# ---- download clients: one flat configuration per protocol, migrated into a list ------------

_FLAT = {
    "sabnzbd": ("sabnzbd_url", "sabnzbd_api_key", "sabnzbd_category", ""),
    "nzbget": ("nzbget_url", "nzbget_password", "nzbget_category", ""),
    "qbittorrent": ("qbittorrent_url", "qbittorrent_password", "qbittorrent_category", "qbittorrent_folder"),
    "transmission": ("transmission_url", "transmission_password", "transmission_label", "transmission_folder"),
    "deluge": ("deluge_url", "deluge_password", "", "deluge_folder"),
}
_DEBRID_KEYS = {"realdebrid": "rd_api_key", "alldebrid": "ad_api_key", "torbox": "tb_api_key", "premiumize": "pm_api_key"}


def _flat_entry(kind: str, s: Settings) -> dict:
    """The one client a flat setting describes, in the list's shape."""
    if kind in _DEBRID_KEYS:
        return {"id": kind, "type": kind, "api_key": getattr(s, _DEBRID_KEYS[kind], "")}
    url_key, secret_key, category_key, folder_key = _FLAT[kind]
    entry = {"id": kind, "type": kind, "url": getattr(s, url_key, "")}
    if secret_key:
        # NBZGet, qBittorrent, Transmission and Deluge all authenticate differently; put the value
        # where that client's own constructor reads it.
        if kind == "sabnzbd":
            entry["api_key"] = getattr(s, secret_key, "")
        else:
            entry["password"] = getattr(s, secret_key, "")
        if kind in ("nzbget", "qbittorrent", "transmission"):
            entry["username"] = getattr(s, f"{kind}_username", "")
    if category_key:
        entry["category"] = getattr(s, category_key, "")
    if folder_key:
        entry["save_path"] = getattr(s, folder_key, "")
    return entry


def migrate_clients(s: Settings) -> None:
    """Build the client list from the flat settings, once, when there is no list yet.

    One entry per protocol that is actually configured, because a working setup can have SABnzbd for
    Usenet and qBittorrent for torrents at the same time, and a single entry would silently drop
    one of them. That is the upgrade promise: every value is carried over, nothing is dropped.
    """
    if s.clients:
        return
    from .downloaders import entry_from_dict

    made: list[dict] = []
    for kind in (s.usenet_client, s.torrent_client):
        if not kind:
            continue
        entry = _flat_entry(kind, s)
        parsed = entry_from_dict(entry)
        if parsed is None or not parsed.configured:
            continue  # nothing configured for this protocol: do not invent an empty client
        made.append(entry)
    s.clients = made
    if made:
        torrent = next((e["id"] for e in made if e["id"] not in ("sabnzbd", "nzbget")), "")
        s.default_client = torrent or made[0]["id"]


# ---- the compatibility path, and when it can go ----------------------------------------------
#
# The flat client fields (`torrent_client`, `qbittorrent_url`, …) are still part of the settings
# model and of the public API: `PUT /api/settings {"torrent_client": "deluge"}` is a documented way
# to change where downloads go. Settings no longer renders them, but anything that still sets them
# has to keep working, so a flat client change is mirrored into the list.
#
# It goes when the flat fields themselves go. That is a separate change: delete them from Settings,
# teach the migration to read the old file's keys directly, and move the callers over.

_FLAT_PROTOCOL_FIELDS = {
    "usenet": ("usenet_client", "sabnzbd_url", "sabnzbd_api_key", "sabnzbd_category",
               "nzbget_url", "nzbget_username", "nzbget_password", "nzbget_category"),
    "torrent": ("torrent_client", "rd_api_key", "ad_api_key", "tb_api_key", "pm_api_key",
                "qbittorrent_url", "qbittorrent_username", "qbittorrent_password",
                "qbittorrent_category", "qbittorrent_folder",
                "transmission_url", "transmission_username", "transmission_password",
                "transmission_label", "transmission_folder",
                "deluge_url", "deluge_password", "deluge_folder"),
}


def refresh_clients(s: Settings) -> None:
    """Mirror a flat client change into the list, for callers that still use the flat fields."""
    from .downloaders import entry_from_dict

    current = {c.get("id"): c for c in (s.clients or []) if isinstance(c, dict)}
    for kind in (s.usenet_client, s.torrent_client):
        if not kind:
            continue
        entry = _flat_entry(kind, s)
        parsed = entry_from_dict(entry)
        if parsed is None:
            continue
        if entry["id"] in current:
            current[entry["id"]] = {**current[entry["id"]], **entry}
        elif parsed.configured:
            current[entry["id"]] = entry
    s.clients = list(current.values())
    ids = [c["id"] for c in s.clients]
    if s.default_client not in ids:
        torrent = next((c for c in ids if c not in ("sabnzbd", "nzbget")), "")
        s.default_client = torrent or (ids[0] if ids else "")


def validate_clients(value) -> list:
    """A list of client records, ids unique, types ones we can actually build."""
    from .downloaders import DELIVERY

    if isinstance(value, str):  # the settings form posts hidden inputs as text
        try:
            value = json.loads(value or "[]")
        except ValueError:
            raise ValueError("clients has to be a list of download clients")
    if not isinstance(value, list):
        raise ValueError("clients has to be a list of download clients")
    out, seen = [], set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("each download client has to be an object")
        kind = str(raw.get("type") or "").strip().lower()
        if kind not in DELIVERY:
            raise ValueError(f"'{kind or 'no type'}' isn't a download client Narratarr can talk to.")
        cid = str(raw.get("id") or kind).strip().lower() or kind
        if cid in seen:
            raise ValueError(f"two clients share the id '{cid}'")
        seen.add(cid)
        entry = {k: (v if isinstance(v, str) else "") for k, v in raw.items() if isinstance(v, str)}
        entry.update({"id": cid, "type": kind})
        for field_name in ("url", "username", "password", "api_key", "category", "save_path"):
            entry.setdefault(field_name, "")
        if entry["url"]:
            url = entry["url"].rstrip("/")
            if not url.startswith(("http://", "https://")):
                raise ValueError(f"The {entry['id']} address must start with http:// or https://")
            entry["url"] = url
        out.append(entry)
    return out


def _merge_client_secrets(new: list, old: list) -> list:
    """A masked secret posted back unchanged means "leave it alone", exactly as flat secrets do.

    And, exactly as flat secrets do, a saved password or key only ever stays with the machine it was
    entered for: if the client's address now points at a different host, or the entry has become a
    different kind of client, a masked secret is dropped rather than carried over. Otherwise changing
    an address would send the old password to the new machine on the next Test or grab.
    """
    by_id = {c.get("id"): c for c in old if isinstance(c, dict)}
    for entry in new:
        prev = by_id.get(entry["id"]) or {}
        same_place = prev.get("type") == entry.get("type") and _host(prev.get("url", "")) == _host(entry.get("url", ""))
        for key in ("password", "api_key"):
            if str(entry.get(key) or "").startswith("•"):
                entry[key] = prev.get(key, "") if same_place else ""
    return new


def _host(url: str) -> str:
    return urlparse(url).netloc.lower() if url else ""


def _write_private(path: Path, text: str) -> None:
    """Write a file only its owner can read, and never leave a half-written one behind."""
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    os.chmod(path, 0o600)
