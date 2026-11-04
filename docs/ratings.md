# Ratings on book cards

Discover can show a star score on each book card, taken from the average Audible publishes for the
audiobook edition. It is on by default and can be switched off in **Settings → Search & sources →
Rating metadata**. This page explains where the number comes from, how a book is matched to it,
what is cached, and what the feature cannot do.

## Where the data comes from

Audible's own catalogue API, and the community-run **Audnex** mirror of it at `api.audnex.us`, which
serves a book by its ASIN. Both are unofficial: neither is a published, supported interface, and the
mirror is run by volunteers. That is one of the reasons the feature is a setting rather than
something always on. Switch it off and Narratarr makes no requests of either.

Audnex answers per region, and a given ASIN only answers in the marketplace that sells it. So the
settings take an **ordered list** of storefronts (`us`, `uk`, `ca`, `au`, `in`, `de`, `fr`, `it`,
`es`, `jp`). They are tried in turn and the first that has the book wins. The storefront that
answered is remembered for that book, so the usual case is one request rather than one per region.
Each row has a **Test** button that asks that storefront about a known title and reports whether it
answers, so you can see which of your regions work rather than inferring it from blank cards.

There is one more step worth knowing about: after your list, Narratarr also asks the storefront the
book itself came from. Every ASIN we hold was minted by the Audible catalogue, so its home region is
a fact rather than a guess. Without that step, a `uk`-only list would show nothing for a book minted
in the `us` catalogue. It turned a 4-of-20 test run into 19-of-20.

## How a book is matched

The gate is deliberately strict, and it is in one place (`narratarr/ratings/match.py`):

* the title must match **exactly** once normalised: case folded, accents folded, punctuation
  stripped, and a trailing subtitle or bracket group removed
* the surname from the query author must appear in the returned authors

Anything weaker is refused and the card reads as unrated. A wrong score is worse than a blank one:
a confident `★ 4.6` on the wrong novel is a lie the reader cannot detect, while an empty slot is
visible and honest. That is the whole reason the gate is strict rather than fuzzy.

Two consequences worth stating, because they are the bulk of the refusals in practice:

* **bundles and rips are not in the catalogue.** "Series 1-4" or a self-published rip has no ASIN,
  so it cannot be rated by this method at all
* **print-only titles are not in an audiobook catalogue.** A book that never had an audiobook
  edition simply will not be found

## What is cached, and for how long

Everything lives in `ratings.sqlite` in your data directory. It is not committed, not synced, and
safe to delete; the worst case is that ratings are fetched again.

| What | Lives for | Why |
|---|---|---|
| A title-and-author → ASIN mapping | forever | the book's identity does not change |
| A rating | 30 days | an average drifts as new listeners rate it |
| A refusal ("exists, nobody has rated it") | 7 days | worth asking again sooner than a rating |
| A miss ("could not find it") | 7 days | same |

Two kinds of row, not one. A **mapping** row records that a title resolved to an ASIN; a **rating**
row records what that ASIN scored, and can say `unrated`. Keeping them apart is what lets a card say
"No Rating Available" (we found your book, nobody has scored it) rather than "No Rating" (we could
not identify it). Those are different facts and the card shows both.

Nothing is fetched while a page is loading. A card with no cached rating comes back empty, the book
is queued, and the score appears the next time you search.

## What the card shows

| State | The card |
|---|---|
| Rated | `★ 4.9` and an **Audible** mark; the tooltip carries the ASIN |
| Exists, unrated | **No Rating Available** |
| Not identified | **No Rating** |
| Ratings switched off | nothing at all, and no requests |

Sorting by rating orders by the score itself, highest first, with unrated books last. The pill
prints the raw score, so the order has to be the raw score; ordering by anything else would show a
column of numbers that does not descend.

## How to keep it current

Two paths, and most people need only the first.

**As you browse.** The background filler runs whenever cards are shown, one request a second, and
fills whatever is missing. Coverage builds up on the shelves you actually look at. This is the
default and needs no setup.

**Bulk, before browsing.** `scripts/prewarm_ratings.py` takes a JSON list of `{title, author}` (or a
CSV with those columns) and fills the cache offline, through the same gate and the same rate limit
as the runtime, so a prewarmed book resolves exactly as a browsed one. It prints a running tally of
matched, rated, unrated and refused, and it is resumable, so an interrupted run costs only the book
it was on.

```bash
python scripts/prewarm_ratings.py my-library.json
python scripts/prewarm_ratings.py my-library.csv --region uk --region us
python scripts/prewarm_ratings.py my-library.json --reset   # re-fetch after scores drift
```

Measured on real shelves: about **210 books a minute**, limited by the rate limit rather than the
script, with roughly **60-65% of popular-shelf titles matching**. The long tail is far worse, for the
bundle and print-only reasons above. Do not expect a personal library of rips to reach those numbers.

If you want stable coverage rather than whatever the filler has reached, keep a saved list and run
the prewarm once a month: scores drift, and new books appear.

## Limitations, stated plainly

* **Editions and omnibuses.** Audible sells a specific edition. A different recording, a box set or
  an omnibus is a different ASIN with a different average, and the strict gate will often refuse it
  rather than score the wrong one. That is the intended trade.
* **ASINs are marketplace-specific.** A book sold only in the US storefront will not answer from the
  UK one, which is why the region list exists and why the book's home region is tried as well.
* **One request a second.** Deliberate: this is somebody else's volunteer-run mirror. A large
  prewarm run takes time and that is by design.
* **A book Audible does not sell cannot be rated this way.** No amount of retrying changes that.
* **Unofficial endpoints.** Either can change or disappear. The feature degrades to blank cards
  rather than breaking a search, and it can be switched off entirely.
* **No count is published.** Audnex returns an average and nothing else, so the pill shows the score
  without a number of ratings, and there is no weighting applied to the sort.
