# Changelog

All notable changes to Narratarr. The app shows this under **Settings → Updates**.

## Unreleased

- **Torznab** as a search source: Jackett, Prowlarr's Torznab endpoint, or any indexer that speaks the protocol. Written against the protocol, not one vendor; the API key is stripped from every stored link whichever parameter name the indexer uses.

## 0.5.5 — 2026-10-29

- **Accounts.** Everyone signs in now. Admins run settings, download clients, imports and users; listeners browse, request and listen. Each person keeps their own place in every book and their own Continue listening shelf. Add people under **Settings → Users**; everyone can change their own password from the account button in the sidebar.
- **After updating,** Narratarr asks you to create the admin account the first time you open it, and your listening progress so far becomes that account's. From another network it also asks for the setup code in Narratarr's log. If you used `NARRATARR_PASSWORD`, it becomes the password of an admin account called `admin` instead.
- **Home network shortcut.** Settings → Users → *Don't ask for a password on my home network* skips sign-in for this machine and your LAN. Off by default, and never applies to requests through a reverse proxy.
- The installer creates the admin account as part of setup, and `narratarr users add|list|passwd|role|remove` manages accounts from a terminal, including resetting a forgotten admin password.

## 0.5.0 — 2026-10-29

- **Connect Audiobookshelf in a click.** Press **Find** and Narratarr looks for Audiobookshelf on this computer, in Docker and across your home network, or at an address you type, public IPs and domains included. Sign in once and it creates its own API key; your password isn't saved. Pick the library from a list instead of hunting for its ID. Plain-http addresses on the internet ask before sending a password.
- A rescan Audiobookshelf rejects (a revoked key, a deleted library) is now logged instead of silently ignored, and **Test** checks the connection.

## 0.4.1 — 2026-10-29

- **Deluge** alongside qBittorrent and Transmission for torrents. Deluge's web UI has no username, only a password; torrents are labelled `narratarr-<book id>` when its Label plugin is enabled, so Narratarr only ever removes torrents it added.

## 0.4.0 — 2026-10-29

- **Update button.** Narratarr checks GitHub once a day for a new release and shows a badge in the sidebar. Native installs can update themselves with one click; Docker users get the command to run. Turn the check off under Settings → Updates.
- **What's new** inside the app: this changelog, under Settings → Updates.

## 0.3.2 — 2026-10-29

- Discover loads three AudiobookBay pages at a time (27 books), since the site shows only 9 per page.

## 0.3.1 — 2026-10-29

A hardening release after a full review of the code.

- **Files are never lost.** Remove keeps files that exist nowhere else (a stopped move-import or Usenet download) and says where it put them; a download never overwrites a folder you already had; two books never share a folder; interrupted imports resume instead of wiping what was already moved; Stop and Remove wait for file work to finish; a torrent you (or another book) added is never deleted; same-named Real-Debrid files no longer overwrite each other.
- **Less leaves your machine.** Fonts are bundled, so the UI never contacts Google; pages send no referrer; release names stay home when smart search is off; error messages carry no URLs; passwords are fully masked; the settings file is private from its first byte.
- **The local API can't be driven by other websites**: cross-site requests and unknown hostnames are refused (`NARRATARR_HOSTS` for reverse proxies). Pointing a service at a new address drops its saved key. A warning is printed when Narratarr is reachable from the network with no password.
- **Bounded memory**: every cache has a size limit, background tasks are kept, a stuck download gives up after a week, cover and release fetches are size-capped.

## 0.3.0 — 2026-10-29

- **NZBGet** alongside SABnzbd for Usenet.
- **qBittorrent and Transmission** alongside Real-Debrid for torrents. Finished torrents are hard-linked into the library so they keep seeding.
- **Wanted list**: an Audible catalog tab to add any book, automatic searches on Prowlarr and LibriVox, and a release picker.
- **Follow authors and series**: new releases go to Wanted automatically.
- **Import books you already have** (copy, move or leave in place), plus a watched import folder.

## 0.2.0 — 2026-10-28

- **Prowlarr** as a Discover tab: one search covers every indexer.
- **SABnzbd** for Usenet releases, with path mapping for separate containers.
- Release names are matched to real books, so they're filed under the right author and title.

## 0.1.0 — 2026-10-28

First public release: LibriVox and Old-Time Radio sources, typo-tolerant search, one-click downloads, the built-in player, the guided installer and the Docker image.
