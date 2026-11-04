"""Why a server on the user's own network couldn't be reached, in words they can act on.

"Couldn't reach SABnzbd" is true and useless. The connection error already knows whether the name
didn't resolve, nothing was listening on the port, nothing answered at all, or the address spoke
plain http to an https request; and inside Docker, "localhost" is Narratarr's own container, which is
the commonest wrong address of all. This module turns those into one sentence each.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import ssl
from pathlib import Path
from urllib.parse import urlparse

import httpx

_UNREACHABLE = {101, 113, 51, 65}  # ENETUNREACH / EHOSTUNREACH, on Linux and on macOS


def in_docker() -> bool:
    """Whether Narratarr is running in a container (Docker or Podman)."""
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists() or bool(os.environ.get("NARRATARR_DOCKER"))


def _host_port(url: str) -> tuple[str, int | None]:
    u = urlparse(url if "://" in url else f"//{url}")
    try:
        port = u.port
    except ValueError:
        port = None
    if port is None and u.scheme in ("http", "https"):
        port = 443 if u.scheme == "https" else 80
    return (u.hostname or "").lower(), port


def is_loopback(url: str) -> bool:
    host, _ = _host_port(url)
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def localhost_hint(url: str, label: str, example: str) -> str:
    """The sentence for "localhost" typed into Narratarr running in Docker, or "" when it doesn't apply."""
    if not (is_loopback(url) and in_docker()):
        return ""
    return (f"Narratarr runs in Docker, so “localhost” here means Narratarr’s own container, not the machine "
            f"{label} runs on. Use that machine’s IP address instead, like {example}.")


def _causes(exc: BaseException) -> list[BaseException]:
    """The exception, what caused it, and the members of any exception group along the way."""
    seen, todo, out = set(), [exc], []
    while todo and len(out) < 20:
        e = todo.pop(0)
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        out.append(e)
        todo += [e.__cause__, e.__context__]
        todo += list(getattr(e, "exceptions", ()) or ())
    return out


def reason(exc: BaseException) -> str:
    """One of "name", "refused", "timeout", "tls", "unreachable" or "" for anything else."""
    causes = _causes(exc)
    if any(isinstance(e, socket.gaierror) for e in causes):
        return "name"
    if any(isinstance(e, ConnectionRefusedError) for e in causes):
        return "refused"
    if any(isinstance(e, ssl.SSLError) or "SSL" in str(e) for e in causes):
        return "tls"
    if any(isinstance(e, OSError) and e.errno in _UNREACHABLE for e in causes):
        return "unreachable"
    if any(isinstance(e, (httpx.TimeoutException, TimeoutError)) for e in causes):
        return "timeout"
    return ""


def explain(exc: BaseException, url: str, label: str, example: str = "http://192.168.1.10:8080") -> str:
    """A full sentence or two: what failed, and what to do about it."""
    host, port = _host_port(url)
    where = f"{host}:{port}" if port else host
    hint = localhost_hint(url, label, example)
    why = reason(exc)
    if hint and why in ("refused", "timeout", ""):
        return f"Couldn't reach {label} at {where}. {hint}"
    if why == "name":
        text = f"Couldn't reach {label}: no machine called “{host}” could be found."
        if "." not in host and in_docker():
            text += (" A container name only works when both containers are on the same Docker network."
                     " Otherwise use the IP address of the machine it runs on.")
        else:
            text += " Check the spelling, or use its IP address."
        return text
    if why == "refused":
        return (f"Couldn't reach {label}: nothing is answering on port {port} at {host}. "
                f"Check the port, and that {label} is running.")
    if why == "tls":
        return f"Couldn't reach {label} at {where} over https. If it doesn't use https, start the address with http:// instead."
    if why == "unreachable":
        return f"Couldn't reach {label}: there's no route to {host} from here. Check the IP address."
    if why == "timeout":
        return (f"Couldn't reach {label}: {where} didn't answer. Check the address, and that no firewall "
                "is in the way.")
    return f"Couldn't reach {label} at {where}. Check the address in Settings."
