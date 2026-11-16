"""Is this torrent already on the cloud service, or will it have to fetch it?

WHY THIS EXISTS
    TorBox charges an uncached add against a strict budget: POST /torrents/createtorrent
    is limited to 60 an hour per token for uncached torrents, 300/min when cached. If
    the user also runs qBittorrent, an uncached release is better sent there: it
    downloads, it seeds, other people keep the swarm alive, and the TorBox budget is
    not spent on something the user's own machine can do.

FOUR ANSWERS, NOT TWO
    cached       the service has it; sending it there downloads immediately
    uncached     the service says it does not have it
    unknown      the service did not give a usable answer (an error, an empty body it
                 does not document, or an identification problem on our side)
    unavailable  this service has no cache check that Narratarr trusts

    Only `uncached` may divert a grab. Turning `unknown` into `uncached` would send
    every release to qBittorrent whenever a provider had a bad minute, which is exactly
    the sort of silent misroute that makes a feature untrustworthy. `unavailable` is a
    statement about the provider, not about the torrent.

    There is no `pending`: the check is one request with a short timeout, and a caller
    that gets no answer gets `unknown` with the reason attached. Pending would only be
    honest if the answer arrived later, and nothing here waits for that.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

CACHED = "cached"
UNCACHED = "uncached"
UNKNOWN = "unknown"
UNAVAILABLE = "unavailable"

_BTIH = re.compile(r"urn:btih:([0-9a-zA-Z]{32,40})", re.IGNORECASE)


def infohash_from_payload(kind: str, payload: str | bytes) -> str:
    """The torrent's info hash, or "" when it cannot be known for certain.

    A magnet carries it. A .torrent file carries it inside the bencoded `info` dict,
    which needs a bencode parser Narratarr does not ship, so a .torrent without a hash
    from its indexer comes back empty rather than guessed at. An empty hash means the
    cache check cannot run, and the caller says so instead of pretending.
    """
    if kind == "magnet" and isinstance(payload, str):
        inner = parse_qs(urlparse(payload).query).get("xt", [""])[0]
        found = _BTIH.search(inner) or _BTIH.search(payload)
        if found:
            token = found.group(1)
            # 32 characters is base32; 40 is hex. Store hex, which is what the APIs take.
            if len(token) == 32:
                import base64

                try:
                    return base64.b32decode(token.upper()).hex()
                except Exception:  # noqa: BLE001 - a malformed magnet is a miss, not a crash
                    return ""
            return token.lower()
    return ""


async def torrent_state(cloud, infohash: str) -> tuple[str, str]:
    """Ask a debrid client about one hash. Returns (state, sentence for the user)."""
    label = getattr(cloud, "label", "The cloud service")
    if not infohash:
        return UNKNOWN, (
            f"Narratarr couldn't identify this torrent's hash, so it can't ask {label} "
            "whether it's cached."
        )

    check = getattr(cloud, "cached", None)
    if check is None:
        return UNAVAILABLE, f"{label} doesn't say whether a torrent is already cached."

    try:
        answer = await check(infohash)
    except Exception as e:  # noqa: BLE001 - any failure is unknown, never a miss
        reason = getattr(e, "code", "") or type(e).__name__
        return UNKNOWN, f"Couldn't ask {label} whether this is cached ({reason})."

    if answer is True:
        return CACHED, f"{label} already has this torrent."
    if answer is False:
        return UNCACHED, f"{label} doesn't have this torrent yet."
    return UNKNOWN, f"{label} didn't give a clear answer about whether this is cached."
