"""Audiobookshelf: finding the server, signing in once, and asking it to rescan.

API: https://api.audiobookshelf.org

Signing in uses the user's ABS username and password exactly once, to create an API
key named "Narratarr" (Settings → Users → API Keys in ABS, from 2.26 on). Only that
key is kept; the password is never stored. The key can be revoked in ABS at any time.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx

from . import reach

log = logging.getLogger("narratarr.audiobookshelf")

PORT = 13378
KEY_NAME = "Narratarr"
# Where Audiobookshelf usually is: on this machine, or next to Narratarr in Docker. Inside Docker,
# Audiobookshelf's own container listens on port 80; 13378 is the port people publish on the host.
NEARBY = (
    f"http://localhost:{PORT}",
    "http://audiobookshelf",
    f"http://audiobookshelf:{PORT}",
    "http://abs",
    f"http://abs:{PORT}",
    f"http://host.docker.internal:{PORT}",
)
EXAMPLE = "192.168.1.10:13378"
ROUTE = "/proc/net/route"  # the kernel's routing table, for the Docker host
PROBE_TIMEOUT = httpx.Timeout(4, connect=1.5, pool=None)
SWEEP_TIMEOUT = httpx.Timeout(2, connect=0.75, pool=None)
SWEEP_AT_ONCE = 64

transport: httpx.AsyncBaseTransport | None = None  # tests swap in a fake server


class AudiobookshelfError(Exception):
    pass


def _client(timeout: httpx.Timeout | float = 15) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, transport=transport, limits=httpx.Limits(max_connections=SWEEP_AT_ONCE))


# ---- finding the server ------------------------------------------------------


def is_public(url: str) -> bool:
    """Whether this address is out on the internet rather than on this machine or network."""
    host = (urlparse(url).hostname or "").lower()
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        pass
    if "." not in host or host.endswith((".local", ".lan", ".home", ".internal", ".home.arpa", ".ts.net")):
        return False
    try:
        addrs = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        return True  # can't tell; assume the worst
    return any(ipaddress.ip_address(a.split("%")[0]).is_global for a in addrs)


def insecure(url: str) -> bool:
    """A password or key sent here could be read by anyone along the way."""
    return url.startswith("http://") and is_public(url)


async def probe(client: httpx.AsyncClient, url: str) -> dict | None:
    """The server at this address, if it's Audiobookshelf. /status needs no login."""
    try:
        resp = await client.get(f"{url}/status")
        data = resp.json() if resp.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("app") != "audiobookshelf":
        return None
    return {
        "url": url,
        "version": str(data.get("serverVersion") or ""),
        "ready": bool(data.get("isInit", True)),
        "password_login": "local" in (data.get("authMethods") or ["local"]),
    }


async def _first(urls: list[str], timeout: httpx.Timeout) -> list[dict]:
    """Probe the addresses, many at a time; the servers that answered, in the order given."""
    gate = asyncio.Semaphore(SWEEP_AT_ONCE)

    async def one(client: httpx.AsyncClient, url: str) -> dict | None:
        async with gate:  # queue here rather than in httpx's pool, whose wait would count against the timeout
            return await probe(client, url)

    async with _client(timeout) as client:
        found = await asyncio.gather(*(one(client, u) for u in urls))
    seen, servers = set(), []
    for s in found:
        if s and s["url"] not in seen:
            seen.add(s["url"])
            servers.append(s)
    return servers


def _local_network() -> ipaddress.IPv4Network | None:
    """This machine's home network (a /24), or None when it isn't on a private one."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))  # picks the outgoing interface; UDP connect sends nothing
            ip = ipaddress.ip_address(s.getsockname()[0])
        except OSError:
            return None
    if not ip.is_private or ip.is_loopback:
        return None
    return ipaddress.ip_network(f"{ip}/24", strict=False)


def _own_addresses() -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """This machine's own addresses, including a public one bound straight to it (as on a VPS)."""
    found = set()
    try:
        found |= {info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None)}
    except OSError:
        pass
    for target in ("10.255.255.255", "8.8.8.8"):  # the outgoing interface for each; UDP connect sends nothing
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            try:
                s.connect((target, 1))
                found.add(s.getsockname()[0])
            except OSError:
                pass
    return {ipaddress.ip_address(a.split("%")[0]) for a in found}


def where(url: str) -> str:
    """Where this Audiobookshelf runs, as far as its address tells: "here" (this machine),
    "docker" (a container beside Narratarr's), "network" (another machine at home) or "internet"."""
    host = (urlparse(url).hostname or "").lower()
    if host in ("localhost", "host.docker.internal") or host.endswith(".localhost"):
        return "here"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is None and "." not in host:
        return "docker"  # a bare name like "audiobookshelf" is another container on the same Docker network
    if ip is not None and (ip.is_loopback or ip in _own_addresses()):
        return "here"
    return "internet" if is_public(url) else "network"


