"""Knowing when a new version is out, and installing it.

The check is one small request to GitHub's releases API, at most once a day,
and can be turned off in Settings. Installing only works for native installs
made with uv (the installer's default); Docker users get the command to run.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import httpx

from . import config
from .cache import Cache

log = logging.getLogger("narratarr.updates")

REPO = "narratarr/narratarr"
RELEASES = (
    os.environ.get("NARRATARR_RELEASES_URL") or f"https://api.github.com/repos/{REPO}/releases"
)
CHECK_SECONDS = 24 * 3600
PACKAGE = os.environ.get(
    "NARRATARR_PACKAGE", ""
)  # override the install source (used by tests and the installer)

_cache: Cache[dict] = Cache(CHECK_SECONDS, 4)
_state = {"status": "idle", "detail": "", "started": 0.0}  # the self-update in progress, if any
_lock = threading.Lock()


def current_version() -> str:
    try:
        return version("narratarr")
    except PackageNotFoundError:
        return "0.0.0"


def parse_version(v: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", v)[:3]) or (0,)


def install_kind() -> str:
    """How this copy was installed: "uv" (can update itself), "docker", "source" or "other"."""
    if Path("/.dockerenv").exists() or os.environ.get("NARRATARR_DATA") == "/config":
        return "docker"
    prefix = Path(sys.prefix).resolve()
    if "uv" in prefix.parts and "tools" in prefix.parts and prefix.name == "narratarr":
        return "uv"
    if (Path(__file__).resolve().parent.parent / ".git").exists():
        return "source"
    return "other"


def find_uv() -> str:
    """uv, even when started by launchd or systemd with a bare PATH."""
    home = Path.home()
    candidates = [
        shutil.which("uv"),
        home / ".local/bin/uv",
        home / ".cargo/bin/uv",
        "/opt/homebrew/bin/uv",
        "/usr/local/bin/uv",
    ]
    for c in candidates:
        if c and Path(c).is_file() and os.access(c, os.X_OK):
            return str(c)
    return ""


def changelog() -> str:
    for p in (
        Path(__file__).parent / "CHANGELOG.md",
        Path(__file__).parent.parent / "CHANGELOG.md",
    ):
        if p.is_file():
            return p.read_text(encoding="utf-8")
    return ""


async def latest_release(
    force: bool = False, client: httpx.AsyncClient | None = None
) -> dict | None:
    """The newest GitHub release: {version, notes, url, published}, or None if it couldn't be checked."""
    if not force:
        hit = _cache.get("latest")
        if hit is not None:
            return hit
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"narratarr/{current_version()}",
    }
    try:
        if client:
            resp = await client.get(f"{RELEASES}/latest", headers=headers)
        else:
            async with httpx.AsyncClient(timeout=10) as c:
                resp = await c.get(f"{RELEASES}/latest", headers=headers)
        resp.raise_for_status()
        r = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    release = {
        "version": (r.get("tag_name") or "").lstrip("v"),
        "notes": r.get("body") or "",
        "url": r.get("html_url") or f"https://github.com/{REPO}/releases",
        "published": (r.get("published_at") or "")[:10],
    }
    _cache.set("latest", release)
    return release


async def status(force: bool = False) -> dict:
    s = config.load()
    current = current_version()
    latest = await latest_release(force) if (s.check_updates or force) else None
    newer = bool(latest and parse_version(latest["version"]) > parse_version(current))
    kind = install_kind()
    return {
        "current": current,
        "latest": latest,
        "update_available": newer,
        "install": kind,
        "can_self_update": kind == "uv" and bool(find_uv()),
        "checks_enabled": s.check_updates,
        **_state,
    }


def _run_update(target: str) -> None:
    """Install the new version, then replace this process with it. Runs in a thread."""
    uv = find_uv()
    try:
        _state.update(status="installing", detail=f"Downloading and installing Narratarr {target}…")
        pkg = PACKAGE or f"git+https://github.com/{REPO}@v{target}"
        out = subprocess.run(
            [
                uv,
                "tool",
                "install",
                "--force",
                "--reinstall-package",
                "narratarr",
                "--refresh-package",
                "narratarr",
                pkg,
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if out.returncode != 0:
            tail = (out.stderr or out.stdout).strip().splitlines()[-1:] or ["unknown error"]
            _state.update(status="failed", detail=f"The install didn't finish: {tail[0][:200]}")
            log.warning("Self-update failed: %s", out.stderr.strip()[-500:])
            return
        _state.update(status="restarting", detail="Installed. Restarting…")
        log.info("Updated to %s; restarting", target)
        time.sleep(0.5)  # let the status reply go out
        exe = Path(sys.argv[0]).resolve()
        os.execv(str(exe), [str(exe), *sys.argv[1:]])
    except Exception as e:  # noqa: BLE001 - whatever happens, the UI must hear about it
        _state.update(status="failed", detail=f"The update didn't finish: {e}")
        log.exception("Self-update crashed")


async def apply(target: str) -> dict:
    """Start installing `target` in the background. Returns the status right away."""
    with _lock:
        if _state["status"] in ("installing", "restarting"):
            return dict(_state)
        if install_kind() != "uv" or not find_uv():
            raise RuntimeError(
                "This copy can't update itself. See Settings → Updates for the command to run."
            )
        _state.update(status="installing", detail="Starting…", started=time.time())
    await asyncio.to_thread(
        lambda: threading.Thread(target=_run_update, args=(target,), daemon=True).start()
    )
    return dict(_state)
