# Changelog

All notable changes to Narratarr. The app shows this under **Settings → Updates**.

## 0.8.8 — 2026-11-02

- **Tidy library.** Settings → General → Tidy your library moves the books already in your library into your folder layout (Audiobookshelf style, Author / Series / Title, or your own), named from Narratarr's details or the files' own tags. **Preview changes** lists every book's folder now and where it would go, with the parts that change picked out. You choose which books move, and **Undo** puts the last batch back.
- Every move is a rename on the same disk, so it's instant and Audiobookshelf keeps everyone's place in each book. A book's ebook moves with it or joins its new folder, and folders left empty are cleared away.
- Nothing risky happens quietly. Books whose new folder is taken, that would share a folder, that hold another book, that are downloading, or that would have to be copied to another disk are held back with the reason. Books with no author or title are marked **needs a look**, with a button to fix their details.
- Optionally, while tidying, add the cover, description and narrator files Audiobookshelf reads, and write the details into the audio files' tags (MP3, M4B, FLAC, Ogg, Opus) so every app agrees.

## 0.8.7 — 2026-11-01

- **Narratarr has a Discord**: <https://discord.gg/kB5EgmfBrA>, for help, ideas and news about new versions. It's linked from the sidebar, your account (on phones too), Settings → Updates and What's new.

## 0.8.6 — 2026-11-01

- **Preferred languages.** Settings → Search & sources → Languages: pick the languages your books should be in (say, German). Discover hides books it can tell are in another language, with one click to show them; automatic search never grabs a release in another language, and Pick a release shows each release's language, with the others faded (you can still grab them). Narratarr goes by what the source says, or the release name (`[German]`, `.GER.`, `Hörbuch`, `livre audio`…), never by words in the book's own title, and a book whose language it can't tell is never hidden.
- **Folder layout presets**: Author / Title, Author / Series / Title, and **Audiobookshelf style**, `{author}/{series}/Vol {series_number} - {year} - {title} {{narrator}}`, so Audiobookshelf reads the series, volume, year and narrator straight from the folder name. Double braces keep braces in a name.
- "Unknown Narrator" (or Author) only fills a folder that's that variable alone, so a book with no narrator gets no `{Unknown Narrator}` in its name.
- **An Unraid template**, with Unraid's usual shares and `nobody:users`. The README shows how to add it.

## 0.8.5 — 2026-11-01

- **A fresh look for Discover.** The search bar now says where it's looking ("in Audible catalog ▾") and switches sources from there. Sources are grouped by what they do (free downloads, your indexers, catalogs) with a dot showing which are ready. Audiobooks or Ebooks sits beside the heading. While you browse, one book gets a **Spotlight**, with what you already have by the same author, and a **New from authors you follow** shelf runs above the grid. Every cover shows where the book stands: In library, Downloading 42%, Awaiting approval, Wanted or Failed.
- **The Library** opens with any requests waiting for approval, which an admin can approve, give a release, or decline right there (a listener sees their own and can take them back). Then Continue listening, then everything, with new filters (Listening, Finished), a sort (recently added, recently listened, title, author), and a line under each book saying how far you are, who asked for it, or why it failed.
- **Authors & series**: each author shows how much of their work you have ("14 of 19 in your library") with tags like New book, Downloading or All caught up; followed series show as stacked covers. An author's page groups their books by series, in reading order, with the ones you don't have yet dimmed and one click from Wanted.
- **Activity** shows how many are downloading, waiting and finished today, who each download is for, and finished books grouped by day, with Try again on failures. A side panel shows requests waiting for approval and when Narratarr will next look for Wanted books.
- **The event log.** Settings → Notifications now shows the events Narratarr writes to `docker logs`, newest first, with where each one was sent: ✓ for delivered, ✕ with the reason when a destination failed, or "Log only". Filter by requests, starts, finishes or failures, search for a book or person, copy it, or download it as a `.log` file. It updates by itself while open, and keeps the most recent 500 events. A destination whose last send failed is highlighted.
- A book's details show a request's progress (Asked, Approval, Downloading, In your library) and the rest of its series in your library.
- Time left no longer reads "3h 60m".

## 0.8.1 — 2026-11-01

- **Approve what listeners ask for.** A listener's request now waits under **Library → Awaiting approval** until an admin approves it. Nothing is searched for or downloaded before then. If the listener picked a release, approving downloads exactly that one; otherwise Narratarr starts looking. Admins see a badge on Library, and the **Added to Wanted** notification goes out for every listener request, including a release they picked themselves, saying it's waiting. Requests made through the app API wait too, and still show as `wanted` to apps. This is on by default; turn it off under **Settings → Users → Requests**. While it's on, only admins follow authors and series, since following adds books by itself.
- **Listeners can take back their own requests**, until the book is in the library. Other people's requests, and books already downloaded, stay an admin's to remove.
- **TorBox and Premiumize** for torrents, alongside Real-Debrid and AllDebrid. TorBox connects with a short code, like AllDebrid, or an API key; Premiumize with its API key. Either can remove the torrent (and, for Premiumize, its files in your cloud) once Narratarr has the book. Narratarr only ever removes the folder a transfer made, never the folder it was put in.
- **More folder layout variables:** `{series}`, `{series_number}` and `{year}`, next to `{author}`, `{title}` and `{narrator}`. When a book doesn't have one, that part is left out along with the words around it, so `{author}/{series}/Vol. {series_number} - {title}` files a book that isn't in a series as just `Author/Title`. Settings has a button for each variable and previews a book with and without a series. Imports read series and year from the files' tags. The README lists every variable.
- Saving a folder layout with a variable Narratarr doesn't know, or one that would step outside the library folder, now explains what's wrong instead of quietly using the default.

