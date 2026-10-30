# Changelog

All notable changes to Narratarr. The app shows this under **Settings → Updates**.

## 0.7.0 — 2026-10-30

- **Ebooks.** Want any book as an **audiobook, an ebook or both**: choose when you add it to Wanted, or open a book in your library and press **Want it too** on the format you don't have. Ebooks are found in your indexers' ebook categories (`7020`, *Books › EBook*, by default; change it under **Settings → Prowlarr**), and one file is kept per book, preferring EPUB, then AZW3, MOBI and PDF.
- **Next to its audiobook.** The ebook goes into the audiobook's folder, or the audiobook into the ebook's, which is how Audiobookshelf shows the two as one book with both a player and a reader. Removing one format with its files leaves the other alone.
- **Open Library** on Discover, for books that never became audiobooks: trending, twelve subjects, and search. Its books are wanted as ebooks unless you pick otherwise.
- **Read in Narratarr.** EPUBs open in a built-in reader that turns pages like a book: two pages side by side on a wide screen, one on a phone. Tap or swipe the edges, or use the arrow keys. Choose Paper, Sepia or Night (or follow your system), the text size and the font, and one page or two on a wide screen. The contents show each chapter's number and where you are, and the footer shows the page and roughly how long the book has left. On a phone, the contents and the text and theme settings rise from the bottom, within reach of your thumb. It remembers your place, and **Continue reading** sits next to Continue listening in your library. PDFs open in your browser's viewer, and every ebook can still be downloaded for Apple Books, Kindle, Kobo or any reading app.
- Books from the internet can't do anything in the reader: their pages run no scripts, load nothing from outside Narratarr, and can't send forms.
- In your library, ebooks wear an **Ebook** badge, show their whole cover and how far you've read. Filter by format.
- **Import** brings in ebook files too, named from the EPUB itself where possible. An ebook can be left where it is.
- **API:** requests take an optional `format` (`audiobook` or `ebook`), and every request now says its format. Lists still show audiobooks unless an app asks for `format=ebook` or `format=all`, so apps built for 0.6.0 see exactly what they did. See `docs/api.md`.

## 0.6.0 — 2026-10-29

- **An API for apps: request books without opening Narratarr.** Apps such as audiobook players can sign in with your account and then search Audible's catalog, request a book, and follow it from Wanted to in your library. It's versioned (`/api/v1`) and only ever grows, so apps built on it keep working across releases. Developers: see `docs/api.md`, or `/api/v1/docs` on your server.
- **Apps signed in** appear under the account button, with when and where each was last used, and you can disconnect any of them. Changing your password, or an admin removing your account, disconnects every app too.
- Apps only need your password once, to sign in; after that they use tokens of their own. Those tokens only work on the API, so they can't touch settings or users. Tokens are stored only as hashes. A stolen refresh token gets the app signed out the moment it's used. Sign-ins, searches and requests have limits.
- **Harder to guess passwords.** Every sign-in attempt now counts before the password is checked, so a burst of simultaneous guesses can't slip past the limit. Attempts are limited per account as well as per address, and IPv6 addresses count by network. Your home network, and networks you've signed in from before, are never held back by the account limit, so nobody can lock you out just by knowing your username. Password checks run two at a time, so a flood of sign-ins can't exhaust memory, and checking your current password when changing it has the same limits.
- `NARRATARR_TRUSTED_PROXIES`: behind a reverse proxy, sign-in limits apply to each visitor rather than to everyone at once.
- **Audible searches find the best matches again.** Audible numbers its result pages from 0, and Narratarr was asking for page 1 first. Searches skipped the 40 most relevant results, Bestsellers and New releases started on their second page, and following an author missed their 50 newest books.
- Narratarr now remembers who requested each book.
- The web UI's internal API is no longer published at `/docs`.

## 0.5.6 — 2026-10-29

- **Audiobookshelf setup guide.** Settings → Audiobookshelf → *Setup guide* asks where Audiobookshelf runs (this machine or another) and how it's installed (Docker or directly), guesses both from what Narratarr can see, and shows the exact steps with your own folders filled in and a Copy button on every command. It ends with **Check now**, which asks Audiobookshelf to rescan and tells you how many of your books it can see, and which are missing.
- When Narratarr and Audiobookshelf share a server but talk over its public address, the guide suggests reconnecting through the machine itself, so Narratarr's key stays off the internet.

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
