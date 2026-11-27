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
    # A second library to rescan after an ebook download, for a layout that files ebooks apart with
    # {kind}. Empty: ebooks go to the library above, like everything else.
    audiobookshelf_ebook_library_id: str = ""
    # Hardcover (hardcover.app): books on your Want to Read shelf come to Wanted. Connected with Hardcover's
    # device sign-in, so no token is ever pasted in. The tokens never leave the server (see SERVER_ONLY).
    hardcover_access_token: str = ""
    hardcover_refresh_token: str = ""
    hardcover_token_expires: float = 0.0
    hardcover_user: str = ""  # the username it's connected as, to show in Settings
    hardcover_sync: bool = True  # look at the shelf now and then while connected
    hardcover_format: str = "audiobook"  # what a book from the shelf is wanted as: audiobook, ebook or both
    prowlarr_url: str = ""
    prowlarr_api_key: str = ""
    prowlarr_categories: str = "3030"  # Newznab/Torznab "Audio/Audiobook"
    prowlarr_ebook_categories: str = "7020"  # "Books/EBook"
    torznab_url: str = ""
    torznab_api_key: str = ""
    torznab_categories: str = "3030"  # Newznab/Torznab "Audio/Audiobook"
    torznab_ebook_categories: str = "7020"  # "Books/EBook"
    # Torznab indexers, as a list: one entry per endpoint, each with a name, so several can be used
    # at once and told apart in Discover and in routing. Empty means "not migrated yet": `load()`
    # builds it from the flat settings above the first time, exactly like the clients list, so an
    # existing install keeps working untouched.
    torznab_indexers: list = field(default_factory=list)
    # How long to wait before trying a book again when its download client was unreachable, in
    # minutes, oldest first. One entry per attempt; empty means a little outage fails the book, as
    # it did before. Four tries over about three quarters of an hour is the default because that
    # covers a container restart or a VPN reconnect without holding a download slot for ever.
    client_retry_minutes: str = "1, 5, 15, 30"
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
    # A release that searching finds on its own waits under Awaiting approval instead of downloading.
    found_needs_approval: bool = False
    # Look up a finished download's missing series, narrators and year on Audible before its folder is
    # named, so a layout that uses them gets them. Off by default: it's a request to Audible per book.
    fill_details_before_filing: bool = False
    # When a download fails because of the release itself (a dead torrent, a failed repair, no audio in
    # it), look for another release of the book and take that instead, like Sonarr and Radarr do.
    retry_failed_downloads: bool = True
    audible_region: str = "us"
    audible_extra_regions: list[str] = field(default_factory=list)  # more Audible stores people can search
    check_updates: bool = True  # one request a day to GitHub's releases API
    # On by default, and on for an existing settings file that predates the key: `load()` starts
    # from the dataclass defaults and only overwrites keys it finds, so an absent key means True.
    ratings_enabled: bool = True
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
    # An optional local torrent client (a client entry id) to use when the selected
    # debrid service says it does not have a release cached. Empty means off, which is
    # the default: sending a grab somewhere the user did not choose is not something to
    # do uninvited.
    debrid_fallback_client: str = ""
    # Ordered routing rules, versioned so a future format change is a migration and not a repair.
    client_routes: dict = field(default_factory=lambda: {"version": 1, "rules": []})

    def public(self) -> dict:
        """Settings safe to send to the browser (secrets masked, account tokens left out)."""
        d = asdict(self)
        for key in SECRETS:
            d[key] = _mask(d[key], key)
        for key in SERVER_ONLY:
            d.pop(key, None)
        d["hardcover_connected"] = bool(self.hardcover_refresh_token or self.hardcover_access_token)
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
        # The list is what "configured" means now; the flat settings remain the answer for an
        # install that has not been migrated or was set up through the older API.
        d["torznab_indexers"] = [
            {**e, "api_key": _mask(str(e.get("api_key") or ""), "x_api_key"),
             "configured": bool(e.get("url") and e.get("api_key"))}
            for e in (asdict(self).get("torznab_indexers") or []) if isinstance(e, dict)
        ]
        d["torznab_configured"] = any(e["configured"] for e in d["torznab_indexers"]) \
            or bool(self.torznab_url and self.torznab_api_key)
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


