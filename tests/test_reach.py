"""Why a server couldn't be reached: the sentence each kind of failure gets, and Audiobookshelf's Find."""

import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from narratarr import api, audiobookshelf, reach
from narratarr.sabnzbd import SABnzbd, SABnzbdError

from test_audiobookshelf import FakeABS


def _connect_error(cause: BaseException) -> httpx.ConnectError:
    try:
        try:
            raise cause
        except BaseException as inner:
            raise httpx.ConnectError(str(inner)) from inner
    except httpx.ConnectError as e:
        return e


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def docker(monkeypatch):
    monkeypatch.setattr(reach, "in_docker", lambda: True)


@pytest.fixture
def bare_metal(monkeypatch):
    monkeypatch.setattr(reach, "in_docker", lambda: False)


# ---- the reason, from the exception chain -------------------------------------------


def test_reasons():
    assert reach.reason(_connect_error(socket.gaierror(-2, "Name or service not known"))) == "name"
    assert reach.reason(_connect_error(ConnectionRefusedError(111, "Connection refused"))) == "refused"
    assert reach.reason(_connect_error(OSError(113, "No route to host"))) == "unreachable"
    assert reach.reason(httpx.ConnectTimeout("")) == "timeout"
    assert reach.reason(_connect_error(OSError("[SSL: WRONG_VERSION_NUMBER] wrong version number"))) == "tls"
    assert reach.reason(httpx.ConnectError("something else")) == ""


def test_a_refusal_inside_an_exception_group_is_found():
    # localhost resolves to two addresses, so the refusal arrives inside a group
    group = ExceptionGroup("multiple connection attempts failed",
                           [ConnectionRefusedError(111, "refused"), ConnectionRefusedError(111, "refused")])
    assert reach.reason(_connect_error(group)) == "refused"


async def test_a_real_refusal_names_the_port(bare_metal):
    port = _free_port()
    try:
        async with httpx.AsyncClient() as c:
            await c.get(f"http://127.0.0.1:{port}/")
    except httpx.HTTPError as e:
        text = reach.explain(e, f"http://127.0.0.1:{port}", "SABnzbd")
    assert f"nothing is answering on port {port} at 127.0.0.1" in text


def test_localhost_in_docker_says_what_localhost_means(docker):
    text = reach.explain(_connect_error(ConnectionRefusedError(111, "refused")), "http://localhost:8080", "SABnzbd")
    assert "Narratarr’s own container" in text and "192.168.1.10" in text


def test_localhost_outside_docker_is_just_a_refusal(bare_metal):
    text = reach.explain(_connect_error(ConnectionRefusedError(111, "refused")), "http://localhost:8080", "SABnzbd")
    assert "container" not in text and "port 8080" in text


def test_an_unknown_container_name_explains_docker_networks(docker):
    text = reach.explain(_connect_error(socket.gaierror(-2, "unknown")), "http://sabnzbd:8080", "SABnzbd")
    assert "“sabnzbd”" in text and "same Docker network" in text


def test_an_unknown_domain_suggests_the_ip(bare_metal):
    text = reach.explain(_connect_error(socket.gaierror(-2, "unknown")), "http://nas.example.lan:8080", "SABnzbd")
    assert "Docker" not in text and "IP address" in text


# ---- SABnzbd's hostname check -----------------------------------------------------


async def test_sabnzbd_hostname_check_is_not_reported_as_a_bad_key():
    def sab(request):
        return httpx.Response(403, text="Access denied - Hostname verification failed: https://sabnzbd.org/hostname-check")

    async with SABnzbd("http://sabnzbd:8080", "key", transport=httpx.MockTransport(sab)) as client:
        with pytest.raises(SABnzbdError) as err:
            await client.version()
    text = str(err.value)
    assert "API key" not in text
    assert "“sabnzbd”" in text and "host_whitelist" in text and "IP" in text