def _docker_host() -> str:
    """The machine Docker runs on, as seen from inside a container: the default gateway. "" outside Docker.

    On Linux, host.docker.internal only exists when the container is started with it, but the
    default gateway of a bridge network is always the host, and ports published there answer on it.
    """
    if not reach.in_docker():
        return ""
    try:
        with open(ROUTE) as f:
            for line in f.read().splitlines()[1:]:
                parts = line.split()
                if len(parts) > 2 and parts[1] == "00000000":
                    return socket.inet_ntoa(bytes.fromhex(parts[2])[::-1])
    except (OSError, ValueError):
        pass
    return ""


async def find_nearby(sweep: bool = True) -> list[dict]:
    """Audiobookshelf on this machine or beside it in Docker; failing that, anywhere on the home network."""
    urls = list(NEARBY)
    host = await asyncio.to_thread(_docker_host)
    if host:
        urls += [f"http://{host}:{PORT}", f"http://{host}"]
    servers = await _first(urls, PROBE_TIMEOUT)
    if servers or not sweep:
        return servers[:1]  # localhost and a Docker name are often the same server
    net = await asyncio.to_thread(_local_network)
    if not net:
        return []
    # In Docker the network we can see is Docker's own, where Audiobookshelf's container listens on 80.
    ports = (PORT, 80) if reach.in_docker() else (PORT,)
    return await _first([f"http://{ip}:{port}" for ip in net.hosts() for port in ports], SWEEP_TIMEOUT)


def nothing_nearby() -> str:
    """Why Find came back empty, and what to type instead."""
    if reach.in_docker():
        return ("No Audiobookshelf found. Narratarr runs in Docker, so it can only look at its own Docker network "
                "and the machine Docker runs on, not your whole home network. Type the IP address of the machine "
                f"running Audiobookshelf and the port it uses, like {EXAMPLE}, and press Find.")
    return "No Audiobookshelf found nearby. Type its address and press Find."


def addresses_for(address: str) -> list[str]:
    """What someone might mean by an address: "abs.example.com" could be https on 443, 13378 or 80,
    at the root or under /audiobookshelf behind a reverse proxy. Anything they spelled out is kept."""
    address = address.strip().rstrip("/")
    if not address:
        return []
    u = urlparse(address if "://" in address else f"//{address}")
    if not u.hostname:
        return []
    host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
    schemes = [u.scheme] if u.scheme in ("http", "https") else ["https", "http"]
    try:
        ports = [u.port] if u.port else [None, PORT]
    except ValueError:
        return []
    paths = [u.path] if u.path else ["", "/audiobookshelf"]
    urls = []
    for scheme in schemes:
        for port in ports:
            for path in paths:
                urls.append(f"{scheme}://{host}{f':{port}' if port else ''}{path}")
    return urls


def _parsed(address: str):
    address = address.strip().rstrip("/")
    return urlparse(address if "://" in address else f"//{address}")


def _other_ports(address: str) -> tuple[int | None, list[str]]:
    """When the user gave a port: the same host on Audiobookshelf's usual ports instead.

    The commonest wrong port is 13378 for a container beside Narratarr's: that's the port published on
    the host, while the container itself listens on 80.
    """
    u = _parsed(address)
    try:
        given = u.port
    except ValueError:
        return None, []
    if not given or not u.hostname:
        return None, []
    host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
    paths = [u.path] if u.path else ["", "/audiobookshelf"]
    return given, [f"http://{host}{'' if port == 80 else f':{port}'}{path}"
                   for port in (80, PORT) if port != given for path in paths]


async def find_at(address: str) -> list[dict]:
    """Audiobookshelf at an address the user typed: a LAN or public IP, or a domain."""
    urls = addresses_for(address)
    if not urls:
        raise AudiobookshelfError("That doesn't look like an address. Try something like 203.0.113.7 or abs.example.com.")
    found = (await _first(urls, PROBE_TIMEOUT))[:1]  # the first answer in order: https before http
    if found:
        return found
    given, others = _other_ports(address)
    found = (await _first(others, PROBE_TIMEOUT))[:1]
    for server in found:
        port = urlparse(server["url"]).port or 80
        server["note"] = f"Found it on port {port}, not {given}."
        if port == 80:
            server["note"] += (" Inside Docker, Audiobookshelf's own container listens on port 80;"
                               f" {given} is only the port published on the machine it runs on.")
    return found


