"""Which download client takes a release.

A grab used to go to the one globally configured client. Routing is an ordered list of rules, each
matching on the indexer that listed the release and the kind of book it is, and the first rule that
matches wins. Order is the precedence model, so it is the user's to set.

Two things matter more than the model:

* **every grab says why.** The decision is logged with the rule that matched, or that nothing did and
  the default took it. When a file lands somewhere unexpected, the log has to answer the question
  without guesswork.
* **nothing changes for an install with no rules.** No routes means the default client, exactly as
  before, because an upgrade must not move anybody's downloads.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from . import config

log = logging.getLogger("narratarr.routing")

VERSION = 1  # the shape of a stored route, so a future change can migrate instead of repair


@dataclass
class Decision:
    """What the resolver decided, and why. `rule` is None when nothing matched."""

    client_id: str = ""
    label: str = ""
    save_path: str = ""
    rule: int | None = None  # the index of the matching rule, 0-based
    reason: str = ""         # one line, written to the log
    problems: list[str] = field(default_factory=list)  # a rule naming something that no longer exists

    @property
    def matched(self) -> bool:
        return self.rule is not None


def routes(settings: config.Settings | None = None) -> list[dict]:
    """The stored rules, oldest format tolerated: a bare list is read as this version."""
    s = settings or config.load()
    raw = getattr(s, "client_routes", None) or {}
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    if isinstance(raw, dict):
        return [r for r in (raw.get("rules") or []) if isinstance(r, dict)]
    return []


def _kind_words(kind: str) -> set[str]:
    """`ebook` also answers to `ebooks`, and `audiobook` to `audiobooks`, so a rule cannot fail on a
    plural the user typed naturally."""
    kind = (kind or "").strip().lower()
    return {kind, kind + "s", kind + "es"} if kind else set()


def matches(rule: dict, indexer: str, kind: str) -> bool:
    """A rule matches when every part it names agrees. A part it leaves empty does not restrict."""
    match = rule.get("match") or {}
    wanted_indexers = [str(i).strip().lower() for i in (match.get("indexers") or []) if str(i).strip()]
    wanted_categories = [str(c).strip().lower() for c in (match.get("categories") or []) if str(c).strip()]
    if not wanted_indexers and not wanted_categories:
        return False  # an empty rule would swallow every release; refuse it rather than surprise

    have_indexer = (indexer or "").strip().lower()
    if wanted_indexers:
        if not have_indexer or not any(have_indexer == w or have_indexer.startswith(w + " ") for w in wanted_indexers):
            return False
    if wanted_categories:
        kinds = _kind_words(kind)
        if not any(c in kinds or c.rstrip("s") in {k.rstrip("s") for k in kinds} for c in wanted_categories):
            return False
    return True


def resolve(settings: config.Settings, indexer: str, kind: str, protocol: str = "") -> Decision:
    """The client that takes this release, the first matching rule, or the default.

    `protocol` is "usenet" or "torrent" when the caller knows it; it only ever decides whether the
    default client is capable of taking this release.
    """
    from .downloaders import entries

    known = {e.id for e in entries(settings)}
    decision = Decision()

    for i, rule in enumerate(routes(settings)):
        client_id = str(rule.get("client") or "").strip().lower()
        if client_id and client_id not in known:
            decision.problems.append(
                f"Rule {i + 1} sends to '{client_id}', which is not a client you have any more."
            )
            continue
        if not matches(rule, indexer, kind):
            continue
        decision.client_id = client_id
        decision.label = str(rule.get("label") or "")
        decision.save_path = str(rule.get("save_path") or "")
        decision.rule = i
        decision.reason = (f"rule {i + 1} matched ({_describe(rule)}, kind {kind or 'any'}) "
                           f"-> {client_id or 'default client'}")
        return decision

    # Nothing matched: fall through, which is what every grab did before routing existed. That means
    # the client the flat setting names for this protocol, so an install with SABnzbd for Usenet and
    # qBittorrent for torrents keeps sending each release where it always went.
    proto = protocol or "torrent"  # a release with no protocol recorded is a torrent
    flat = str(getattr(settings, "usenet_client" if proto == "usenet" else "torrent_client", "") or "")
    default = flat if flat in known else str(getattr(settings, "default_client", "") or "")
    if default in known:
        decision.client_id = default
    else:
        of_protocol = [e.id for e in entries(settings) if e.delivery == proto]
        decision.client_id = of_protocol[0] if of_protocol else default
    decision.reason = (f"no rule matched ({indexer or 'unknown indexer'}, kind {kind or 'any'}) "
                       f"-> default client {decision.client_id or 'none configured'}")
    return decision


def _describe(rule: dict) -> str:
    match = rule.get("match") or {}
    parts = []
    if match.get("indexers"):
        parts.append("indexer " + ", ".join(str(i) for i in match["indexers"]))
    if match.get("categories"):
        parts.append("category " + ", ".join(str(c) for c in match["categories"]))
    return "; ".join(parts) or "nothing"


def coverage(settings: config.Settings | None = None, indexers: list[str] | None = None) -> dict:
    """Which indexers a rule covers, and which fall through to the default.

    The most useful diagnostic in the feature: a typo'd indexer name is invisible otherwise, because
    the release is simply taken by the default client and nothing complains.
    """
    from .downloaders import entries

    s = settings or config.load()
    rules = routes(s)
    known = [e.id for e in entries(s)]
    seen: dict[str, int | None] = {}
    for name in indexers or []:
        hit = None
        for i, rule in enumerate(rules):
            wanted = [str(x).strip().lower() for x in ((rule.get("match") or {}).get("indexers") or [])]
            if wanted and (name or "").strip().lower() in wanted:
                hit = i
                break
        seen[name] = hit
    return {
        "covered": {k: v for k, v in seen.items() if v is not None},
        "uncovered": [k for k, v in seen.items() if v is None],
        "unknown_clients": [
            f"Rule {i + 1} -> {rule.get('client')}"
            for i, rule in enumerate(rules)
            if rule.get("client") and str(rule["client"]).strip().lower() not in known
        ],
    }


def validate(value, settings: config.Settings) -> dict:
    """A stored routes block: versioned, rules ordered, every client it names must exist.

    `settings` is passed in rather than loaded, because this runs inside `config.update()` while the
    settings lock is held.
    """
    from .downloaders import entries

    if isinstance(value, str):  # the settings form posts hidden inputs as text
        try:
            value = json.loads(value or "{}")
        except ValueError:
            raise ValueError("routes has to be an object with a version and a list of rules")
    if value in (None, "", []):
        return {"version": VERSION, "rules": []}
    if isinstance(value, list):
        value = {"version": VERSION, "rules": value}
    if not isinstance(value, dict):
        raise ValueError("routes has to be an object with a version and a list of rules")
    rules_in = value.get("rules") or []
    if not isinstance(rules_in, list):
        raise ValueError("routes.rules has to be a list")
    known = {e.id for e in entries(settings)}
    out = []
    for raw in rules_in:
        if not isinstance(raw, dict):
            raise ValueError("each route has to be an object")
        match = raw.get("match") or {}
        indexers = [str(i).strip() for i in (match.get("indexers") or []) if str(i).strip()]
        categories = [str(c).strip() for c in (match.get("categories") or []) if str(c).strip()]
        if not indexers and not categories:
            raise ValueError("a route has to match an indexer, a category, or both")
        client_id = str(raw.get("client") or "").strip().lower()
        if not client_id:
            raise ValueError("a route has to name a client")
        out.append({
            "match": {"indexers": indexers, "categories": categories},
            "client": client_id,
            "label": str(raw.get("label") or ""),
            "save_path": str(raw.get("save_path") or ""),
        })
    return {"version": int(value.get("version") or VERSION), "rules": out}
