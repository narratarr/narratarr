"""Cover art is proxied, and only for hosts we are configured to use (issue #24).

The complaint: Discover linked cover art straight at `i.omgwtfnzbs.org`, so a reader behind a VPN
still handed their real address to the indexer on every page. These tests pin the three things
that fix needs: the rewrite happens, the allowlist comes from configuration, and the endpoint
cannot be talked into fetching an arbitrary address.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

from narratarr import api, config, imageproxy

INDEXER = "https://prowlarr.example.lan:9696"
IMAGE = "https://i.omgwtfnzbs.org/covers/dune.jpg"


@pytest.fixture
def indexer_configured():
    config.update({"prowlarr_url": INDEXER, "prowlarr_api_key": "k"})
    imageproxy.clear_cache()
    # The indexer has handed us this poster URL, which is what puts its host on the allowlist.
    imageproxy.proxied(IMAGE, from_indexer=True)
    yield
    imageproxy.clear_cache()
    imageproxy._OBSERVED.clear()


def serve(handler):
    """Point the proxy's HTTP client at a test handler instead of the network."""
    imageproxy._TRANSPORT = httpx.MockTransport(handler)
    return handler


def binary(request, body=b"jpeg-data", content_type="image/jpeg", status=200, headers=None):
    return httpx.Response(status, content=body, headers={"content-type": content_type, **(headers or {})})


# ---- the rewrite ---------------------------------------------------------------------------


def test_an_indexer_image_host_is_rewritten_to_the_proxy(indexer_configured):
    """The poster host is not the indexer API host, so `from_indexer` is what makes this work."""
    assert imageproxy.proxied(IMAGE, from_indexer=True) == (
        "/api/image?url=https%3A%2F%2Fi.omgwtfnzbs.org%2Fcovers%2Fdune.jpg"
    )
    assert "i.omgwtfnzbs.org" in imageproxy.allowed_hosts()


def test_a_url_not_from_an_indexer_is_not_rewritten_or_trusted():
    """Nothing can widen the allowlist by asking nicely."""
    assert imageproxy.proxied(IMAGE) == IMAGE
    assert "i.omgwtfnzbs.org" not in imageproxy.allowed_hosts()


def test_an_unrelated_host_is_left_alone():
    """openlibrary and archive.org art is not the indexer; it keeps its own URL."""
    url = "https://covers.openlibrary.org/b/id/123-L.jpg"
    assert imageproxy.proxied(url) == url


def test_a_missing_or_relative_url_is_untouched():
    assert imageproxy.proxied("") == ""
    assert imageproxy.proxied("/static/cover.png") == "/static/cover.png"


def test_the_allowlist_is_derived_from_configuration():
    config.update({
        "prowlarr_url": INDEXER,
        "torznab_url": "https://nzb.example.lan/torznab",
        "abb_base_url": "https://audiobookbay.example",
    })
    assert imageproxy.allowed_hosts() == {
        "prowlarr.example.lan",
        "nzb.example.lan",
        "audiobookbay.example",
    }


def test_nothing_configured_means_nothing_proxied():
    config.update({"prowlarr_url": "", "torznab_url": "", "abb_base_url": ""})
    assert imageproxy.allowed_hosts() == set()
    assert imageproxy.proxied(IMAGE) == IMAGE


# ---- the endpoint --------------------------------------------------------------------------


def test_the_endpoint_streams_the_image_with_its_content_type(indexer_configured):
    serve(lambda request: binary(request, content_type="image/webp"))

    response = TestClient(api.app).get("/api/image", params={"url": IMAGE})

    assert response.status_code == 200
    assert response.content == b"jpeg-data"
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "max-age=86400" in response.headers["cache-control"]


def test_a_host_outside_the_allowlist_is_refused_without_a_request():
    calls = []

    def handler(request):
        calls.append(request.url)
        return binary(request)

    serve(handler)
    config.update({"prowlarr_url": INDEXER, "prowlarr_api_key": "k"})

    response = TestClient(api.app).get(
        "/api/image", params={"url": "http://169.254.169.254/latest/meta-data/"}
    )

    assert response.status_code == 403
    assert calls == [], "an SSRF attempt must not reach the network at all"


def test_a_redirect_off_the_allowlist_is_refused(indexer_configured):
    def handler(request):
        if request.url.host == "i.omgwtfnzbs.org":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/secret"})
        return binary(request)

    serve(handler)

    response = TestClient(api.app).get("/api/image", params={"url": IMAGE})

    assert response.status_code == 403


def test_a_redirect_within_the_allowlist_is_followed(indexer_configured):
    def handler(request):
        if request.url.path == "/covers/dune.jpg":
            return httpx.Response(302, headers={"location": "https://i.omgwtfnzbs.org/real.jpg"})
        return binary(request, content_type="image/png")

    serve(handler)

    response = TestClient(api.app).get("/api/image", params={"url": IMAGE})

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_a_non_image_response_is_refused(indexer_configured):
    serve(lambda request: binary(request, body=b"<html>", content_type="text/html"))

    response = TestClient(api.app).get("/api/image", params={"url": IMAGE})

    assert response.status_code == 502


def test_an_oversized_image_is_refused(indexer_configured):
    serve(lambda request: binary(request, body=b"x" * (imageproxy.MAX_BYTES + 1)))

    response = TestClient(api.app).get("/api/image", params={"url": IMAGE})

    assert response.status_code == 502


def test_an_upstream_error_is_a_502_not_a_500(indexer_configured):
    serve(lambda request: binary(request, body=b"", status=503))

    response = TestClient(api.app).get("/api/image", params={"url": IMAGE})

    assert response.status_code == 502


def test_a_missing_url_parameter_is_refused():
    assert TestClient(api.app).get("/api/image").status_code == 403