async def why_not(address: str) -> str:
    """Why there's no Audiobookshelf at this address, in a sentence the user can act on."""
    u = _parsed(address)
    if not u.hostname:
        return "That doesn't look like an address. Try something like 192.168.1.10:13378."
    url = address.strip().rstrip("/")
    if "://" not in url:  # what they most likely meant: https for a domain on the internet, else http
        url = f"{'https' if is_public(f'http://{u.hostname}') else 'http'}://{url}"
    hint = reach.localhost_hint(url, "Audiobookshelf", EXAMPLE)
    try:
        async with _client(PROBE_TIMEOUT) as client:
            resp = await client.get(f"{url}/status")
    except httpx.HTTPError as e:
        return reach.explain(e, url, "Audiobookshelf", EXAMPLE)
    if hint:  # something answered, but in Docker that's Narratarr's own container
        return hint
    return (f"Something answered at {url}, but it isn't Audiobookshelf ({resp.status_code}). "
            "Check the port: it's the one you open Audiobookshelf's web page on.")


# ---- talking to it -------------------------------------------------------------


class Audiobookshelf:
    def __init__(self, url: str, token: str = ""):
        if not url:
            raise AudiobookshelfError("Find your Audiobookshelf server first.")
        self.url = url.rstrip("/")
        self._token = token
        self._client = _client()

    async def __aenter__(self) -> Audiobookshelf:
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, token: str | None = None, **kw) -> httpx.Response:
        token = token if token is not None else self._token
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            resp = await self._client.request(method, f"{self.url}{path}", headers=headers, **kw)
        except httpx.HTTPError as e:
            raise AudiobookshelfError(reach.explain(e, self.url, "Audiobookshelf", f"http://{EXAMPLE}")) from e
        if resp.status_code == 401:
            raise AudiobookshelfError("Audiobookshelf didn't accept Narratarr's key. Sign in again.")
        if resp.status_code == 403:
            raise AudiobookshelfError("That Audiobookshelf account isn't an admin, and only admins can start a library scan.")
        if resp.status_code == 404:
            raise AudiobookshelfError("Audiobookshelf doesn't know that library any more. Pick it again.")
        if resp.status_code >= 400:
            raise AudiobookshelfError(f"Audiobookshelf returned an error ({resp.status_code}).")
        return resp

    async def sign_in(self, username: str, password: str) -> str:
        """Log in once and create an API key for Narratarr. Returns the key; the password goes no further."""
        try:
            resp = await self._client.post(f"{self.url}/login", json={"username": username, "password": password})
        except httpx.HTTPError as e:
            raise AudiobookshelfError(reach.explain(e, self.url, "Audiobookshelf", f"http://{EXAMPLE}")) from e
        if resp.status_code == 401:
            raise AudiobookshelfError("Audiobookshelf didn't accept that username and password.")
        if resp.status_code == 429:
            raise AudiobookshelfError("Audiobookshelf has paused sign-ins after too many tries. Wait a few minutes.")
        if resp.status_code >= 400:
            raise AudiobookshelfError(f"Audiobookshelf couldn't sign you in ({resp.status_code}).")
        try:
            user = resp.json()["user"]
        except (ValueError, KeyError, TypeError):
            raise AudiobookshelfError("That address answered, but not like Audiobookshelf does.")
        if user.get("type") not in ("root", "admin"):
            raise AudiobookshelfError("Sign in with an Audiobookshelf admin account. Only admins can start a library scan.")
        session = user.get("accessToken") or user.get("token") or ""
        try:
            resp = await self._client.post(
                f"{self.url}/api/api-keys", headers={"Authorization": f"Bearer {session}"},
                json={"name": KEY_NAME, "userId": user.get("id"), "isActive": True},
            )
        except httpx.HTTPError as e:
            raise AudiobookshelfError(reach.explain(e, self.url, "Audiobookshelf", f"http://{EXAMPLE}")) from e
        if resp.status_code == 404 and user.get("token"):
            return user["token"]  # before 2.26 there are no API keys; the account's own token never expires
        if resp.status_code >= 400:
            raise AudiobookshelfError(f"Audiobookshelf wouldn't create a key for Narratarr ({resp.status_code}).")
        try:
            return resp.json()["apiKey"]["apiKey"]
        except (ValueError, KeyError, TypeError):
            raise AudiobookshelfError("Audiobookshelf created a key but didn't send it back. Paste one instead.")

    async def status(self) -> dict:
        found = await probe(self._client, self.url)
        if not found:
            raise AudiobookshelfError(await why_not(self.url))
        return found

    async def libraries(self) -> list[dict]:
        """Audiobook libraries (podcast libraries are left out), with the folders each one watches."""
        try:
            data = (await self._call("GET", "/api/libraries")).json()
        except ValueError:
            raise AudiobookshelfError("That address answered, but not like Audiobookshelf does.")
        found = data.get("libraries", data) if isinstance(data, dict) else data
        return [
            {"id": lib.get("id"), "name": lib.get("name") or "Library",
             "folders": [f.get("fullPath") for f in lib.get("folders") or [] if f.get("fullPath")]}
            for lib in found or [] if isinstance(lib, dict) and lib.get("mediaType", "book") == "book"
        ]

    async def scan(self, library_id: str) -> None:
        await self._call("POST", f"/api/libraries/{library_id}/scan")

    async def items(self, library_id: str) -> list[dict]:
        """Every book in a library, with where Audiobookshelf found it."""
        found, page = [], 0
        while page < 40:  # 20,000 books is plenty for a check
            resp = await self._call("GET", f"/api/libraries/{library_id}/items",
                                    params={"limit": 500, "page": page, "minified": 1})
            try:
                data = resp.json()
                results = data.get("results") or []
            except (ValueError, AttributeError):
                raise AudiobookshelfError("That address answered, but not like Audiobookshelf does.")
            found += [{"path": i.get("path") or "", "rel": i.get("relPath") or "", "missing": bool(i.get("isMissing")),
                       **_book_of(i)} for i in results if isinstance(i, dict)]
            page += 1
            if not results or len(found) >= int(data.get("total") or 0):
                break
        return found


