"""Bookmarks ratings loader: loads bookmarks.json/db and provides lookup(title,author)."""
from __future__ import annotations
import json, pathlib, sqlite3
from typing import Optional

try:
    from rapidfuzz.fuzz import token_sort_ratio
except ImportError:
    token_sort_ratio = None

# Search order: repo root bookmarks.json, data dir, alongside this file
CANDIDATES = [
    pathlib.Path(__file__).parent.parent / "bookmarks.json",
    pathlib.Path("/app/data/bookmarks.json"),
    pathlib.Path("/config/bookmarks.json"),
    pathlib.Path("/app/bookmarks.json"),
]
DB_CANDIDATES = [
    pathlib.Path(__file__).parent.parent / "bookmarks.db",
    pathlib.Path("/app/data/bookmarks.db"),
    pathlib.Path("/config/bookmarks.db"),
    pathlib.Path("/app/bookmarks.db"),
]

def _surname(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    # take last token
    parts = s.replace(",", " ").split()
    return parts[-1].lower() if parts else ""

def _load_entries() -> list[dict]:
    for p in CANDIDATES:
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
    for p in DB_CANDIDATES:
        if p.exists():
            try:
                con = sqlite3.connect(str(p))
                con.row_factory = sqlite3.Row
                rows = con.execute("SELECT slug,title,author,url,rave,positive,mixed,pan,consensus,R FROM bookmarks").fetchall()
                con.close()
                out = []
                for r in rows:
                    out.append({
                        "title": r["title"], "author": r["author"], "slug": r["slug"], "url": r["url"],
                        "flags": {"rave": r["rave"], "positive": r["positive"], "mixed": r["mixed"], "pan": r["pan"]},
                        "consensus": r["consensus"], "R": r["R"],
                    })
                if out:
                    return out
            except Exception:
                continue
    return []

_entries: list[dict] | None = None

def _get_entries():
    global _entries
    if _entries is None:
        _entries = _load_entries()
    return _entries

def reload():
    global _entries
    _entries = None
    return _get_entries()

def format_rating(entry: dict) -> str:
    """★ X.X · Rave(N) Pos(N) Mix(N) Pan(N) — Consensus"""
    f = entry["flags"]
    return f"★ {entry['R']:.1f} · Rave({f['rave']}) Pos({f['positive']}) Mix({f['mixed']}) Pan({f['pan']}) — {entry['consensus']}"

def lookup(title: str, author: str) -> Optional[dict]:
    """Find best match by title fuzzy + surname check. Returns entry or None."""
    entries = _get_entries()
    if not entries:
        return None
    title = (title or "").strip()
    author = (author or "").strip()
    if not title:
        return None
    best = None
    best_score = 0
    surname_q = _surname(author)
    for e in entries:
        # surname gate if both have authors
        if surname_q and e.get("author"):
            if _surname(e["author"]) != surname_q:
                continue
        # title fuzzy
        if token_sort_ratio is None:
            # fallback: exact lower match
            score = 100 if title.lower() == e["title"].lower() else 0
        else:
            score = token_sort_ratio(title.lower(), e["title"].lower())
        if score >= 88 and score > best_score:
            best_score = score
            best = e
    return best

def enrich_item(item: dict) -> dict:
    """Attach bookmarks_rating to a discover item dict in-place, return item."""
    m = lookup(item.get("title", ""), item.get("author", ""))
    if m:
        item["bookmarks_rating"] = {
            "R": m["R"],
            "consensus": m["consensus"],
            "url": m["url"],
            "flags": m["flags"],
            "display": format_rating(m),
        }
    else:
        item["bookmarks_rating"] = None
    return item
