#!/usr/bin/env python3
"""Scrape Bookmarks books listed in the WordPress bookmark sitemaps.

The sitemap (/wp-sitemap.xml) enumerates every book page as
https://bookmarks.reviews/bookmark/<slug>/. Counting the per-critic reviews on
https://bookmarks.reviews/reviews/all/<slug>/ gives exact Rave/Positive/Mixed/Pan
totals (the book card renders only 10 flag slots and truncates).

Usage: scrape_sitemap.py [--parts 8,9] [--limit 600] [--out bookmarks_full.json]
Resumable: slugs already in the output file are skipped.
"""
import argparse
import json
import os
import re
import time
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
BASE = "https://bookmarks.reviews"
TIERS = ("rave", "positive", "mixed", "pan")
LABEL = {"rave": "Rave", "positive": "Positive", "mixed": "Mixed", "pan": "Pan"}
BONUS = {"Rave": 0.2, "Positive": 0.1, "Mixed": 0.0, "Pan": -0.2}


def get(url, tries=3):
    for n in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception:
            if n == tries - 1:
                raise
            time.sleep(2 * (n + 1))


def sitemap_slugs(parts):
    slugs = []
    for n in parts:
        xml = get(f"{BASE}/wp-sitemap-posts-bookmark-{n}.xml")
        for loc in re.findall(r"<loc>([^<]+)</loc>", xml):
            m = re.search(r"/bookmark/([^/]+)/?$", loc)
            if m:
                slugs.append(m.group(1))
    return slugs


def parse(html):
    m = re.search(r'<title>All Book Marks reviews for (.+?) by (.+?) Book Marks</title>', html)
    if not m:
        return None
    counts = {t: 0 for t in TIERS}
    for found in re.findall(r'class="review_rating\s+(\w+)"', html):
        k = found.lower()
        if k in counts:
            counts[k] += 1
    total = sum(counts.values())
    if not total:
        return None
    consensus = LABEL[max(TIERS, key=lambda t: counts[t])]
    R = (4 * counts["rave"] + 3 * counts["positive"] + 2 * counts["mixed"] + counts["pan"]) / total
    R += BONUS.get(consensus, 0.0)
    return {"title": m.group(1).strip(), "author": m.group(2).strip(),
            "flags": counts, "consensus": consensus,
            "R": round(max(1.0, min(4.0, R)), 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parts", default="8,9")
    ap.add_argument("--limit", type=int, default=600)
    ap.add_argument("--out", default="bookmarks_full.json")
    ap.add_argument("--newest-first", action="store_true", default=True)
    a = ap.parse_args()
    parts = [int(p) for p in a.parts.split(",") if p.strip()]

    existing = {}
    if os.path.exists(a.out):
        try:
            for e in json.load(open(a.out)):
                existing[e["slug"]] = e
        except Exception:
            pass

    slugs = sitemap_slugs(parts)
    if a.newest_first:
        slugs = list(reversed(slugs))
    todo = [s for s in slugs if s not in existing][: a.limit]
    print(f"parts={parts} listed={len(slugs)} have={len(existing)} fetching={len(todo)}", flush=True)

    ok = bad = 0
    for i, slug in enumerate(todo, 1):
        try:
            e = parse(get(f"{BASE}/reviews/all/{slug}/"))
        except Exception as ex:
            bad += 1
            print(f"{i:>4}/{len(todo)} {slug:<46} ERR {type(ex).__name__}", flush=True)
            time.sleep(1)
            continue
        if e:
            e["slug"] = slug
            e["url"] = f"{BASE}/reviews/{slug}/"
            existing[slug] = e
            ok += 1
        else:
            bad += 1
        if i % 20 == 0:
            json.dump(list(existing.values()), open(a.out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
            print(f"{i:>4}/{len(todo)} ok={ok} bad={bad} total={len(existing)} last={slug}", flush=True)
        time.sleep(1)

    json.dump(list(existing.values()), open(a.out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print(f"\nDONE ok={ok} bad={bad} total={len(existing)} -> {a.out}", flush=True)


if __name__ == "__main__":
    main()