async def test_sabnzbd_bad_key_is_still_a_bad_key():
    def sab(request):
        return httpx.Response(403, text="API Key Incorrect")

    async with SABnzbd("http://192.168.1.10:8080", "key", transport=httpx.MockTransport(sab)) as client:
        with pytest.raises(SABnzbdError, match="rejected the API key"):
            await client.version()


async def test_sabnzbd_unreachable_says_why(bare_metal):
    port = _free_port()
    async with SABnzbd(f"http://127.0.0.1:{port}", "key") as client:
        with pytest.raises(SABnzbdError, match=f"nothing is answering on port {port}"):
            await client.version()


# ---- Audiobookshelf: other ports, the Docker host, and why nothing was found -----------


async def test_find_at_the_published_port_finds_the_container_on_80(monkeypatch, docker):
    # Inside a Docker network, Audiobookshelf listens on 80; 13378 is only published on the host.
    monkeypatch.setattr(audiobookshelf, "transport", httpx.MockTransport(FakeABS(at=("http://audiobookshelf",))))
    found = await audiobookshelf.find_at("audiobookshelf:13378")
    assert [s["url"] for s in found] == ["http://audiobookshelf"]
    assert "port 80, not 13378" in found[0]["note"]


async def test_find_at_the_right_port_has_no_note(monkeypatch):
    monkeypatch.setattr(audiobookshelf, "transport", httpx.MockTransport(FakeABS(at=("http://192.168.1.10:13378",))))
    found = await audiobookshelf.find_at("192.168.1.10:13378")
    assert [s["url"] for s in found] == ["http://192.168.1.10:13378"] and "note" not in found[0]


async def test_find_nearby_in_docker_tries_the_docker_host(monkeypatch, docker, tmp_path):
    route = tmp_path / "route"
    route.write_text("Iface\tDestination\tGateway \tFlags\n"
                     "eth0\t00000000\t010011AC\t0003\n"  # default route via 172.17.0.1
                     "eth0\t000011AC\t00000000\t0001\n")
    monkeypatch.setattr(audiobookshelf, "ROUTE", str(route))
    assert audiobookshelf._docker_host() == "172.17.0.1"
    monkeypatch.setattr(audiobookshelf, "transport", httpx.MockTransport(FakeABS(at=("http://172.17.0.1:13378",))))
    monkeypatch.setattr(audiobookshelf, "_local_network", lambda: pytest.fail("the host answered; no sweep"))
    assert [s["url"] for s in await audiobookshelf.find_nearby()] == ["http://172.17.0.1:13378"]


def test_the_docker_host_is_only_looked_for_in_docker(monkeypatch, bare_metal):
    monkeypatch.setattr(audiobookshelf, "ROUTE", "/nonexistent")
    assert audiobookshelf._docker_host() == ""


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(audiobookshelf, "transport", httpx.MockTransport(FakeABS(at=())))
    with TestClient(api.app) as c:
        c.app.state.worker.poke = lambda: None
        yield c


async def _nothing(sweep=True):
    return []


def test_find_says_why_nothing_was_found_in_docker(client, monkeypatch, docker):
    monkeypatch.setattr(audiobookshelf, "find_nearby", _nothing)
    r = client.post("/api/audiobookshelf/find", json={"address": ""}).json()
    assert r["servers"] == [] and "runs in Docker" in r["reason"] and "IP address" in r["reason"]


def test_find_at_localhost_in_docker_explains_localhost(client, docker):
    r = client.post("/api/audiobookshelf/find", json={"address": "localhost:13378"}).json()
    assert r["servers"] == [] and "Narratarr’s own container" in r["reason"]


def test_find_at_something_else_says_it_isnt_audiobookshelf(monkeypatch, bare_metal):
    monkeypatch.setattr(audiobookshelf, "transport", httpx.MockTransport(lambda r: httpx.Response(200, text="<html>")))
    with TestClient(api.app) as c:
        c.app.state.worker.poke = lambda: None
        r = c.post("/api/audiobookshelf/find", json={"address": "192.168.1.10:8080"}).json()
    assert r["servers"] == [] and "isn't Audiobookshelf" in r["reason"]
