# Download routing

Narratarr can send a release to a particular download client, based on which indexer found it and
what kind of book it is. This page explains the rules, what happens when one does not match, and how
to check a route before trusting it.

If you never add a rule, nothing changes: every grab goes where it went before.

## What routing is

Downloads used to go to the one client configured in Settings. Now there is a **list of clients**
and an **ordered list of rules**:

- a release is matched against the rules **top to bottom**, and the **first match wins**
- if no rule matches, it falls through to a **default client**
- a rule may also set its own **label** (the category the client files it under) and its own
  **save path**, each falling back to the client's own when left empty

Order is the precedence model. Move a rule up and it starts winning.

## The migration story, and why the default is per protocol

Upgrading carries your existing setup across: the flat client settings become entries in the client
list, one **per protocol** that is actually configured.

Two entries, not one, because a working install can have SABnzbd for Usenet and qBittorrent for
torrents at the same time. Collapsing those into a single client would have silently dropped one of
them, and nobody's working setup may break on upgrade.

The flat client settings from older versions (`torrent_client`, `usenet_client` and their address,
credential, category and folder fields) **still work and are still read**. Settings no longer shows
them, but they are part of the settings API and they still take precedence for the protocol default.
They go away in a later change that removes them along with the compatibility path that keeps them
in step with the list.

For the same reason the default is resolved **per protocol** when no rule matches:

1. the client you have set up for this protocol (Usenet or torrent)
2. then the default client you picked
3. then the first client that can take this delivery at all

A single flat default would send a torrent to SABnzbd on an install that has both, which is the
failure this ordering exists to prevent. A release with no protocol recorded is treated as a torrent.

## How matching works

A rule has two optional parts. **At least one must be given**; a rule that matches nothing is refused
when you save it, because it would swallow every release.

- **Indexer.** The name the indexer reports, e.g. `The Pirate Bay`. Prowlarr appends a
  disambiguating suffix when two indexers share a name (`The Pirate Bay (Prowlarr)`), and that
  suffix is tolerated, so renaming an indexer upstream does not silently kill your rule.
- **Category.** `audiobook` or `ebook`, and the plural forms work too. A category-only rule covers
  every indexer, which is how "all audiobooks to Deluge" is one rule rather than five.

A part you leave empty does not restrict anything. Both parts given means both must agree.

### What a rule can key on, and what it cannot

The indexer picker is filled from the sources that actually hand releases to a download client, so
every name in it can fire.

**AudiobookBay is a special case worth knowing.** Its listings do not carry a per-indexer name, so
Narratarr files them under the source's own name, `AudiobookBay`, and the picker offers exactly that
spelling. A rule keyed on `AudiobookBay` therefore matches every AudiobookBay release. What you
cannot do is key on a specific uploader or tracker inside the site, because there is no such field to
match.

**Some sources are not routable at all**, and deliberately so:

| Source | Why no rule can match |
|---|---|
| Audible catalog | a catalogue for browsing, not an indexer; releases come from somewhere else |
| Open Library | the same: book details, not releases |
| LibriVox | a free direct download; no client is involved, so there is nothing to route |
| Old-Time Radio | the same |

The picker does not offer them. A rule keyed on one of those names could never fire, which is why
they are absent rather than disabled.

## What the log says

Every grab writes one line, whether a rule matched or not. When a file lands somewhere unexpected,
the log answers the question without guesswork.

A matched rule:

```
Book 1 (The Pirate Bay, kind audiobook): rule 1 matched (indexer The Pirate Bay; kind audiobook) -> deluge
```

A fall-through:

```
Book 1 (Some Other Tracker, kind audiobook): no rule matched (Some Other Tracker, kind audiobook) -> default client qbittorrent
```

## When a rule names a client that no longer exists

Removing a client does not silently break routing. The rule stays in the list, and:

- the grab logs a warning naming the rule and the missing client
- the release still downloads, on the default client, rather than failing
- **Coverage**, under Settings → Downloads → Routing, lists the broken rule

Fix the rule, or delete it, and the warning stops.

## Testing a route before trusting it

Three ways, cheapest first:

1. **Test** on a client row asks that daemon for its version. It works on an unsaved entry, so you
   can check a client before saving it.
2. **Test** on a rule shows what would happen for that rule's indexer and category: the resolved
   client, the label and the save path that would be sent. It grabs nothing.
3. **Coverage** shows which advertised indexers no rule matches, so a typo'd name shows up as
   "uncovered" instead of quietly sending everything to the default.

On Discover, each result row carries a small chip naming the client that would take it. The chip is
a hint: the decision is made again at grab time, because a rule can change between seeing a row and
clicking it.

## Worked examples

### One indexer to one client

Send everything from a private tracker to Deluge, leaving other indexers alone:

| Indexer | Category | Client | Label | Save path |
|---|---|---|---|---|
| `The Pirate Bay` | *(any)* | `deluge` | `torrents` | `/data/books` |

Everything else falls through to the default client, which is exactly what happened before routing
existed.

### Every audiobook to Deluge, whatever found it

One rule replaces one per indexer:

| Indexer | Category | Client | Label | Save path |
|---|---|---|---|---|
| *(none)* | Audiobooks | `deluge` | `audiobooks` | *(the client's own)* |

Leaving the indexer empty is what makes it match all of them. An ebook rule below it can send ebooks
elsewhere, and because order matters, the audiobook rule keeps its claim on audiobooks.

## What it does not do

- **No size or age matching.** A rule matches on the indexer and the kind of book, nothing else.
- **No per-rule retry or priority.** The client's own queue governs what happens after the grab.
- **The chip is a hint, not a promise.** The decision is re-made when you actually grab.
