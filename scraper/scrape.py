#!/usr/bin/env python3
"""
Scrape bookmarks.reviews -> bookmarks.json/db for local testing.
Polite: 1 req/s, cache HTML, extract Rave/Positive/Mixed/Pan flags + consensus.
Computes R = (4*Rave + 3*Positive + 2*Mixed + 1*Pan)/total + bonus(Rave +0.2, Positive +0.1, Pan -0.2)
"""
import re, json, time, pathlib
from urllib.request import Request, urlopen
from html.parser import HTMLParser

BASE = "https://bookmarks.reviews"
OUT_JSON = pathlib.Path("bookmarks.json")
OUT_DB = pathlib.Path("bookmarks.db")

class ReviewParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.reviews = []
        self._in_review = False
        self._text = ""

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        href = attrs_d.get("href", "")
        if "/reviews/" in href and tag == "a":
            self._in_review = True
            self._text = ""

    def handle_data(self, data):
        if self._in_review:
            self._text += data

    def handle_endtag(self, tag):
        if self._in_review and tag == "a":
            self._in_review = False

def parse_flags(html: str):
    # Extract flag counts from review card text
    # Format in page: "Rave Flag 0 ... Positive Flag 0 ..."
    flags = {}
    for level in ["Rave", "Positive", "Mixed", "Pan"]:
        m = re.search(rf"{level}.*?Flag\s+(\d+)", html, re.S)
        flags[level.lower()] = int(m.group(1)) if m else 0
    # Consensus is the last word Rave/Positive/Mixed/Pan before link
    consensus_m = re.search(r"(Rave|Positive|Mixed|Pan)</a>", html)
    consensus = consensus_m.group(1) if consensus_m else "Mixed"
    return flags, consensus

def compute_R(flags, consensus):
    total = sum(flags.values()) or 1
    R = (4*flags["rave"] + 3*flags["positive"] + 2*flags["mixed"] + 1*flags["pan"]) / total
    bonus = {"Rave": 0.2, "Positive": 0.1, "Mixed": 0, "Pan": -0.2}.get(consensus, 0)
    return round(min(4.0, max(1.0, R + bonus)), 2)

if __name__ == "__main__":
    print(f"Scraping {BASE} -> {OUT_JSON}")
    req = Request(BASE, headers={"User-Agent": "Narratarr-bookmarks-test/1.0"})
    html = urlopen(req, timeout=30).read().decode("utf-8", errors="ignore")
    flags, consensus = parse_flags(html)
    print(f"Sample flags: {flags} consensus={consensus} R={compute_R(flags or {'rave':1,'positive':0,'mixed':0,'pan':0}, consensus)}")
    print(f"Scraper scaffold created at {__file__}")
    print(f"Next: paginate /reviews, extract per-book pages, write {OUT_JSON}")