# Never sent to the browser, even masked, and never changed through the settings form: they act as the
# user's own account elsewhere. Only the code that connects and disconnects that account writes them.
SERVER_ONLY = ("hardcover_access_token", "hardcover_refresh_token", "hardcover_token_expires")
SECRETS = (
    "rd_api_key", "ad_api_key", "tb_api_key", "pm_api_key", "audiobookshelf_token", "brave_api_key", "prowlarr_api_key", "sabnzbd_api_key",
    "nzbget_password", "qbittorrent_password", "transmission_password", "deluge_password", "torznab_api_key",
)
CHOICES = {
    "hardcover_format": ("audiobook", "ebook", "both"),
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
                known = {f.name: f for f in fields(Settings)}
                for k, v in raw.items():
                    if k in known:
                        setattr(_settings, k, as_bool(v) if isinstance(known[k].default, bool) else v)
            migrate_clients(_settings)
            migrate_torznab(_settings)
        return _settings


def as_bool(v) -> bool:
    """A switch's value. Older pages sent a ticked box as "on", which was saved as text; read it as on."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "on", "yes")
    return bool(v)


def update(changes: dict) -> Settings:
    """Apply changes and persist. Masked secrets sent back unchanged are ignored."""
    s = load()
    list_given = "clients" in changes
    indexers_given = "torznab_indexers" in changes
    prev_default = s.default_client
    touched_flat = any(k in _FLAT_PROTOCOL_FIELDS[p] for k in changes for p in _FLAT_PROTOCOL_FIELDS)
    touched_torznab_flat = any(k in ("torznab_url", "torznab_api_key", "torznab_categories",
                                     "torznab_ebook_categories") for k in changes)
    with _lock:
        known = {f.name: f for f in fields(Settings)}
        fresh = {k for k, v in changes.items() if k in SECRETS and isinstance(v, str) and v and not v.startswith("•")}
        for k, v in changes.items():
            if k not in known:
                continue
            if k in SECRETS and isinstance(v, str) and v.startswith("•"):
                continue
            if isinstance(known[k].default, bool):
                v = as_bool(v)
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
            elif k == "audible_extra_regions":
                v = parse_audible_stores(v)
            elif k == "clients":
                v = _merge_client_secrets(validate_clients(v), s.clients)
            elif k == "torznab_indexers":
                v = _merge_torznab_secrets(validate_torznab_indexers(v), s.torznab_indexers)
            elif k == "client_routes":
                from . import routing

                v = routing.validate(v, s)
            elif k in ("default_client", "debrid_fallback_client"):
                v = str(v or "").strip().lower()
            elif k in ("audiobookshelf_library_id", "audiobookshelf_ebook_library_id"):
                # Sent to Audiobookshelf as part of a URL path, so only an id's own characters.
                v = str(v or "").strip()
                if v and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", v):
                    raise ValueError("That isn't an Audiobookshelf library id.")
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
            if k == "client_retry_minutes":
                v = parse_retry_minutes(v)
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
        if touched_torznab_flat and not indexers_given:
            refresh_torznab(s)  # likewise for the old Torznab fields
        # A removed client must not leave the default pointing at nothing.
        ids = [c["id"] for c in s.clients]
        if s.default_client not in ids:
            s.default_client = ids[0] if ids else ""
        from .downloaders import DEBRID as DEBRID_TYPES, DELIVERY

        # The uncached fallback may only name a local torrent client that exists. Anything else
        # (a removed client, a cloud service, a Usenet client) turns it off rather than misroutes.
        local_torrent = {c["id"] for c in s.clients
                         if DELIVERY.get(c["type"]) == "torrent" and c["type"] not in DEBRID_TYPES}
        if s.debrid_fallback_client not in local_torrent:
            s.debrid_fallback_client = ""

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


def parse_retry_minutes(value) -> str:
    """The waits between attempts after an unreachable client, as the user typed them.

    Stored normalised ("1, 5, 15, 30") so the setting reads the same however it was entered. An
    empty string is a real choice: no retry at all, which is what Narratarr did before.
    """
    if isinstance(value, (list, tuple)):
        raw = [str(x).strip() for x in value if str(x).strip()]
    else:
        raw = [x for x in re.split(r"[,\s]+", str(value or "").strip()) if x]
    out: list[int] = []
    for item in raw:
        try:
            minutes = int(float(item))
        except ValueError:
            raise ValueError(f"'{item}' isn't a number of minutes.") from None
        if minutes < 1:
            raise ValueError("Wait at least a minute before trying again.")
        if minutes > 1440:
            raise ValueError("Wait no more than a day (1440 minutes) between attempts.")
        out.append(minutes)
    if len(out) > 10:
        raise ValueError("Ten attempts is the most a book can be given.")
    return ", ".join(str(m) for m in out)


def retry_delays(s: Settings | None = None) -> list[int]:
    """The waits, in seconds, before each retry after an unreachable download client."""
    s = s or load()
    minutes = re.split(r"[,\s]+", (s.client_retry_minutes or "").strip())
    return [int(m) * 60 for m in minutes if m.isdigit()]


def parse_audible_stores(value) -> list[str]:
    """The extra Audible stores people may search, in the admin's order. Codes from CHOICES["audible_region"]."""
    if isinstance(value, str):
        raw = [x for x in re.split(r"[,\s]+", value.strip()) if x]
    elif isinstance(value, (list, tuple)):
        raw = [str(x).strip() for x in value if str(x).strip()]
    else:
        raw = []
    out: list[str] = []
    for code in raw:
        code = code.lower()
        if code not in CHOICES["audible_region"]:
            raise ValueError(f"'{code}' isn't an Audible store. Use one of: {', '.join(CHOICES['audible_region'])}.")
        if code not in out:
            out.append(code)
    return out


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


# ---- Torznab indexers: one flat configuration migrated into a list ---------------------------

TORZNAB_DEFAULTS = {"categories": "3030", "ebook_categories": "7020"}


def migrate_torznab(s: Settings) -> None:
    """Build the indexer list from the flat settings, once, when there is no list yet.

    The upgrade promise is the same as for download clients: an address and key that worked keep
    working, under the name the old screen used, so nothing has to be re-entered after updating.
    """
    if s.torznab_indexers or not (s.torznab_url and s.torznab_api_key):
        return
    s.torznab_indexers = [{
        "id": "torznab",
        "name": "Torznab",
        "url": s.torznab_url,
        "api_key": s.torznab_api_key,
        "categories": s.torznab_categories or TORZNAB_DEFAULTS["categories"],
        "ebook_categories": s.torznab_ebook_categories or TORZNAB_DEFAULTS["ebook_categories"],
    }]


def torznab_entries(s: Settings) -> list[dict]:
    """The configured Torznab indexers, in the user's order.

    Falls back to the flat settings for an install whose list has not been written yet, so every
    caller has one answer and no caller has to know about the old shape.
    """
    if s.torznab_indexers:
        return [e for e in s.torznab_indexers if isinstance(e, dict) and e.get("url") and e.get("api_key")]
    if s.torznab_url and s.torznab_api_key:
        return [{
            "id": "torznab", "name": "Torznab", "url": s.torznab_url, "api_key": s.torznab_api_key,
            "categories": s.torznab_categories or TORZNAB_DEFAULTS["categories"],
            "ebook_categories": s.torznab_ebook_categories or TORZNAB_DEFAULTS["ebook_categories"],
        }]
    return []


def _torznab_id(name: str, taken: set[str]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-") or "torznab"
    cid, n = slug, 2
    while cid in taken:
        cid, n = f"{slug}-{n}", n + 1
    return cid


def validate_torznab_indexers(value) -> list:
    """A list of indexer records. A card left blank is dropped rather than saved as an empty indexer."""
    if isinstance(value, str):  # the settings form posts hidden inputs as text
        try:
            value = json.loads(value or "[]")
        except ValueError:
            raise ValueError("torznab_indexers has to be a list of indexers")
    if not isinstance(value, list):
        raise ValueError("torznab_indexers has to be a list of indexers")
    out, taken = [], set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("each Torznab indexer has to be an object")
        url = str(raw.get("url") or "").strip().rstrip("/")
        api_key = str(raw.get("api_key") or "").strip()
        if not url and not api_key:
            continue
        if url and not url.startswith(("http://", "https://")):
            raise ValueError("A Torznab address must start with http:// or https://")
        # Name it after its host when the user hasn't: two indexers are only useful if the picker
        # can tell them apart, and "Torznab" twice tells nobody anything.
        name = str(raw.get("name") or "").strip() or (_host(url) or "Torznab")
        cid = str(raw.get("id") or "").strip().lower()
        if not cid or cid in taken:
            cid = _torznab_id(name, taken)
        taken.add(cid)
        entry = {
            "id": cid,
            "name": name[:60],
            "url": url,
            "api_key": api_key,
            "categories": str(raw.get("categories") or TORZNAB_DEFAULTS["categories"]).strip(),
            "ebook_categories": str(raw.get("ebook_categories") or TORZNAB_DEFAULTS["ebook_categories"]).strip(),
        }
        out.append(entry)
    return out


def _merge_torznab_secrets(new: list, old: list) -> list:
    """A masked key posted back unchanged means "leave it alone", exactly as the flat secret does.

    And a saved key only stays with the address it was entered for: changing an indexer's host
    drops the masked key rather than sending it to a different machine.
    """
    by_id = {e.get("id"): e for e in old if isinstance(e, dict)}
    for entry in new:
        prev = by_id.get(entry["id"]) or {}
        same_place = _host(prev.get("url", "")) == _host(entry.get("url", ""))
        if str(entry.get("api_key") or "").startswith("•"):
            entry["api_key"] = prev.get("api_key", "") if same_place else ""
    return new


def refresh_torznab(s: Settings) -> None:
    """Mirror the flat Torznab fields into the list entry they describe.

    The flat fields are still part of the public API (`PUT /api/settings {"torznab_url": …}`), so a
    caller that only sets them has to keep working. Clearing both removes that entry again; an
    indexer added under the new screen keeps its own id and is never touched here.
    """
    entries = [e for e in (s.torznab_indexers or []) if isinstance(e, dict)]
    has = any(e.get("id") == "torznab" for e in entries)
    if not (s.torznab_url and s.torznab_api_key):
        if has:  # the flat indexer was switched off, so its entry goes too
            s.torznab_indexers = [e for e in entries if e.get("id") != "torznab"]
        return
    entry = {
        "id": "torznab", "name": "Torznab", "url": s.torznab_url, "api_key": s.torznab_api_key,
        "categories": s.torznab_categories or TORZNAB_DEFAULTS["categories"],
        "ebook_categories": s.torznab_ebook_categories or TORZNAB_DEFAULTS["ebook_categories"],
    }
    if has:
        # Keep the name it already has: a plain flat-field change is not a rename.
        s.torznab_indexers = [
            {**e, **{k: v for k, v in entry.items() if k != "name"}} if e.get("id") == "torznab" else e
            for e in entries
        ]
    else:
        s.torznab_indexers = entries + [entry]


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
