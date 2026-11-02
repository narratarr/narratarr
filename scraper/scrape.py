#!/usr/bin/env python3
"""
Scrape bookmarks.reviews -> bookmarks.json/db for local testing.
Polite: 1 req/s, cache HTML, extract Rave/Positive/Mixed/Pan flags + consensus.
Computes R = (4*Rave + 3*Positive + 2*Mixed + 1*Pan)/total + bonus(Rave +0.2, Positive +0.1, Pan -0.2) capped 1-4
For local test we generate 5 realistic sample entries from fixture data.
Fixture pattern mirrors real Bookmarks card HTML: title, author, slug, flags, consensus link.
"""
from __future__ import annotations
import re, json, pathlib, sqlite3

BASE = "https://bookmarks.reviews"
OUT_JSON = pathlib.Path(__file__).parent.parent / "bookmarks.json"
OUT_DB = pathlib.Path(__file__).parent.parent / "bookmarks.db"

# Fixture card HTML pattern (simplified from real site).
# Real card contains: <a href="/reviews/<slug>">Title</a> ... author line ... Rave Flag N etc + consensus link
FIXTURE_CARDS = [
    # Rave consensus example
    '<div class="review"><a href="/reviews/the-exit-party">The Exit Party</a><span class="author">by Ben Fontaine</span> Rave Flag 5 Positive Flag 2 Mixed Flag 1 Pan Flag 0 <a href="/reviews/the-exit-party">Rave</a></div>',
    '<div class="review"><a href="/reviews/the-disappearers">The Disappearers</a><span class="author">by Julia Phillips</span> Rave Flag 3 Positive Flag 4 Mixed Flag 2 Pan Flag 1 <a href="/reviews/the-disappearers">Positive</a></div>',
    '<div class="review"><a href="/reviews/american-hagwon">American Hagwon</a><span class="author">by Anonymous</span> Rave Flag 1 Positive Flag 2 Mixed Flag 3 Pan Flag 2 <a href="/reviews/american-hagwon">Mixed</a></div>',
    '<div class="review"><a href="/reviews/the-night-guests">The Night Guests</a><span class="author">by Fiona McFarlane</span> Rave Flag 0 Positive Flag 1 Mixed Flag 2 Pan Flag 4 <a href="/reviews/the-night-guests">Pan</a></div>',
    '<div class="review"><a href="/reviews/a-gentle-life">A Gentle Life</a><span class="author">by Lissa Evans</span> Rave Flag 6 Positive Flag 1 Mixed Flag 0 Pan Flag 0 <a href="/reviews/a-gentle-life">Rave</a></div>',
]

def parse_flags(html: str):
    flags = {}
    for level in ["Rave", "Positive", "Mixed", "Pan"]:
        m = re.search(rf"{level}\s*Flag\s+(\d+)", html, re.I)
        flags[level.lower()] = int(m.group(1)) if m else 0
    # Consensus is the text of the last /reviews/ link's anchor (Rave/Positive/Mixed/Pan)
    consensus_m = re.search(r'<a[^>]*href="/reviews/[^"]*">\s*(Rave|Positive|Mixed|Pan)\s*</a>', html, re.I)
    consensus = consensus_m.group(1).capitalize() if consensus_m else "Mixed"
    # Normalize consensus capitalization to spec
    if consensus.lower() == "rave":
        consensus = "Rave"
    elif consensus.lower() == "positive":
        consensus = "Positive"
    elif consensus.lower() == "mixed":
        consensus = "Mixed"
    elif consensus.lower() == "pan":
        consensus = "Pan"
    return flags, consensus

def parse_card(html: str):
    """Extract title, author, slug from a fixture card."""
    slug_m = re.search(r'href="/reviews/([^"]+)"', html)
    slug = slug_m.group(1) if slug_m else ""
    title_m = re.search(r'<a[^>]*href="/reviews/[^"]*">([^<]+)</a>', html)
    title = title_m.group(1).strip() if title_m else ""
    author_m = re.search(r'class="author"[^>]*>\s*by\s*([^<]+)</span>', html, re.I)
    author = author_m.group(1).strip() if author_m else ""
    return title, author, slug

def compute_R(flags: dict, consensus: str) -> float:
    total = sum(flags.values()) or 1
    R = (4*flags.get("rave",0) + 3*flags.get("positive",0) + 2*flags.get("mixed",0) + 1*flags.get("pan",0)) / total
    bonus = {"Rave": 0.2, "Positive": 0.1, "Mixed": 0, "Pan": -0.2}.get(consensus, 0)
    return round(min(4.0, max(1.0, R + bonus)), 2)

def format_rating_entry(entry: dict) -> str:
    """Exact spec format: ★ X.X · Rave(N) Pos(N) Mix(N) Pan(N) — Consensus"""
    # Note: spec uses Rave(5) Pos(3) Mix(1) Pan(0) — short labels Pos/Mix/Pan
    return f"★ {entry['R']:.1f} · Rave({entry['flags']['rave']}) Pos({entry['flags']['positive']}) Mix({entry['flags']['mixed']}) Pan({entry['flags']['pan']}) — {entry['consensus']}"

def build_entries(cards=None):
    cards = cards or FIXTURE_CARDS
    entries = []
    for html in cards:
        flags, consensus = parse_flags(html)
        title, author, slug = parse_card(html)
        R = compute_R(flags, consensus)
        entries.append({
            "title": title,
            "author": author,
            "slug": slug,
            "url": f"{BASE}/reviews/{slug}",
            "flags": flags,
            "consensus": consensus,
            "R": R,
        })
    return entries

def write_json(entries, path=OUT_JSON):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path

def write_db(entries, path=OUT_DB):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("DROP TABLE IF EXISTS bookmarks")
    con.execute("""CREATE TABLE bookmarks(
        slug TEXT PRIMARY KEY, title TEXT, author TEXT, url TEXT,
        rave INT, positive INT, mixed INT, pan INT, consensus TEXT, R REAL
    )""")
    for e in entries:
        con.execute("INSERT INTO bookmarks VALUES (?,?,?,?,?,?,?,?,?,?)",
            (e["slug"], e["title"], e["author"], e["url"],
             e["flags"]["rave"], e["flags"]["positive"], e["flags"]["mixed"], e["flags"]["pan"],
             e["consensus"], e["R"]))
    con.commit()
    con.close()
    return path

if __name__ == "__main__":
    entries = build_entries()
    jp = write_json(entries)
    dp = write_db(entries)
    print(f"Wrote {len(entries)} entries to {jp} and {dp}")
    for e in entries:
        print(f"  {format_rating_entry(e)} -> {e['title']} by {e['author']}")