## 0.8.0 — 2026-11-01

- **Notifications.** A new **Settings → Notifications** page sends word to **ntfy**, **Gotify**, **Discord** or any **webhook** when someone adds a book to Wanted (with who asked for it), and when a download starts, finishes or fails. Choose the events for each destination, and press **Test** to send one straight away. A webhook can carry headers (such as `Authorization: Bearer …`) and a JSON body of your own, with fill-ins like `{title}`, `{author}`, `{cover}`, `{username}` and `{event}`. AudioBookRequest's `{bookTitle}`, `{bookAuthors}` and `{bookCover}` work too.
- Tokens, header values and webhook addresses are kept hidden in Settings and stored where only Narratarr can read them. A notification that can't be sent never holds up a download: it's tried once more, then noted on its destination.
- **Events in the log.** Every request, download start, finished book and failure is now written to Narratarr's log (`docker logs`), so scripts can follow along even without notifications.
- **Torznab, for Jackett users.** Settings → Search & sources has a **Torznab** section for Jackett, Prowlarr's Torznab endpoint, or any indexer that speaks Torznab. It's searched alongside Prowlarr, or on its own: it gets a tab on Discover, Wanted books are looked for on it (in its ebook categories for ebooks), and the Audible and Open Library tabs appear with it alone. Its API key is kept out of every saved link, whatever name the indexer gives it. Contributed by curator8888.
- If an indexer can't be reached while you search Discover, Narratarr now says so instead of showing no results.

## 0.7.4 — 2026-10-31

- **Authors, not narrators.** Books you import now take their author from the **Artist** tag first, then **Album artist**, the same way Audiobookshelf reads them. Many audiobooks put the narrator in Album artist, which made Narratarr show the narrator as the author (and miss books by authors you follow). A name that's only the narrator never counts as the author, "Author, read by Narrator" is split in two, and Audible's narrator tag in M4B files is read too.
- **Edit details.** Open any book in your library and choose **Edit details** to correct its title, author or narrators. This fixes books imported before this update, and anything else that's off. Your files and folders stay as they are. Only admins can edit.
- **A download folder for each torrent client.** qBittorrent, Transmission and Deluge each have an optional **Download folder**, so Narratarr's books can be kept apart from your other downloads instead of landing in the client's default folder.
- **Transmission labels.** Torrents Narratarr sends to Transmission get a label (`audiobooks` by default, Transmission 3.0 or later), so you can filter them, like qBittorrent's category.

## 0.7.3 — 2026-10-31

- **AllDebrid.** Torrents can now go through AllDebrid as well as Real-Debrid. In **Settings → Downloads**, pick the AllDebrid tile and choose **Connect AllDebrid**: Narratarr shows a short code, you enter it on alldebrid.com (on any device where you're signed in), and you're connected. There's no API key to copy, and your password never reaches Narratarr. Pasting a key still works too.
- If AllDebrid wants you to approve a new location, or won't serve your server's address (it refuses some data-centre and VPN addresses), Narratarr tells you so in plain words instead of failing quietly.
- The torrent tiles are now grouped: **In the cloud** (Real-Debrid, AllDebrid) and **On this machine** (qBittorrent, Transmission, Deluge).

## 0.7.2 — 2026-10-30

- **Settings, in pages.** Settings is no longer one long scroll. It's six short pages (General, Search & sources, Downloads, Audiobookshelf, Users and Updates), with a menu that shows each one's state at a glance: which download clients are ready, whether Prowlarr and Audiobookshelf are connected, how many people can sign in, and whether an update is out. Each page has its own address, so the browser's Back button works.
- **A save bar that shows up only when it's needed.** Change something and a bar appears with how many changes are unsaved, **Discard** and **Save changes**. Moving between Settings pages keeps what you've typed.
- Torrent clients are now tiles that say which ones are ready, the download-folder mapping reads left to right, and Prowlarr's categories fold away until you need them.
- **Audiobookshelf** shows when it's connected, and **Check now** counts how many of your books it can see, right on the page.
- On a phone, Settings opens as a list. Each area has a page of its own with a way back, and the save bar sits above the tab bar.

## 0.7.1 — 2026-10-30

- **Ebooks on the Prowlarr tab.** Discover's Prowlarr tab has an **Audiobooks / Ebooks** switch, so you can browse and search ebook releases from all your indexers directly, not only through Wanted. Ebooks you download from there are filed as ebooks.
- **Which indexers have ebooks.** **Test** under Settings → Prowlarr now lists the indexers that offer ebooks, or says none do. When an ebook search, or a Wanted ebook, finds nothing because no indexer has an ebook category, Narratarr says so instead of "Nothing found yet".

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
