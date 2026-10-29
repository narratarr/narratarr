"""Settings, persisted as JSON in the data directory."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from urllib.parse import urlparse

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
    smart_search: bool = True
    search_provider: str = "audiobookbay"  # or "brave"
    brave_api_key: str = ""
    audiobookshelf_url: str = ""
    audiobookshelf_token: str = ""
    audiobookshelf_library_id: str = ""
    prowlarr_url: str = ""
    prowlarr_api_key: str = ""
    prowlarr_categories: str = "3030"  # Newznab/Torznab "Audio/Audiobook"
    torznab_url: str = ""
    torznab_api_key: str = ""
    torznab_categories: str = "3030"  # Newznab/Torznab "Audio/Audiobook"
    sabnzbd_url: str = ""
    sabnzbd_api_key: str = ""
    sabnzbd_category: str = ""
    usenet_client: str = "sabnzbd"  # or "nzbget"
    nzbget_url: str = ""
    nzbget_username: str = ""
    nzbget_password: str = ""
    nzbget_category: str = ""
    torrent_client: str = "realdebrid"  # or "qbittorrent" / "transmission" / "deluge"
    qbittorrent_url: str = ""
    qbittorrent_username: str = ""
    qbittorrent_password: str = ""
    qbittorrent_category: str = "audiobooks"
    transmission_url: str = ""
    transmission_username: str = ""
    transmission_password: str = ""
    deluge_url: str = ""
    deluge_password: str = ""
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
    auth_local_bypass: bool = False  # skip sign-in for requests from this machine and the home network

    def public(self) -> dict:
        """Settings safe to send to the browser (secrets masked)."""
        d = asdict(self)
        for key in SECRETS:
            d[key] = _mask(d[key], key)
        d["rd_configured"] = bool(self.rd_api_key)
        d["brave_configured"] = bool(self.brave_api_key)
        d["prowlarr_configured"] = bool(self.prowlarr_url and self.prowlarr_api_key)
        d["torznab_configured"] = bool(self.torznab_url and self.torznab_api_key)
        d["sabnzbd_configured"] = bool(self.sabnzbd_url and self.sabnzbd_api_key)
        d["nzbget_configured"] = bool(self.nzbget_url)
        d["qbittorrent_configured"] = bool(self.qbittorrent_url)
        d["transmission_configured"] = bool(self.transmission_url)
        d["deluge_configured"] = bool(self.deluge_url)
        d["usenet_configured"] = d[f"{self.usenet_client}_configured"]
        d["torrent_configured"] = d["rd_configured" if self.torrent_client == "realdebrid" else f"{self.torrent_client}_configured"]
        return d


SECRETS = (
    "rd_api_key", "audiobookshelf_token", "brave_api_key", "prowlarr_api_key", "sabnzbd_api_key",
    "nzbget_password", "qbittorrent_password", "transmission_password", "deluge_password", "torznab_api_key",
)
CHOICES = {
    "search_provider": ("audiobookbay", "brave"),
    "usenet_client": ("sabnzbd", "nzbget"),
    "torrent_client": ("realdebrid", "qbittorrent", "transmission", "deluge"),
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
        return _settings


def update(changes: dict) -> Settings:
    """Apply changes and persist. Masked secrets sent back unchanged are ignored."""
    s = load()
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
            setattr(s, k, v)
        if s.import_folder:
            lib, imp = Path(s.library_dir).expanduser().resolve(), Path(s.import_folder).expanduser().resolve()
            if lib == imp or lib in imp.parents or imp in lib.parents:
                raise ValueError("The import folder can't be inside the library folder, or contain it.")
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _write_private(SETTINGS_FILE, json.dumps(asdict(s), indent=2))
    return s


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