def _book_of(item: dict) -> dict:
    """The title, author and ASIN Audiobookshelf has for a library item."""
    meta = ((item.get("media") or {}).get("metadata") or {}) if isinstance(item.get("media"), dict) else {}
    return {"title": str(meta.get("title") or ""), "author": str(meta.get("authorName") or ""),
            "asin": str(meta.get("asin") or "")}


OWNED_SECONDS = 600
_owned: dict = {"key": None, "at": 0.0, "books": []}


async def owned(settings) -> list[dict]:
    """The books in the connected Audiobookshelf library, as {title, author, asin}. Kept for ten minutes,
    so pages that check it stay quick. Nothing, rather than an error, when it can't be reached."""
    import time

    url, token, library = settings.audiobookshelf_url, settings.audiobookshelf_token, settings.audiobookshelf_library_id
    if not (url and token and library):
        return []
    key = (url, library)
    if _owned["key"] == key and time.time() - _owned["at"] < OWNED_SECONDS:
        return _owned["books"]
    try:
        async with Audiobookshelf(url, token) as server:
            books = [i for i in await server.items(library) if not i["missing"] and i["title"]]
    except AudiobookshelfError as e:
        log.info("Couldn't read the Audiobookshelf library: %s", e)
        return _owned["books"] if _owned["key"] == key else []
    _owned.update(key=key, at=time.time(), books=books)
    return books


def _parts(path: str) -> tuple[str, ...]:
    return tuple(p for p in path.replace("\\", "/").split("/") if p)


def compare(books: list[dict], library_dir: str, items: list[dict]) -> dict:
    """Which of Narratarr's books Audiobookshelf has, and where it sees Narratarr's library folder.

    Paths differ between the two (Docker mounts, mapped drives), so books are matched by
    where they sit under the library folder: Narratarr's "Author/Title" against the end of
    the path Audiobookshelf found it at."""
    root = _parts(library_dir)
    there = [(_parts(i["path"]), _parts(i["rel"])) for i in items if not i["missing"]]
    # Audiobookshelf sees a book's folder. An ebook's path is its file, and an ebook that shares its
    # audiobook's folder is the same item there, so it counts once.
    folders: dict[tuple[str, ...], str] = {}
    for book in books:
        path = _parts(book["path"])
        folders.setdefault(path[:-1] if book.get("kind") == "ebook" else path, book["title"])
    seen, missing, seen_at = 0, [], ""
    for path, title in folders.items():
        rel = path[len(root):] if path[:len(root)] == root and len(path) > len(root) else path[-2:]
        hit = next((full for full, r in there if len(r) >= len(rel) and r[-len(rel):] == rel), None)
        if hit is None:
            missing.append(title)
            continue
        seen += 1
        if not seen_at and len(hit) > len(rel):
            seen_at = "/" + "/".join(hit[:-len(rel)]) if not hit[0].endswith(":") else "\\".join(hit[:-len(rel)])
    return {"books": len(folders), "seen": seen, "missing": missing[:5], "seen_at": seen_at}


def pick_library(libraries: list[dict], library_dir: str) -> str:
    """The library that watches Narratarr's folder; the only one if there is just one; else none."""
    want = library_dir.rstrip("/")
    for lib in libraries:
        if any(f.rstrip("/") == want for f in lib["folders"]):
            return lib["id"]
    return libraries[0]["id"] if len(libraries) == 1 else ""
