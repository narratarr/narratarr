# Bookmarks Ratings — Local Test Plan (DO NOT PUSH UPSTREAM YET)

**Branch:** `bookmarks-ratings-local-test` off `0.8.7` (currently on curator8888/narratarr fork)
**Dataset repo (future):** `github.com/narratarr/bookmarks-ratings` — weekly GH Action
**Local test dataset:** `bookmarks.json` / `bookmarks.db` in repo root (gitignored for now, committed to branch for test)

## Feature
On `/#/discover`, for each indexer category shelf, show under each book name:
`★ 3.4 · Rave(5) Pos(3) Mix(1) Pan(0) — Rave` linked to `https://bookmarks.reviews/reviews/<slug>`
And add `Sort: Rating / Popular / Newest` per category (query param `sort=rating`, server-side).

**Attribution:** `Ratings from Bookmarks, courtesy Lit Hub` footer per Discover load.

## Local Test Wiring (what this branch does)
1. **Scraper:** `scraper/scrape.py` — 1 rps, cache HTML, parse flags + consensus, compute R, write `bookmarks.json` + `bookmarks.db`. Run manually: `python scraper/scrape.py`
2. **Reader:** `narratarr/bookmarks.py` — loads DB, `lookup(title, author)` with rapidfuzz `token_sort_ratio >=88` + surname match
3. **Backend:** `narratarr/api.py /api/discover` — enrich with `bookmarks: {R, consensus, slug_url, counts}`, honor `?sort=rating`
4. **Frontend:** `Discover.svelte` (or equivalent) — rating row + sort toggle + empty state ("No Bookmarks rating yet") + attribution
5. **Seed:** `bookmarks.json` with ~5 sample entries so `sort=rating` works without full crawl

## Weekly GitHub flow (for later, not in this branch)
- New repo `narratarr/bookmarks-ratings` with workflow `scrape.yml` (cron Mon 04:00 UTC, `workflow_dispatch`)
- `pip install -r scraper/requirements.txt && python scraper/scrape.py --out bookmarks.db && python scraper/dump.py --json`
- `git commit + gh release create ratings-YYYYMMDD` — instances `GET .../releases/latest/download/bookmarks.db` with `If-None-Match`

## How to test locally
```bash
git checkout bookmarks-ratings-local-test
python scraper/scrape.py  # creates bookmarks.json (~1MB)
docker build -t narratarr:bookmarks-test .
docker compose up --build  # mount ./bookmarks.db:/app/data/bookmarks.db
# Open http://10.2.3.144:8484/#/discover → check rating row + sort toggle
```

## Not yet done
- Full paginator for all 5000 Bookmarks pages
- Release asset CDN (raw.githubusercontent vs Releases)
- Backfill CLI: `narratarr ratings backfill`

**Do not push to `narratarr/narratarr` upstream.** Push to `curator8888/narratarr:bookmarks-ratings-local-test` for local testing only.
