"""Who is making a request, and what they're allowed to do.

Signing in is required. The exceptions are /api/health, the sign-in endpoints themselves,
and, when an admin turns it on, requests from this machine or the home network, which act
as the first admin. Requests that came through a reverse proxy never count as local,
because the proxy hides where they really came from.

Before anyone has an account, the first visitor creates the admin. From this machine or
the home network that's all it takes; from anywhere else it also needs the setup code
printed in the log, so a stranger who finds a fresh install can't claim it.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import secrets
import time

from . import config, users

log = logging.getLogger("narratarr.auth")

COOKIE = "narratarr_session"
PROXY_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded", "cf-connecting-ip", "true-client-ip")
# Paths anyone may use. Everything else under /api/ needs a signed-in user.
OPEN = {"/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/setup"}
# Paths only admins may use (any method), and admin-only method/path pairs.
ADMIN_PREFIXES = ("/api/users", "/api/settings/", "/api/audiobookshelf", "/api/import", "/api/updates/apply")
LOGIN_TRIES = 8  # per address, every LOGIN_WINDOW seconds
LOGIN_WINDOW = 10 * 60

_failures: dict[str, list[float]] = {}
_setup_code = ""


def is_local(request) -> bool:
    """This machine or the home network, reached directly rather than through a proxy."""
    if any(h in request.headers for h in PROXY_HEADERS):
        return False
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_private or ip.is_link_local


def current_user(request) -> dict | None:
    user = users.session_user(request.cookies.get(COOKIE, ""))
    if user:
        return user
    if config.load().auth_local_bypass and is_local(request):
        return users.owner()
    return None


def admin_only(method: str, path: str) -> bool:
    if path.startswith(ADMIN_PREFIXES) or (path == "/api/settings" and method != "GET"):
        return True
    return method == "DELETE" and path.startswith("/api/library/")


# ---- first run --------------------------------------------------------------------------


def setup_code() -> str:
    """The one-time code that lets someone create the first admin from outside the home network."""
    global _setup_code
    if not _setup_code:
        _setup_code = f"{secrets.randbelow(10**4):04d}-{secrets.randbelow(10**4):04d}"
    return _setup_code


def adopt_env_password() -> None:
    """Before 0.5.5 there was one shared password ($NARRATARR_PASSWORD). It becomes the admin account."""
    password = os.environ.get("NARRATARR_PASSWORD", "")
    if not password or users.count():
        return
    try:
        users.create("admin", password, "admin")
    except users.UserError as e:
        log.warning("Couldn't turn NARRATARR_PASSWORD into an account (%s); create one in the browser instead.", e)
        return
    log.warning("Created the admin account 'admin' with your NARRATARR_PASSWORD. Sign in with that, then unset it.")


def announce_setup() -> None:
    if users.count():
        return
    message = (f"No accounts yet. Open Narratarr to create the admin account. From another network "
               f"you'll be asked for this setup code: {setup_code()}  (or run: narratarr users add <name>)")
    print(f"\n  {message}\n", flush=True)
    log.warning(message)


# ---- sign-in attempts -----------------------------------------------------------------------
# Each attempt is counted *before* the password is checked (and forgiven when it's right),
# so a burst of simultaneous guesses can't all slip in under the limit.
#
# Two limits. Per address (IPv6 by /64, since one machine can have millions of addresses):
# LOGIN_TRIES. Per account: NAME_TRIES, however many addresses the guesses come from. The
# account limit only ever stops strangers: someone on the home network, or on a network that
# has signed in to that account before, can always sign in with the right password, so
# nobody can lock a person out just by knowing their username.

NAME_TRIES = 30  # per account, every LOGIN_WINDOW seconds
_MAX_KEYS = 10_000
_KNOWN_PER_ACCOUNT = 32
_known: dict[str, list[str]] = {}  # username -> networks that signed in to it, most recent last
_warned_untrusted = False


def _trusted_proxies() -> list:
    nets = []
    for part in os.environ.get("NARRATARR_TRUSTED_PROXIES", "").split(","):
        try:
            nets.append(ipaddress.ip_network(part.strip(), strict=False))
        except ValueError:
            pass
    return nets


def _ip(text: str):
    """An address as proxies write it: 1.2.3.4, 1.2.3.4:5678, 2001:db8::1, [2001:db8::1]:443."""
    text = text.strip().strip('"')
    if text.startswith("["):
        text = text[1:text.find("]")] if "]" in text else text[1:]
    elif text.count(":") == 1:
        text = text.split(":", 1)[0]
    try:
        return ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return None


def _in(addr: str, nets: list) -> bool:
    ip = _ip(addr)
    return ip is not None and any(ip in n for n in nets)


def client_ip(request) -> str:
    """Who's really asking. Behind a reverse proxy listed in NARRATARR_TRUSTED_PROXIES, that's
    the address the proxy saw (the last one in X-Forwarded-For it didn't add itself); otherwise
    the address that connected. Headers from anyone else are ignored, since they can be forged."""
    global _warned_untrusted
    host = request.client.host if request.client else ""
    nets = _trusted_proxies()
    if not nets or not _in(host, nets):
        if not _warned_untrusted and "x-forwarded-for" in request.headers:
            _warned_untrusted = True
            log.warning("Requests arrive through a proxy at %s. Set NARRATARR_TRUSTED_PROXIES to its address so "
                        "sign-in limits apply to each visitor instead of everyone at once.", host)
        return host
    chain = [h for value in request.headers.getlist("x-forwarded-for") for h in value.split(",") if h.strip()]
    for hop in reversed(chain):
        ip = _ip(hop)
        if ip is None:
            return host  # garbled: don't trust anything further along
        if not any(ip in n for n in nets):
            return str(ip)
    return host


def _network(addr: str) -> str:
    """The unit limits apply to: one IPv4 address, or one IPv6 /64."""
    ip = _ip(addr)
    if ip is None:
        return addr
    if ip.version == 6:
        return str(ip.ipv4_mapped) if ip.ipv4_mapped else str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def _recent(key: str, now: float) -> list[float]:
    recent = [t for t in _failures.get(key, []) if now - t < LOGIN_WINDOW]
    if recent:
        _failures[key] = recent
    else:
        _failures.pop(key, None)
    return recent


def _trim(now: float) -> None:
    """Keep the table bounded however many addresses try. Live account counters are never
    dropped (there's one per real account at most), so a flood can't reset them."""
    if len(_failures) <= _MAX_KEYS:
        return
    for key in [k for k, ts in _failures.items() if not ts or now - ts[-1] >= LOGIN_WINDOW]:
        del _failures[key]
    if len(_failures) > _MAX_KEYS:
        addresses = sorted((k for k in _failures if k.startswith("ip:")), key=lambda k: _failures[k][-1])
        for key in addresses[: len(addresses) // 2]:
            del _failures[key]


def start_try(request, username: str = ""):
    """Count a sign-in attempt before checking it. None when there have been too many; otherwise
    a ticket to pass to forgive() if the attempt turns out to be right."""
    now = time.time()
    network = _network(client_ip(request))
    keys = [f"ip:{network}"]
    if len(_recent(keys[0], now)) >= LOGIN_TRIES:
        return None
    name = username.strip().lower()[:64]
    if name and users.by_name(name):  # only real accounts get a counter, so made-up names can't flood the table
        familiar = is_local(request) or network in _known.get(name, ())
        if not familiar and len(_recent(f"user:{name}", now)) >= NAME_TRIES:
            return None
        keys.append(f"user:{name}")
    for key in keys:
        _failures.setdefault(key, []).append(now)
    _trim(now)
    return keys, now, name, network


def forgive(ticket) -> None:
    """The attempt was right: it doesn't count, and this network is now familiar to the account."""
    keys, at, name, network = ticket
    for key in keys:
        try:
            _failures[key].remove(at)
        except (KeyError, ValueError):
            pass
    if name:
        seen = [n for n in _known.get(name, []) if n != network] + [network]
        if name not in _known and len(_known) >= _MAX_KEYS:
            _known.pop(next(iter(_known)))
        _known[name] = seen[-_KNOWN_PER_ACCOUNT:]


def too_many_tries(request) -> bool:
    return len(_recent(f"ip:{_network(client_ip(request))}", time.time())) >= LOGIN_TRIES


def failed_try(request) -> None:
    now = time.time()
    _failures.setdefault(f"ip:{_network(client_ip(request))}", []).append(now)
    _trim(now)


def cookie_args(request) -> dict:
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"
    return {"key": COOKIE, "httponly": True, "samesite": "lax", "secure": secure, "path": "/"}
