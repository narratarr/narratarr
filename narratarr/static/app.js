// Narratarr web UI — no build step, just modules.

import { $, $$, esc, I, api, hue, coverHTML, firstAuthor, fmtBytes, fmtLeft, ago, toast } from "./util.js";
import { player } from "./player.js";
import { openAbsGuide } from "./guide.js";
import { reader } from "./reader.js";


// Narratarr's community: help, ideas and release news.
const DISCORD_URL = "https://discord.gg/kB5EgmfBrA";
const ACTIVE = ["queued", "resolving", "caching", "downloading", "organizing"];
const STATUS = {
  requested: "Awaiting approval",
  wanted: "Wanted",
  queued: "Queued",
  resolving: "Resolving",
  get caching() { return `On ${cloudName()}`; },
  downloading: "Downloading",
  organizing: "Organizing",
  completed: "In library",
  failed: "Failed",
};
const PHASES = [
  ["resolving", "Resolve"],
  ["caching", "Real-Debrid"],
  ["downloading", "Download"],
  ["organizing", "Organize"],
];
const CLIENTS = { sabnzbd: "SABnzbd", nzbget: "NZBGet", realdebrid: "Real-Debrid", alldebrid: "AllDebrid", torbox: "TorBox", premiumize: "Premiumize", qbittorrent: "qBittorrent", transmission: "Transmission", deluge: "Deluge" };
// Cloud services fetch torrents for us; then we download the files from them.
const DEBRID = ["realdebrid", "alldebrid", "torbox", "premiumize"];
const SHORT = { realdebrid: "rd", alldebrid: "ad", torbox: "tb", premiumize: "pm" }; // their settings' prefixes
const cloudName = () => (DEBRID.includes(state.settings?.torrent_client) ? CLIENTS[state.settings.torrent_client] : "Real-Debrid");
// Catalogs are where book details come from; the book itself is then found on your indexers.
const CATALOGS = ["audible", "openlibrary"];
const isCatalog = (name) => CATALOGS.includes(name);
const FORMATS = {
  audiobook: { label: "Audiobook", icon: "headphones", wanted: "" },
  ebook: { label: "Ebook", icon: "book", wanted: " as an ebook" },
  both: { label: "Both", icon: "", wanted: " as an audiobook and an ebook" },
};


// ---------------------------------------------------------------------------
// state + api

const state = {
  settings: null,
  sources: [],
  library: [],
  libraryKey: "",
  discover: { q: "", category: "", page: 0, total: 1, items: [], loading: false, error: "", token: 0, dym: "", lang: "", fmt: "", sort: "", note: "", source: lastSource(), kind: lastKind() },
  libFilter: "all",
  libFormat: "",
  libSearch: "",
  format: savedFormat(), // what catalog books are wanted as: audiobook, ebook or both ("" until someone picks)
  drawer: null, // { url, bookId }
  view: null,
  follows: [],
  updates: null,
};


const byUrl = () => new Map(state.library.map((b) => [b.source_url, b]));
// A catalog book in the library: its audiobook, or its ebook when that's all there is.
const libFor = (map, url) => map.get(url) || map.get(`${url}#ebook`);
const isEbook = (b) => b?.kind === "ebook";
// EPUBs open in Narratarr's reader, PDFs in the browser's own viewer; other formats are downloaded.
const canRead = (b) => isEbook(b) && b.status === "completed" && /\.(epub|pdf)$/i.test(b.path || "");
// Part-way through: listening (seconds) or reading (percent).
const underway = (b) => b.status === "completed" && !b.finished && b.listen_duration
  && (isEbook(b) ? b.listen_position > 0 : b.listen_position > 30);
const readLeft = (b) => `${Math.max(1, Math.round(100 - (b.listen_position / b.listen_duration) * 100))}% left`;

function savedFormat() {
  try { return localStorage.getItem("narratarr.format") || ""; } catch { return ""; }
}
// Open Library is mostly for ebooks, so its books start out as ebooks until someone picks otherwise.
const formatFor = (source) => state.format || (source === "openlibrary" ? "ebook" : "audiobook");

// ---------------------------------------------------------------------------
// helpers


function statusTag(status) {
  return `<span class="status-tag s-${status}"><i></i>${esc(STATUS[status] || status)}</span>`;
}

const isAdmin = () => state.user?.role === "admin";
const mine = (b) => !!b && b.requested_by === state.user?.id;
// A listener's request waits for an admin while that's turned on (Settings → Users).
const asksFirst = () => !isAdmin() && state.settings?.listener_approval !== false;

// A book's state on its cover: In library, Downloading 42%, Awaiting approval, Wanted, Failed.
// On the Library's own shelves a downloaded book says Finished or New instead (or nothing).
function stBadge(b, onShelf = false) {
  if (!b) return "";
  const s = b.status;
  let cls = "dl", text = STATUS[s] || s, icon = "";
  if (s === "completed") {
    if (!onShelf) [cls, text, icon] = ["ok", "In library", I.check];
    else if (b.finished) [cls, text, icon] = ["ok", "Finished", I.check];
    else if (!b.listen_position && Date.now() / 1000 - (b.completed_at || 0) < 3 * 86400) [cls, text] = ["new", "New"];
    else return "";
  } else if (s === "requested") [cls, text, icon] = ["req", "Awaiting approval", I.bell];
  else if (s === "wanted") [cls, text, icon] = ["want", "Wanted", I.bookmark];
  else if (s === "failed") [cls, text] = ["bad", "Failed"];
  else if (s === "queued") text = "Queued";
  else if (s === "caching") text = `On ${cloudName()} ${Math.round(b.progress || 0)}%`;
  else if (s === "downloading") text = `Downloading ${Math.round(b.progress || 0)}%`;
  return `<span class="st-badge ${cls}">${icon}${esc(text)}</span>`;
}

function spinBtn(btn, on) {
  if (on) {
    btn.dataset.html = btn.innerHTML;
    btn.innerHTML = '<span class="spinner"></span>';
    btn.disabled = true;
  } else if (btn.dataset.html) {
    btn.innerHTML = btn.dataset.html;
    btn.disabled = false;
  }
}


function modal({ title, body, confirm = "Confirm", danger = false, extra = "" }) {
  return new Promise((resolve) => {
    const scrim = $("#modal");
    scrim.hidden = false;
    scrim.innerHTML = `
      <div class="modal" role="alertdialog" aria-modal="true" aria-labelledby="modal-title">
        <h3 id="modal-title">${esc(title)}</h3>
        <p>${esc(body)}</p>
        ${extra}
        <div class="actions">
          <button class="btn ghost" data-act="cancel">Cancel</button>
          <button class="btn ${danger ? "danger solid" : "primary"}" data-act="ok">${esc(confirm)}</button>
        </div>
      </div>`;
    const done = (ok) => {
      const extras = Object.fromEntries($$("input", scrim).map((i) => [i.name, i.checked]));
      scrim.hidden = true;
      scrim.innerHTML = "";
      document.removeEventListener("keydown", onKey, true);
      resolve(ok ? { ok, ...extras } : null);
    };
    const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); done(false); } };
    document.addEventListener("keydown", onKey, true);
    scrim.onclick = (e) => {
      if (e.target === scrim) return done(false);
      const act = e.target.closest("[data-act]")?.dataset.act;
      if (act) done(act === "ok");
    };
    $('[data-act="ok"]', scrim).focus();
  });
}

// What the Prowlarr tab searches for: "audiobook" or "ebook".
function lastKind() {
  try { return localStorage.getItem("narratarr.discoverKind") === "ebook" ? "ebook" : "audiobook"; } catch { return "audiobook"; }
}

function lastSource() {
  try { return localStorage.getItem("narratarr.source") || localStorage.getItem("audiobookeer.source") || "librivox"; } catch { return "librivox"; }
}
const FALLBACK_SOURCES = {
  local: { label: "Your files", delivery: "local" },
  audible: { label: "Audible catalog", delivery: "wanted" },
  openlibrary: { label: "Open Library", delivery: "wanted" },
};
const sourceInfo = (name) => state.sources.find((s) => s.name === name)
  || { name, label: name, delivery: "torrent", categories: [], ...FALLBACK_SOURCES[name] };
const isDirect = (name) => sourceInfo(name).delivery === "direct";

// Which download client a book needs that isn't set up yet ("" when it can download now).
function missingClient(source, protocol) {
  if (isDirect(source) || isCatalog(source) || source === "local") return "";
  const st = state.settings || {};
  if (protocol === "usenet") return st.usenet_configured ? "" : CLIENTS[st.usenet_client] || "SABnzbd";
  return st.torrent_configured ? "" : CLIENTS[st.torrent_client] || "Real-Debrid";
}

// Wanted Audible books, imports and client-handled jobs each show their own steps.
function phasesFor(b) {
  const st = state.settings || {};
  if (b.source === "local") return [["downloading", "Copy"], ["organizing", "Organize"]];
  if (isDirect(b.source)) return PHASES.filter(([s]) => s !== "caching");
  const client = b.protocol === "usenet" ? b.download_client || st.usenet_client
    : b.download_client || (DEBRID.includes(st.torrent_client) ? "" : st.torrent_client);
  if (client && !DEBRID.includes(client)) return [["resolving", "Resolve"], ["downloading", CLIENTS[client]], ["organizing", "Organize"]];
  return PHASES.map(([s, label]) => [s, s === "caching" ? cloudName() : label]);
}

// ---------------------------------------------------------------------------
// library actions (shared by every view)

async function addBook(url, { cover = "", title = "", source = "audiobookbay", protocol = "", format } = {}) {
  if (isCatalog(source)) {
    format ||= formatFor(source);
    // A catalog only describes the book: it goes to Wanted and Narratarr looks for a release.
    const book = await api("/api/library", { method: "POST", body: { url, download: true, source, format } });
    await refreshLibrary();
    toast(book.status === "requested" ? `Asked for ${book.title}. It’ll be looked for once an admin approves it.`
      : book.status === "wanted" ? `${book.title} is on your Wanted list${FORMATS[format].wanted}. Looking for it now…` : `${book.title} is already in your library`,
      { kind: "ok", cover: cover || book.cover_url });
    return book;
  }
  // Free sources download directly; torrents and Usenet releases need a download client.
  const missing = missingClient(source, protocol);
  const download = !missing;
  const book = await api("/api/library", { method: "POST", body: { url, download, source, ...(format === "ebook" ? { format } : {}) } });
  await refreshLibrary();
  toast(
    book.status === "requested" ? `Asked for ${book.title || title}. It’ll download once an admin approves it.`
    : download ? `${book.title || title} is on its way` : `Saved ${book.title || title} to Wanted. Connect ${missing} in Settings to download.`,
    { kind: "ok", cover: cover || book.cover_url }
  );
  return book;
}

async function bookAction(id, action) {
  if (action === "remove") {
    const book = state.library.find((b) => b.id === id);
    const takeBack = !isAdmin();
    const decline = isAdmin() && book?.status === "requested";
    const res = await modal({
      title: takeBack ? "Take back your request?" : decline ? "Decline this request?" : "Remove from library?",
      body: takeBack ? `“${book?.title}” won’t be looked for or downloaded.`
        : decline ? `“${book?.title}” won’t be downloaded${book.requested_by_name ? ` for ${esc(book.requested_by_name)}` : ""}.`
        : `“${book?.title}” will be removed from Narratarr.`,
      confirm: takeBack ? "Take it back" : decline ? "Decline" : "Remove",
      danger: true,
      extra: book?.path && !takeBack
        ? `<label class="switch"><input type="checkbox" name="files"><i></i>Also delete its files on disk${book.source === "local" ? " (the ones you imported)" : ""}</label>`
        : "",
    });
    if (!res) return false;
    if (player.book?.id === id) player.close();
    const out = await api(`/api/library/${id}?delete_files=${!!res.files}`, { method: "DELETE" });
    toast(out?.kept ? `Removed. Its files were kept at ${out.kept}` : takeBack ? "Request taken back" : decline ? "Request declined" : "Removed from library",
      out?.kept ? { kind: "ok" } : {});
  } else if (action === "approve") {
    const book = await api(`/api/library/${id}/approve`, { method: "POST" });
    toast(book.status === "queued" ? `Approved. ${book.title} is downloading` : `Approved. Looking for ${book.title} now`, { kind: "ok", cover: book.cover_url });
  } else {
    const book = state.library.find((b) => b.id === id);
    const missing = action === "download" && book && missingClient(book.source, book.protocol);
    if (missing) {
      toast(`Connect ${missing} in Settings first.`, { kind: "err" });
      return false;
    }
    const out = await api(`/api/library/${id}/${action}`, { method: "POST" });
    if (out?.status === "requested") toast("Asked an admin to approve it", { kind: "ok" });
  }
  await refreshLibrary();
  return true;
}

// ---------------------------------------------------------------------------
// polling

function updateBadges() {
  const active = state.library.filter((b) => ACTIVE.includes(b.status)).length;
  const badge = $("#activity-badge");
  badge.hidden = !active;
  badge.textContent = active;
  const waiting = isAdmin() ? state.library.filter((b) => b.status === "requested").length : 0;
  const req = $("#requests-badge");
  req.hidden = !waiting;
  req.textContent = waiting;
}

async function refreshLibrary() {
  try {
    state.library = await api("/api/library");
  } catch {
    return;
  }
  updateBadges();

  const key = state.library.map((b) => `${b.id}:${b.status}`).join("|");
  const structural = key !== state.libraryKey;
  state.libraryKey = key;
  state.view?.update?.(structural);
  updateDrawerLive();
}

function updateRdPill() {
  const pill = $("#rd-pill");
  const st = state.settings || {};
  const ready = [st.torrent_configured && CLIENTS[st.torrent_client], st.usenet_configured && CLIENTS[st.usenet_client]].filter(Boolean);
  pill.className = `rd-pill ${ready.length ? "ok" : "missing"}`;
  $("span", pill).textContent = ready.length ? `${ready.join(" · ")} ready` : "Connect a downloader";
}

async function refreshFollows() {
  try { state.follows = await api("/api/follows"); } catch { /* the page still works without them */ }
}

// ---------------------------------------------------------------------------
// router

const views = { discover: Discover, library: Library, authors: Authors, activity: Activity, settings: Settings };

function route() {
  let [name = "discover", param] = location.hash.replace(/^#\/?/, "").split("?")[0].split("/");
  if (name === "settings" && state.user?.role !== "admin") name = "discover";
  if (name === "settings" && state.viewName === "settings" && state.view?.go) {
    state.view.go(param); // another Settings page: same form, so nothing typed is lost
    window.scrollTo({ top: 0 });
    return;
  }
  const View = views[name] || Discover;
  $$("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === name));
  state.view?.destroy?.();
  const root = $("#view");
  root.classList.remove("enter");
  void root.offsetWidth;
  root.classList.add("enter");
  state.view = View(root, param);
  state.viewName = name;
  window.scrollTo({ top: 0 });
}

// ---------------------------------------------------------------------------
// Discover

function cardHTML(item, lib, i = 0) {
  const h = hue(item.title + item.author);
  const facts = [
    item.format, item.size, item.language && item.language !== "English" ? item.language : "",
    item.protocol === "usenet" ? "Usenet" : item.seeders != null ? `${item.seeders} seeder${item.seeders === 1 ? "" : "s"}` : "",
    item.source === "audible" ? shortLength(item.length) : item.source === "openlibrary" ? item.posted : "",
    item.source === "audible" ? releaseLabel(item.released || item.posted) : "",
  ].filter(Boolean);
  const active = lib && ACTIVE.includes(lib.status);
  return `
    <article class="card" tabindex="0" data-url="${esc(item.url)}" ${lib ? `data-id="${lib.id}"` : ""} style="--glow:hsl(${h} 80% 55%);animation-delay:${Math.min(i, 12) * 35}ms">
      <div class="cover-wrap">
        ${coverHTML(item)}
        ${stBadge(lib)}
        ${active ? `<div class="bar"><i style="width:${lib.progress || 0}%"></i></div>` : ""}
        ${quickButton(item, lib)}
      </div>
      <div class="meta">
        <div class="title">${esc(item.title)}</div>
        <div class="author">${esc(item.author || (item.indexer ? `via ${item.indexer}` : "Unknown author"))}</div>
        ${facts.length ? `<div class="facts">${facts.map((f) => `<span>${esc(f)}</span>`).join("")}</div>` : ""}
        ${bmRatingHTML(item)}
      </div>
    </article>`;
}

function bmRatingHTML(item) {
  // `rating` is whatever provider Settings selected. `label` is the small mark beside the score:
  // "Audible" where there is no count to show, or a consensus line from Bookmarks.
  const r = item.rating || item.bookmarks_rating;
  let inner = "";
  if (r && (r.score_text || r.display)) {
    const score = r.score_text || `★ ${Number(r.R).toFixed(1)}`;
    const second = r.label || r.consensus || (r.count ? `${r.count}` : "");
    const tip = r.tooltip || r.display || "";
    inner = `<a class="bm-rating" href="${esc(r.url)}" target="_blank" rel="noopener" title="${esc(tip)}">
        <span class="bm-score">${esc(score)}</span>
        ${second ? `<span class="bm-cons">${esc(second)}</span>` : ""}
      </a>`;
  } else {
    // The same words on every card, whatever the source. David asked for this exact string, and a
    // per-source gate left LibriVox cards silently blank instead.
    inner = `<span class="bm-rating empty">No Rating</span>`;
  }
  // Always emit the slot so every card reserves the same space and the row
  // sits at the same height whether or not a rating exists.
  return `<div class="bm-slot">${inner}</div>`;
}

const shortLength = (len) => (len || "").replace(/ hours?/, "h").replace(/ and /, " ").replace(/ minutes?/, "m");

function releaseLabel(day, always = false) {
  const t = Date.parse(day || "");
  if (!t) return "";
  const when = new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  return t > Date.now() ? `Out ${when}` : always ? when : "";
}

function quickButton(item, lib) {
  if (!lib && isCatalog(item.source)) {
    const as = FORMATS[formatFor(item.source)].wanted;
    return `<button class="quick" data-quick aria-label="Add ${esc(item.title)} to Wanted${as}" title="Add to Wanted${as}">${I.bookmark}</button>`;
  }
  if (canRead(lib)) return `<button class="quick play-quick" data-play="${lib.id}" aria-label="Read ${esc(item.title)}" title="Read">${I.book}</button>`;
  if (isEbook(lib) && lib.status === "completed") return `<a class="quick" href="/api/library/${lib.id}/ebook" download aria-label="Download the ebook of ${esc(item.title)}" title="Download ebook">${I.down}</a>`;
  if (!lib) return `<button class="quick" data-quick aria-label="Download ${esc(item.title)}" title="Download">${I.plus}</button>`;
  if (lib.status === "completed") return `<button class="quick play-quick" data-play="${lib.id}" aria-label="Play ${esc(item.title)}" title="Play">${I.play}</button>`;
  return "";
}

// The big card at the top of Discover.
function spotlightHTML(item, lib, src) {
  const narr = (item.narrators || []).slice(0, 2).join(", ");
  const facts = [
    isCatalog(item.source) ? (formatFor(item.source) === "ebook" ? "Ebook" : "Audiobook") : item.format,
    item.source === "audible" ? shortLength(item.length) : item.size,
    item.language && item.language !== "English" ? item.language : "",
    item.protocol === "usenet" ? "Usenet" : item.seeders != null ? `${item.seeders} seeder${item.seeders === 1 ? "" : "s"}` : "",
    releaseLabel(item.released || item.posted),
  ].filter(Boolean);
  const author = firstAuthor(item.author);
  const also = author ? state.library.filter((b) => firstAuthor(b.author) === author && b.source_url !== item.url).slice(0, 3) : [];
  let primary;
  if (lib?.status === "completed") {
    primary = `<button class="btn primary" data-play="${lib.id}">${isEbook(lib) ? `${I.book} Read` : `${I.play} Play`}</button>`;
  } else if (lib) {
    primary = stBadge(lib).replace("st-badge", "st-badge inline");
  } else {
    const asks = asksFirst();
    const label = asks ? "Ask for it" : isCatalog(item.source) ? "Add to Wanted"
      : missingClient(item.source, item.protocol) ? "Save to Wanted" : "Download";
    primary = `<button class="btn primary" data-quick>${asks ? I.bell : isCatalog(item.source) ? I.bookmark : I.down} ${label}</button>`;
  }
  return `
    <section class="spotlight" data-url="${esc(item.url)}" aria-label="Spotlight: ${esc(item.title)}" style="--glow:hsl(${hue(item.title + item.author)} 80% 55%)">
      <div class="spot-cover">${coverHTML(item)}</div>
      <div class="spot-copy">
        <div class="eyebrow">Spotlight · ${esc(src.label)}</div>
        <h2>${esc(item.title)}</h2>
        <p class="by">${esc(item.author || (item.indexer ? `via ${item.indexer}` : "Unknown author"))}${narr ? ` · read by ${esc(narr)}` : ""}</p>
        ${facts.length ? `<div class="spot-facts">${facts.map((f) => `<span>${esc(f)}</span>`).join("")}</div>` : ""}
        ${item.description ? `<p class="spot-desc">${esc(item.description)}</p>` : ""}
        <div class="spot-actions">
          ${primary}
          <button class="btn" type="button" data-spot-details>Details</button>
          ${author ? `<button class="btn ghost" type="button" data-more-by="${esc(author)}">More by ${esc(author)}</button>` : ""}
        </div>
        ${!lib && asksFirst() ? `<p class="spot-note">An admin approves requests before anything downloads.</p>` : ""}
        ${also.length ? `<div class="spot-also"><span>Also by ${esc(author)}</span>${also.map((b) => `
          <button type="button" class="also" data-open-book="${b.id}">${coverHTML(b)}<span>${esc(b.title)}</span>${stBadge(b).replace("st-badge", "st-badge inline")}</button>`).join("")}</div>` : ""}
      </div>
    </section>`;
}

// New books from the authors and series you follow (following puts them in the library as Wanted).
function followShelfHTML() {
  const norm = (n) => String(n || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  const authors = new Set(state.follows.filter((f) => f.kind === "author").map((f) => norm(f.name)));
  const series = new Set(state.follows.filter((f) => f.kind === "series").map((f) => f.asin));
  if (!authors.size && !series.size) return "";
  const when = (b) => Date.parse(b.released || "") || (b.added_at || 0) * 1000;
  const books = state.library
    .filter((b) => !isEbook(b) && (authors.has(norm(firstAuthor(b.author))) || (b.series || []).some((x) => series.has(x.asin))))
    .sort((a, b) => when(b) - when(a))
    .slice(0, 12);
  if (!books.length) return "";
  return `
    <section class="follow-shelf" aria-labelledby="fs-title">
      <div class="section-head"><h2 id="fs-title">New from authors you follow</h2><a class="see-all" href="#/authors">See all</a></div>
      <div class="hshelf">${books.map((b, i) => cardHTML({ ...b, url: b.source_url }, b, i)).join("")}</div>
    </section>`;
}

const skeletons = (n) => Array.from({ length: n }, () => '<div class="card skeleton"><div class="cover-wrap"></div><div class="line"></div><div class="line short"></div></div>').join("");

function Discover(root) {
  const d = state.discover;
  if (!state.sources.some((s) => s.name === d.source)) d.source = "librivox";
  const src = sourceInfo(d.source);
  const st = state.settings;
  const needsClient = st && (src.delivery === "torrent" ? !st.torrent_configured
    : src.delivery === "indexer" ? !st.torrent_configured && !st.usenet_configured : false);
  const ebooksHere = src.name === "prowlarr" ? d.kind === "ebook" : isCatalog(d.source) && formatFor(d.source) === "ebook";
  const defaultLabel = src.name === "prowlarr" && d.kind === "ebook" ? "Latest ebooks from your indexers" : src.default_label || "Fresh arrivals";
  const greeting = (() => {
    const h = new Date().getHours();
    return h < 5 ? "Late night listening" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
  })();

  // Audiobooks or ebooks: Prowlarr searches different categories; a catalog book is wanted in that format.
  const fmtNow = src.name === "prowlarr" ? d.kind : isCatalog(d.source) ? (formatFor(d.source) === "ebook" ? "ebook" : "audiobook") : "";
  const groups = [
    ["Free · download now", (s) => s.delivery === "direct"],
    ["Your indexers", (s) => s.delivery === "torrent" || s.delivery === "indexer"],
    ["Catalogs · add to Wanted", (s) => s.delivery === "wanted"],
  ];
  const ready = (s) => s.delivery === "direct" || (s.delivery === "torrent" ? st?.torrent_configured
    : s.delivery === "indexer" ? st?.torrent_configured || st?.usenet_configured : st?.indexer_configured);
  const sourceSub = (s) => s.name === "openlibrary" ? "Books · for ebooks"
    : { direct: "Free · direct download", indexer: "Your indexers", wanted: "Add to Wanted" }[s.delivery] || "Torrents";
  const groupHTML = (role) => groups.map(([label, test]) => {
    const list = state.sources.filter(test);
    return list.length ? `<div class="src-group"><span class="src-label">${label}</span><div class="src-pills">${list.map((s) => `
      <button type="button" role="${role}" aria-${role === "radio" ? "checked" : "selected"}="${s.name === d.source}" class="src-pill ${s.name === d.source ? "on" : ""}" data-src="${esc(s.name)}" title="${esc(sourceSub(s))}">
        <i class="dot ${ready(s) ? "ok" : ""}" aria-hidden="true"></i>${esc(s.label)}</button>`).join("")}</div></div>` : "";
  }).join("");

  root.innerHTML = `
    <header class="disc-head">
      <div>
        <div class="eyebrow">${greeting}</div>
        <h1 class="page-title">What are we <em>${ebooksHere ? "reading" : "listening"}</em> to next?</h1>
      </div>
      ${fmtNow ? `<div class="segmented kinds" role="radiogroup" aria-label="Look for">
        ${[["audiobook", I.headphones, "Audiobooks"], ["ebook", I.book, "Ebooks"]].map(([k, icon, label]) => `
          <button type="button" role="radio" aria-checked="${fmtNow === k}" aria-label="${label}" class="${fmtNow === k ? "on" : ""}" data-kind="${k}">${icon}<span>${label}</span></button>`).join("")}
      </div>` : ""}
    </header>
    <div class="search-wrap">
      <form class="search" id="search-form" role="search">
        ${I.search}
        <input id="q" type="search" placeholder="${ebooksHere ? "Search titles and authors… typos are fine" : "Search titles, authors, narrators… typos are fine"}" autocomplete="off" value="${esc(d.q)}" aria-label="Search ${esc(src.label)}" />
        <div class="scope">
          <button type="button" class="scope-btn" id="scope-btn" aria-haspopup="true" aria-expanded="false" aria-label="Searching ${esc(src.label)}. Change where to search">in <b>${esc(src.label)}</b>${I.chevron}</button>
          <div class="scope-menu" id="scope-menu" role="listbox" aria-label="Where to search" hidden>${groupHTML("option")}</div>
        </div>
        <kbd>/</kbd>
        <button class="btn primary" type="submit" aria-label="Search">${I.search}<span>Search</span></button>
      </form>
      <div class="suggest" id="suggest" role="listbox" hidden></div>
    </div>
    <div class="src-groups" id="sources" role="radiogroup" aria-label="Where to look">${groupHTML("radio")}</div>
    ${needsClient ? `
      <div class="banner">
        <div class="ico">${I.key}</div>
        <div><strong>Connect a download client to start downloading</strong><p>${src.delivery === "indexer"
          ? "Usenet results go to SABnzbd or NZBGet, torrents to Real-Debrid, AllDebrid, TorBox, Premiumize, qBittorrent, Transmission or Deluge. You can save books to Wanted until then."
          : "Use Real-Debrid, AllDebrid, TorBox, Premiumize, qBittorrent, Transmission or Deluge. You can browse and save books to Wanted until then."}</p></div>
        <a class="btn" href="#/settings">Open Settings</a>
      </div>` : ""}
    <div id="spotlight"></div>
    <div id="follow-shelf"></div>
    <div class="section-head"><h2 id="disc-title"></h2><div class="refine" id="refine"></div></div>
    <div class="chips" id="chips">
      <button class="chip ${!d.q && !d.category ? "on" : ""}" data-cat="">${esc(defaultLabel)}</button>
      ${src.categories.map((c) => `<button class="chip ${!d.q && d.category === c.slug ? "on" : ""}" data-cat="${esc(c.slug)}">${esc(c.name)}</button>`).join("")}
    </div>
    <div id="dym"></div>
    <div id="lang-note"></div>
    <div class="grid" id="grid"></div>
    <div class="load-more" id="sentinel"></div>
    <div class="muted" style="margin-top:18px;font-size:12px;text-align:center">Ratings from Bookmarks, courtesy Lit Hub</div>`;

  const grid = $("#grid", root);
  const sentinel = $("#sentinel", root);

  function heading() {
    const cat = src.categories.find((c) => c.slug === d.category);
    $("#disc-title", root).innerHTML = d.q
      ? `Results for <em style="font-style:italic">“${esc(d.q)}”</em> <span class="in-src">in ${esc(src.label)}${src.name === "prowlarr" && d.kind === "ebook" ? " · ebooks" : ""}</span>`
      : esc(cat ? cat.name : defaultLabel);
    $("#dym", root).innerHTML = d.dym
      ? `<p class="dym">Including results for <em>${esc(d.dym)}</em> · <button data-literal>Show only close matches for “${esc(d.q)}”</button></p>`
      : "";
    refine();
  }

  // Filter + sort what's loaded, client side.
  function refine() {
    const opts = (vals, cur, all) => `<option value="">${all}</option>` + [...new Set(vals.filter(Boolean))].sort()
      .map((v) => `<option ${v === cur ? "selected" : ""}>${esc(v)}</option>`).join("");
    // The unsorted order is whatever the source gave us: relevance, popularity or recency.
    const listedNewest = !d.q && (["torrent", "indexer"].includes(src.delivery) || d.category === "new");
    const natural = d.q ? "Best match" : listedNewest ? "Newest first" : "Most popular";
    const sorts = [["", natural], ["rating", "Rating"], ["newest", "Newest first"], ["largest", "Largest"], ["smallest", "Smallest"], ["title", "Title A–Z"]]
      .filter(([v]) => !(v === "newest" && listedNewest));
    const shown = visibleItems().length;
    $("#refine", root).innerHTML = !d.items.length ? "" : `
      <span class="muted">${shown}${shown !== d.items.length ? ` of ${d.items.length}` : ""} books</span>
      <select class="select" data-r="lang" aria-label="Language">${opts(d.items.map((i) => i.language), d.lang, "All languages")}</select>
      <select class="select" data-r="fmt" aria-label="Format">${opts(d.items.map((i) => i.format.toUpperCase()), d.fmt, "All formats")}</select>
      <select class="select" data-r="sort" aria-label="Sort">${sorts.map(([v, l]) => `<option value="${v}" ${v === d.sort ? "selected" : ""}>${l}</option>`).join("")}</select>`;
  }

  const sizeOf = (s) => {
    const m = /([\d.]+)\s*([KMGT]?B)/i.exec(s || "");
    return m ? parseFloat(m[1]) * 1024 ** ["B", "KB", "MB", "GB", "TB"].indexOf(m[2].toUpperCase()) : 0;
  };

  function visibleItems() {
    let items = d.items.filter((i) => (!d.lang || i.language === d.lang) && (!d.fmt || i.format.toUpperCase() === d.fmt))
      .filter((i) => d.allLangs || i.lang_ok !== false);
    const by = {
      rating: (a, b) => { const s = (x) => x.rating?.bayes ?? x.bookmarks_rating?.R ?? null; const av = s(a), bv = s(b); if (av == null && bv == null) return 0; if (av == null) return 1; if (bv == null) return -1; return bv - av; },
      newest: (a, b) => (Date.parse(b.posted) || 0) - (Date.parse(a.posted) || 0),
      largest: (a, b) => sizeOf(b.size) - sizeOf(a.size),
      smallest: (a, b) => sizeOf(a.size) - sizeOf(b.size),
      title: (a, b) => a.title.localeCompare(b.title),
    }[d.sort];
    return by ? [...items].sort(by) : items;
  }

  // Browsing (not searching or filtering): the first good-looking book gets the spotlight.
  function spotlightItem(items) {
    if (d.q || d.lang || d.fmt || d.sort || d.error || items.length < 6) return null;
    return items.slice(0, 4).find((i) => /^https:\/\//.test(i.cover_url || "")) || items[0];
  }

  function renderItems() {
    const lib = byUrl();
    let items = visibleItems();
    const spot = spotlightItem(items);
    $("#spotlight", root).innerHTML = spot ? spotlightHTML(spot, libFor(lib, spot.url), src) : "";
    $("#follow-shelf", root).innerHTML = d.q ? "" : followShelfHTML();
    if (spot) items = items.filter((i) => i !== spot);
    const others = d.allLangs ? 0 : d.items.filter((i) => i.lang_ok === false).length;
    $("#lang-note", root).innerHTML = others ? `<p class="dym">${others} book${others === 1 ? "" : "s"} in other languages hidden (you chose ${esc(state.settings?.languages || "")}) · <button data-all-langs>Show them</button></p>`
      : d.allLangs && d.items.some((i) => i.lang_ok === false) ? `<p class="dym">Showing every language · <button data-all-langs>Only mine</button></p>` : "";
    grid.innerHTML = items.map((it, i) => cardHTML(it, libFor(lib, it.url), i)).join("");
    if (!items.length && !d.loading) {
      const filtered = d.items.length > 0;
      grid.innerHTML = `<div class="empty" style="grid-column:1/-1">
        <div class="art">${d.error ? I.alert : I.search}</div>
        <h3>${d.error ? "Couldn’t load books" : filtered ? "Nothing matches those filters" : "Nothing found"}</h3>
        <p>${esc(d.error || (filtered ? "Try another language or format." : d.note || "Try searching by author, or just part of the title."))}</p>
        ${d.error ? '<button class="btn" data-retry>Try again</button>' : ""}
      </div>`;
    }
  }

  function footer() {
    if (d.loading) sentinel.innerHTML = '<span class="spinner" style="color:var(--text-3)"></span>';
    else if (d.page && d.page < d.total) sentinel.innerHTML = '<button class="btn" data-more>Load more</button>';
    else sentinel.innerHTML = "";
  }

  async function load(reset) {
    if (d.loading && !reset) return;
    const token = ++d.token;
    if (reset) { d.items = []; d.page = 0; d.total = 1; d.error = ""; d.dym = ""; d.note = ""; grid.innerHTML = skeletons(12); }
    d.loading = true;
    heading();
    footer();
    const slow = setTimeout(() => {
      if (d.loading && token === d.token) {
        sentinel.innerHTML = `<div style="display:grid;justify-items:center;gap:12px;color:var(--text-3)"><span class="spinner"></span><span>${esc(src.label)} is slow to respond. Still trying…</span></div>`;
      }
    }, 6000);
    try {
      const params = new URLSearchParams({ q: d.q, category: d.category, page: d.page + 1, source: d.source });
      if (src.name === "prowlarr") params.set("kind", d.kind);
      if (d.sort === "rating") params.set("sort", "rating");
      const res = await api(`/api/discover?${params}`);
      if (token !== d.token) return;
      const seen = new Set(d.items.map((i) => i.url));
      d.items.push(...res.items.filter((i) => !seen.has(i.url)));
      d.page = res.page;
      d.total = res.total_pages;
      if (reset) { d.dym = res.did_you_mean || ""; d.note = res.note || ""; }
    } catch (e) {
      if (token !== d.token) return;
      d.error = e.message;
      if (!reset) toast(e.message, { kind: "err" });
    }
    clearTimeout(slow);
    d.loading = false;
    if (!root.isConnected) return;
    renderItems();
    heading();
    footer();
  }

  const input = $("#q", root);
  const box = $("#suggest", root);
  let sugg = [], hi = -1, suggTimer = 0, suggToken = 0;

  function search(q) {
    d.q = q;
    d.lang = d.fmt = d.sort = "";
    if (q) d.category = "";
    $$(".chip", root).forEach((c) => c.classList.toggle("on", !q && c.dataset.cat === d.category));
    hideSuggest();
    load(true);
  }

  function hideSuggest() { clearTimeout(suggTimer); box.hidden = true; hi = -1; suggToken++; }

  function showSuggest() {
    if (!sugg.length || document.activeElement !== input) return hideSuggest();
    box.innerHTML = `<div class="label">Did you mean</div>` + sugg.map((sg, i) => `
      <button type="button" role="option" class="${i === hi ? "on" : ""}" data-s="${i}">
        ${/^https:\/\//.test(sg.cover_url) ? `<img src="${esc(sg.cover_url)}" alt="" referrerpolicy="no-referrer">` : '<span class="ph"></span>'}
        <span style="min-width:0"><b>${esc(sg.title)}</b><small>${esc(sg.author)}</small></span>
      </button>`).join("");
    box.hidden = false;
  }

  const pickSuggestion = (sg) => { input.value = sg.title.split(":")[0]; search(input.value.trim()); };

  input.addEventListener("input", () => {
    clearTimeout(suggTimer);
    const q = input.value.trim();
    if (q.length < 3 || !state.settings?.smart_search) return hideSuggest();
    suggTimer = setTimeout(async () => {
      const token = ++suggToken;
      try {
        const res = await api(`/api/suggest?q=${encodeURIComponent(q)}`);
        if (token !== suggToken) return;
        sugg = res; hi = -1; showSuggest();
      } catch { /* suggestions are a nicety */ }
    }, 300);
  });
  input.addEventListener("keydown", (e) => {
    if (box.hidden) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const n = sugg.length;
      hi = e.key === "ArrowDown" ? (hi + 1 >= n ? -1 : hi + 1) : (hi <= -1 ? n - 1 : hi - 1);
      showSuggest();
    } else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); hideSuggest(); }
    else if (e.key === "Enter" && hi >= 0) { e.preventDefault(); pickSuggestion(sugg[hi]); }
  });
  input.addEventListener("blur", () => setTimeout(hideSuggest, 150));
  box.onpointerdown = (e) => e.preventDefault(); // keep focus so the click lands
  box.onclick = (e) => { const b = e.target.closest("[data-s]"); if (b) pickSuggestion(sugg[Number(b.dataset.s)]); };

  $("#search-form", root).onsubmit = (e) => { e.preventDefault(); search(input.value.trim()); };
  input.addEventListener("search", () => { if (!input.value && d.q) search(""); });

  $("#refine", root).onchange = (e) => {
    const r = e.target.dataset.r;
    if (!r) return;
    d[r] = e.target.value;
    renderItems();
    refine();
  };

  const scopeBtn = $("#scope-btn", root), scopeMenu = $("#scope-menu", root);
  const closeScope = () => { scopeMenu.hidden = true; scopeBtn.setAttribute("aria-expanded", "false"); };
  scopeBtn.onclick = () => {
    scopeMenu.hidden = !scopeMenu.hidden;
    scopeBtn.setAttribute("aria-expanded", String(!scopeMenu.hidden));
    if (!scopeMenu.hidden) $(".src-pill.on", scopeMenu)?.focus();
  };
  scopeMenu.onkeydown = (e) => { if (e.key === "Escape") { e.stopPropagation(); closeScope(); scopeBtn.focus(); } };
  const outsideScope = (e) => { if (!e.target.closest(".scope")) closeScope(); };
  document.addEventListener("click", outsideScope);

  $("#sources", root).onclick = scopeMenu.onclick = (e) => {
    const b = e.target.closest("[data-src]");
    closeScope();
    if (!b || b.dataset.src === d.source) return;
    d.source = b.dataset.src;
    try { localStorage.setItem("narratarr.source", d.source); } catch { /* private mode */ }
    // Keep the search (if any) and run it against the new source.
    d.category = "";
    d.items = [];
    d.lang = d.fmt = d.sort = "";
    d.token++;
    d.loading = false;
    route();
  };

  const kindBox = $(".disc-head .kinds", root);
  if (kindBox) kindBox.onclick = (e) => {
    const kind = e.target.closest("[data-kind]");
    if (!kind || kind.dataset.kind === fmtNow) return;
    if (src.name === "prowlarr") {
      d.kind = kind.dataset.kind;
      try { localStorage.setItem("narratarr.discoverKind", d.kind); } catch { /* private mode */ }
      d.items = [];
      d.lang = d.fmt = d.sort = "";
      d.token++;
      d.loading = false;
    } else {
      state.format = kind.dataset.kind; // what catalog books are wanted as from now on
      try { localStorage.setItem("narratarr.format", state.format); } catch { /* private mode */ }
    }
    route(); // the same search, now for the other format
  };

  $("#chips", root).onclick = (e) => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    d.category = chip.dataset.cat;
    d.q = "";
    $("#q", root).value = "";
    $$(".chip", root).forEach((c) => c.classList.toggle("on", c === chip));
    chip.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
    load(true);
  };

  root.onclick = async (e) => {
    if (e.target.closest("[data-more]")) return load(false);
    if (e.target.closest("[data-all-langs]")) { d.allLangs = !d.allLangs; renderItems(); refine(); return; }
    if (e.target.closest("[data-retry]")) return load(true);
    if (e.target.closest("[data-literal]")) {
      // Re-run without typo correction by searching the exact phrase.
      d.dym = "";
      d.items = d.items.filter((i) => i.score >= 90);
      renderItems(); heading();
      return;
    }
    if (e.target.closest("a.quick")) return; // an ebook download: let the link do it
    const play = e.target.closest("[data-play]");
    if (play) return playBook(Number(play.dataset.play));
    const moreBy = e.target.closest("[data-more-by]");
    if (moreBy) { input.value = moreBy.dataset.moreBy; return search(moreBy.dataset.moreBy); }
    const openBook = e.target.closest("[data-open-book]");
    if (openBook) return openDrawer({ bookId: Number(openBook.dataset.openBook) });
    const shelfCard = e.target.closest("#follow-shelf .card[data-id]");
    if (shelfCard) return openDrawer({ bookId: Number(shelfCard.dataset.id) });
    const card = e.target.closest(".card[data-url], .spotlight[data-url]");
    if (!card) return;
    const item = d.items.find((i) => i.url === card.dataset.url);
    if (!item) return;
    if (e.target.closest("[data-quick]")) {
      const btn = e.target.closest("[data-quick]");
      spinBtn(btn, true);
      try { await addBook(item.url, { cover: item.cover_url, title: item.title, source: d.source, protocol: item.protocol, format: kindOf(item) }); }
      catch (err) { toast(err.message, { kind: "err" }); spinBtn(btn, false); }
      return;
    }
    openDrawer({ url: item.url, preview: item, source: d.source, format: kindOf(item) });
  };
  root.onkeydown = (e) => {
    if (e.key === "Enter" && e.target.matches(".card")) e.target.click();
  };
  // A Prowlarr release found while searching for ebooks is an ebook (catalog books pick their own format).
  function kindOf(item) {
    return item.source === "prowlarr" && d.kind === "ebook" ? "ebook" : undefined;
  }

  const io = new IntersectionObserver((entries) => {
    if (entries[0].isIntersecting && !d.loading && d.page && d.page < d.total && !d.error) load(false);
  }, { rootMargin: "600px" });
  io.observe(sentinel);

  if (d.items.length) { renderItems(); heading(); footer(); }
  else load(true);

  return {
    update(structural) {
      if (structural && !d.loading && d.items.length) return renderItems();
      const lib = byUrl();
      $$(".card[data-url]", root).forEach((card) => {
        const b = libFor(lib, card.dataset.url);
        const bar = $(".bar i", card);
        if (bar && b) bar.style.width = `${b.progress || 0}%`;
        const badge = $(".st-badge", card);
        if (badge && b) badge.outerHTML = stBadge(b);
      });
    },
    destroy() { io.disconnect(); document.removeEventListener("click", outsideScope); },
  };
}

// ---------------------------------------------------------------------------
// Library

function libCardHTML(b, i) {
  const h = hue(b.title + b.author);
  const active = ACTIVE.includes(b.status);
  const listening = underway(b);
  const ebook = isEbook(b);
  const quick = b.status !== "completed" ? ""
    : canRead(b) ? `<button class="quick play-quick" data-play="${b.id}" aria-label="Read ${esc(b.title)}" title="Read">${I.book}</button>`
    : ebook ? `<a class="quick" href="/api/library/${b.id}/ebook" download aria-label="Download the ebook of ${esc(b.title)}" title="Download ebook">${I.down}</a>`
    : `<button class="quick play-quick" data-play="${b.id}" aria-label="Play ${esc(b.title)}" title="Play">${I.play}</button>`;
  const facts = listening ? [ebook ? readLeft(b) : fmtLeft(b.listen_duration - b.listen_position)]
    : b.status === "requested" ? [b.requested_by_name ? `Asked by ${b.requested_by_name}` : "Awaiting approval"]
    : b.status === "wanted" ? [b.last_searched ? `Looked ${ago(b.last_searched)}` : "Not looked for yet"]
    : b.status === "failed" ? [(b.error || "Couldn’t download").slice(0, 70)]
    : active ? [b.status === "queued" ? "Waiting for a free slot" : b.detail || STATUS[b.status]]
    : b.finished ? [ebook ? "Read" : "Finished"]
    : ebook ? [b.format, b.size] : [shortLength(b.length), b.size];
  return `
    <article class="card" tabindex="0" data-id="${b.id}" style="--glow:hsl(${h} 80% 55%);animation-delay:${Math.min(i, 12) * 30}ms">
      <div class="cover-wrap">
        ${coverHTML(b)}
        ${stBadge(b, true)}
        ${active ? `<div class="bar"><i style="width:${b.progress || 0}%"></i></div>` : ""}
        ${listening ? `<div class="listen-bar"><i style="width:${(b.listen_position / b.listen_duration) * 100}%"></i></div>` : ""}
        ${ebook ? `<span class="kind-tag" title="Ebook">${I.book}Ebook</span>` : ""}
        ${quick}
      </div>
      <div class="meta">
        <div class="title">${esc(b.title)}</div>
        <div class="author">${esc(b.author || "Unknown author")}</div>
        <div class="facts ${b.status === "failed" ? "bad" : ""}">${facts.filter(Boolean).map((f) => `<span>${esc(f)}</span>`).join("")}</div>
      </div>
    </article>`;
}

function continueListening() {
  return shelfHTML("Continue listening", (b) => !isEbook(b)) + shelfHTML("Continue reading", isEbook);
}

function shelfHTML(title, which) {
  const books = state.library
    .filter((b) => which(b) && underway(b))
    .sort((a, b) => (b.listened_at || 0) - (a.listened_at || 0))
    .slice(0, 8);
  if (!books.length) return "";
  return `
    <div class="section-head"><h2>${title}</h2></div>
    <div class="shelf">${books.map((b, i) => `
      <button class="resume" data-play="${b.id}" style="animation-delay:${i * 40}ms">
        ${coverHTML(b)}
        <span style="min-width:0">
          <b>${esc(b.title)}</b>
          <small>${esc(isEbook(b) ? readLeft(b) : fmtLeft(b.listen_duration - b.listen_position))}</small>
          <span class="progress"><i style="width:${(b.listen_position / b.listen_duration) * 100}%"></i></span>
        </span>
        <span class="go">${isEbook(b) ? I.book : I.play}</span>
      </button>`).join("")}
    </div>`;
}

async function playBook(id) {
  const book = state.library.find((b) => b.id === id);
  if (!book) return;
  if (isEbook(book)) return canRead(book) ? reader.open(book) : downloadEbook(book);
  if (player.book?.id === id) player.toggle();
  else await player.open(book);
}

function downloadEbook(b) {
  const a = document.createElement("a");
  a.href = `/api/library/${b.id}/ebook`;
  a.download = "";
  document.body.append(a);
  a.click();
  a.remove();
}

// Requests waiting for approval: admins approve them right here; a listener sees their own.
function requestsTrayHTML() {
  const admin = isAdmin();
  const reqs = state.library.filter((b) => b.status === "requested" && (admin || mine(b)))
    .sort((a, b) => a.updated_at - b.updated_at);
  if (!reqs.length) return "";
  const n = reqs.length;
  return `
    <section class="req-tray" aria-labelledby="req-title">
      <div class="req-head">
        <h2 id="req-title">${I.bell}${admin ? (n === 1 ? "A request is waiting for you" : `${n} requests are waiting for you`)
          : n === 1 ? "Your request is waiting for approval" : `Your ${n} requests are waiting for approval`}</h2>
        <span>${admin ? "Nothing downloads until you approve" : "An admin approves requests before anything downloads"}</span>
      </div>
      ${reqs.map((b) => {
        const picked = !isCatalog(b.source);
        const what = picked ? `picked “${esc(b.release || b.title)}”${b.size ? ` · ${esc(b.size)}` : ""}`
          : admin ? "no release picked: approving starts the search" : "any good release";
        return `
        <div class="req-row" data-id="${b.id}">
          ${coverHTML(b)}
          <div class="req-copy">
            <b>${esc(b.title)}</b> <span class="req-by">· ${esc(firstAuthor(b.author) || "Unknown author")}</span>${isEbook(b) ? ` <span class="kind-inline">${I.book}Ebook</span>` : ""}
            <small>${admin ? `<b>${esc(b.requested_by_name || "A listener")}</b> asked` : "Asked"} ${esc(ago(b.updated_at))} · ${what}</small>
          </div>
          <div class="req-actions">${admin ? `
            <button class="btn primary sm" data-act="approve">${I.check} Approve</button>
            <button class="btn sm" data-act="releases">Pick a release</button>
            <button class="btn sm ghost danger" data-act="remove">Decline</button>`
            : `<button class="btn sm ghost danger" data-act="remove" data-mine>Take back</button>`}
          </div>
        </div>`;
      }).join("")}
    </section>`;
}

const LIB_SORTS = {
  added: ["Recently added", (a, b) => b.added_at - a.added_at],
  listened: ["Recently listened", (a, b) => (b.listened_at || 0) - (a.listened_at || 0) || b.added_at - a.added_at],
  title: ["Title A–Z", (a, b) => a.title.localeCompare(b.title)],
  author: ["Author A–Z", (a, b) => (a.author || "").localeCompare(b.author || "") || a.title.localeCompare(b.title)],
};

function Library(root) {
  const filters = [
    ["all", "All", () => true],
    ["completed", "On your shelf", (b) => b.status === "completed"],
    ["listening", "Listening", (b) => underway(b)],
    ["finished", "Finished", (b) => b.status === "completed" && b.finished],
    ["active", "In progress", (b) => ACTIVE.includes(b.status)],
    ["requested", "Awaiting approval", (b) => b.status === "requested"],
    ["wanted", "Wanted", (b) => b.status === "wanted"],
    ["failed", "Failed", (b) => b.status === "failed"],
  ];
  state.libSort ||= "added";

  root.innerHTML = `
    <header class="page-row">
      <div>
        <div class="eyebrow">Your collection</div>
        <h1 class="page-title">The <em>Library</em></h1>
        <p class="lede" id="lib-summary"></p>
      </div>
      <button class="btn" id="import-btn">${I.down} Import books</button>
    </header>
    <div id="req-tray"></div>
    <div id="shelf"></div>
    <section class="lib-all" aria-labelledby="lib-all-title">
      <div class="section-head">
        <h2 id="lib-all-title">Everything</h2>
        <div class="lib-tools">
          <div class="segmented formats-filter" id="lib-format" hidden></div>
          <label class="lib-find">${I.search}<input id="lib-search" type="search" placeholder="Title or author" value="${esc(state.libSearch)}" aria-label="Filter by title or author" /></label>
          <select class="select" id="lib-sort" aria-label="Sort">${Object.entries(LIB_SORTS).map(([k, [label]]) =>
            `<option value="${k}" ${state.libSort === k ? "selected" : ""}>${label}</option>`).join("")}</select>
        </div>
      </div>
      <div class="fpills" id="filters" role="tablist" aria-label="Show"></div>
      <div class="grid" id="lib-grid"></div>
    </section>`;

  function render() {
    const lib = state.library;
    const count = (fn) => lib.filter(fn).length;
    const shelf = count((b) => b.status === "completed");
    const done = count((b) => b.status === "completed" && b.finished);
    $("#lib-summary", root).textContent = lib.length
      ? `${lib.length} book${lib.length === 1 ? "" : "s"} · ${shelf} on your shelf · ${done} finished` : "Nothing here yet.";
    $("#req-tray", root).innerHTML = requestsTrayHTML();
    $("#shelf", root).innerHTML = continueListening();

    $("#filters", root).innerHTML = filters.filter(([k, , fn]) => k === "all" || count(fn) || state.libFilter === k).map(([k, label, fn]) =>
      `<button type="button" role="tab" aria-selected="${state.libFilter === k}" class="fpill ${state.libFilter === k ? "on" : ""}" data-f="${k}">${label} <small>${count(fn)}</small></button>`).join("");

    // Audiobooks and ebooks side by side, or one at a time once there are ebooks to tell apart.
    const ebooks = count(isEbook);
    const fmtBox = $("#lib-format", root);
    fmtBox.hidden = !ebooks;
    if (!ebooks) state.libFormat = "";
    fmtBox.innerHTML = [["", "All"], ["audiobook", `${I.headphones} Audiobooks`], ["ebook", `${I.book} Ebooks`]]
      .map(([k, label]) => `<button class="${state.libFormat === k ? "on" : ""}" data-format="${k}" aria-pressed="${state.libFormat === k}">${label}</button>`).join("");

    const fn = (filters.find((f) => f[0] === state.libFilter) || filters[0])[2];
    const q = state.libSearch.toLowerCase();
    const books = lib.filter(fn).filter((b) => !state.libFormat || (b.kind || "audiobook") === state.libFormat)
      .filter((b) => !q || `${b.title} ${b.author} ${b.narrators.join(" ")}`.toLowerCase().includes(q))
      .sort(LIB_SORTS[state.libSort]?.[1] || LIB_SORTS.added[1]);
    const grid = $("#lib-grid", root);

    if (!lib.length) {
      grid.innerHTML = `<div class="empty" style="grid-column:1/-1">
        <div class="art">${I.books}</div>
        <h3>Your shelves are empty</h3>
        <p>Find something on Discover and it’ll land here, organized and ready to listen to. Already have audiobooks? Bring them in.</p>
        <div class="cta-row"><a class="btn primary" href="#/discover">${I.search} Start discovering</a>
        <button class="btn" data-import>${I.down} Import books you have</button></div></div>`;
    } else if (!books.length) {
      grid.innerHTML = `<div class="empty" style="grid-column:1/-1"><div class="art">${I.search}</div><h3>No matches</h3><p>Nothing in this view matches your filter.</p></div>`;
    } else {
      grid.innerHTML = books.map(libCardHTML).join("");
    }
  }

  $("#filters", root).onclick = (e) => {
    const b = e.target.closest("[data-f]");
    if (!b) return;
    state.libFilter = b.dataset.f;
    render();
  };
  $("#lib-search", root).oninput = (e) => { state.libSearch = e.target.value; render(); };
  $("#lib-sort", root).onchange = (e) => { state.libSort = e.target.value; render(); };
  $("#lib-format", root).onclick = (e) => {
    const b = e.target.closest("[data-format]");
    if (!b) return;
    state.libFormat = b.dataset.format;
    render();
  };
  root.onclick = async (e) => {
    if (e.target.closest("a.quick")) return; // an ebook download: let the link do it
    if (e.target.closest("#import-btn, [data-import]")) return openImport();
    const row = e.target.closest(".req-row");
    const act = e.target.closest("[data-act]");
    if (row && act) {
      const id = Number(row.dataset.id);
      if (act.dataset.act === "releases") {
        await openDrawer({ bookId: id });
        const lib = drawerBook();
        if (lib) showReleases(lib);
        return;
      }
      spinBtn(act, true);
      try { await bookAction(id, act.dataset.act); } catch (err) { toast(err.message, { kind: "err" }); spinBtn(act, false); }
      return;
    }
    if (row) return openDrawer({ bookId: Number(row.dataset.id) });
    const play = e.target.closest("[data-play]");
    if (play) return playBook(Number(play.dataset.play));
    const card = e.target.closest(".card[data-id]");
    if (card) openDrawer({ bookId: Number(card.dataset.id) });
  };
  root.onkeydown = (e) => { if (e.key === "Enter" && e.target.matches(".card")) e.target.click(); };

  let listenSig = "";
  render();
  return {
    update(structural) {
      const sig = state.library.map((b) => `${b.id}:${b.finished}:${Math.round(b.listen_position / 60)}`).join("|");
      if (structural || sig !== listenSig) { listenSig = sig; return render(); }
      for (const b of state.library) {
        const card = $(`.card[data-id="${b.id}"]`, root);
        if (!card) continue;
        const bar = $(".bar i", card);
        if (bar) bar.style.width = `${b.progress || 0}%`;
        const badge = $(".st-badge", card);
        if (badge && ACTIVE.includes(b.status)) badge.outerHTML = stBadge(b, true);
      }
    },
  };
}

// ---------------------------------------------------------------------------
// Updates and What's new

// Enough Markdown for release notes: headings, lists, bold, code, links. Everything is escaped first.
function md(text) {
  const inline = (s) => esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  const out = [];
  let list = false;
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trimEnd();
    const h = /^(#{1,3})\s+(.*)/.exec(line);
    const li = /^\s*[-*]\s+(.*)/.exec(line);
    if (li) { if (!list) { out.push("<ul>"); list = true; } out.push(`<li>${inline(li[1])}</li>`); continue; }
    if (list) { out.push("</ul>"); list = false; }
    if (h) out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`);
    else if (line.trim()) out.push(`<p>${inline(line)}</p>`);
  }
  if (list) out.push("</ul>");
  return out.join("");
}

// The changelog, with versions you already have folded under the new ones.
function changelogHTML(markdown, current) {
  const sections = markdown.split(/^(?=## )/m);
  return sections.map((sec) => {
    const m = /^## (\d+\.\d+\.\d+)/.exec(sec);
    const older = m && cmpVersion(m[1], current) <= 0;
    return `<div class="${older ? "older" : ""}">${md(sec)}</div>`;
  }).join("");
}

const cmpVersion = (a, b) => {
  const pa = String(a).split(".").map(Number), pb = String(b).split(".").map(Number);
  for (let i = 0; i < 3; i++) if ((pa[i] || 0) !== (pb[i] || 0)) return (pa[i] || 0) - (pb[i] || 0);
  return 0;
};

async function checkUpdates(force = false) {
  try {
    state.updates = await api(`/api/updates${force ? "?force=true" : ""}`);
  } catch { return null; }
  const pill = $("#update-pill");
  pill.hidden = !state.updates.update_available;
  if (state.updates.update_available) $("span", pill).textContent = `Update to ${state.updates.latest.version}`;
  return state.updates;
}
$("#update-pill").onclick = () => openWhatsNew();

async function openWhatsNew() {
  const scrim = $("#modal");
  const u = state.updates || (await checkUpdates()) || {};
  const latest = u.latest;
  const newer = u.update_available;
  scrim.hidden = false;
  scrim.innerHTML = `
    <div class="modal whatsnew" role="dialog" aria-modal="true" aria-labelledby="wn-title">
      <h3 id="wn-title">${newer ? `Narratarr ${esc(latest.version)} is out` : "What’s new"}</h3>
      <div class="ver"><b>You have ${esc(u.current || "")}</b>${latest ? `<span>Latest ${esc(latest.version)}${latest.published ? ` · ${esc(latest.published)}` : ""}</span>` : ""}
        ${latest ? `<a href="${esc(latest.url)}" target="_blank" rel="noopener noreferrer" style="color:var(--accent)">On GitHub ↗</a>` : ""}
        <a href="${DISCORD_URL}" target="_blank" rel="noopener noreferrer" style="color:var(--accent)">Talk about it on Discord ↗</a></div>
      <div class="notes" id="wn-notes"><span class="spinner"></span></div>
      <div id="wn-progress"></div>
      <div class="actions">
        <button class="btn ghost" data-act="cancel">Close</button>
        ${newer && u.can_self_update ? `<button class="btn primary" data-act="update">${I.down} Update now</button>` : ""}
      </div>
    </div>`;
  const close = () => { scrim.hidden = true; scrim.innerHTML = ""; document.removeEventListener("keydown", onKey, true); };
  const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
  document.addEventListener("keydown", onKey, true);

  const notes = $("#wn-notes", scrim);
  try {
    // Release notes for the newer version come from GitHub; the bundled changelog covers the rest.
    const log = await api("/api/changelog");
    const body = log.markdown.replace(/^[\s\S]*?(?=^## )/m, "");  // skip the preamble, start at the first version
    let head = "";
    if (newer && latest.notes && !body.includes(`## ${latest.version}`)) {
      const n = latest.notes.trim();
      head = (/^#{1,3}\s/.test(n) ? n : `## ${latest.version}\n\n${n}`) + "\n\n";
    }
    notes.innerHTML = changelogHTML(head + body, log.current);
  } catch (e) {
    notes.innerHTML = `<p>${esc(e.message)}</p>`;
  }
  if (newer && !u.can_self_update) {
    const how = u.install === "docker" ? "This copy runs in Docker, so update it from the machine that runs it:"
      : u.install === "source" ? "This copy runs from the source code. Update it in that folder:"
      : "Update this copy by running the installer again:";
    const cmd = u.install === "docker" ? "docker compose pull && docker compose up -d"
      : u.install === "source" ? "git pull && uv sync"
      : "curl -fsSL https://raw.githubusercontent.com/narratarr/narratarr/main/install.sh | bash";
    $("#wn-progress", scrim).innerHTML = `<div class="update-progress"><div>${esc(how)}<code class="update-cmd">${esc(cmd)}</code></div></div>`;
  }

  scrim.onclick = async (e) => {
    if (e.target === scrim || e.target.closest('[data-act="cancel"]')) return close();
    const btn = e.target.closest('[data-act="update"]');
    if (!btn) return;
    spinBtn(btn, true);
    const prog = $("#wn-progress", scrim);
    try {
      await api("/api/updates/apply", { method: "POST", body: { version: latest.version } });
    } catch (err) {
      prog.innerHTML = `<div class="update-progress bad">${esc(err.message)}</div>`;
      spinBtn(btn, false);
      return;
    }
    btn.remove();
    prog.innerHTML = `<div class="update-progress"><span class="spinner"></span><span id="wn-detail">Starting…</span></div>`;
    // Watch it install, then wait for the new version to come back up and reload.
    const before = u.current;
    const started = Date.now();
    while (Date.now() - started < 15 * 60 * 1000) {
      await new Promise((r) => setTimeout(r, 1500));
      try {
        const s = await api("/api/updates");
        if (s.current !== before) { location.reload(); return; }
        if (s.status === "failed") { prog.innerHTML = `<div class="update-progress bad">${esc(s.detail)}</div>`; return; }
        $("#wn-detail", scrim).textContent = s.detail || "Working…";
      } catch {
        $("#wn-detail", scrim).textContent = "Restarting…";  // the old process is gone; the new one is starting
      }
    }
    prog.innerHTML = `<div class="update-progress bad">That took too long. Check the log, or run the installer again.</div>`;
  };
}

// ---------------------------------------------------------------------------
// Import books you already have

function openImport(folder = "") {
  const scrim = $("#modal");
  const st = state.settings || {};
  const library = st.library_dir || "";
  const imp = { books: [], folder: "", mode: "copy" };
  scrim.hidden = false;
  scrim.innerHTML = `
    <div class="modal wide import" role="dialog" aria-modal="true" aria-labelledby="imp-title">
      <h3 id="imp-title">Import books you already have</h3>
      <p>Point Narratarr at a folder on this computer. Each folder of audio files becomes one audiobook, and each ebook file (EPUB, MOBI, AZW3, PDF…) one ebook, named from the files themselves or the folder names.</p>
      <form class="input-row" id="imp-form">
        <input class="input mono" id="imp-folder" placeholder="/path/to/your/audiobooks" value="${esc(folder)}" autocomplete="off" />
        <button class="btn primary" id="imp-scan" type="submit">${I.search} Scan</button>
      </form>
      <div class="chips imp-quick">
        ${library ? `<button class="chip" type="button" data-folder="${esc(library)}">Your library folder</button>` : ""}
        ${st.import_folder ? `<button class="chip" type="button" data-folder="${esc(st.import_folder)}">Import folder</button>` : ""}
      </div>
      <div id="imp-results"></div>
      <div class="actions">
        <button class="btn ghost" data-act="cancel">Cancel</button>
        <button class="btn primary" data-act="import" disabled>Import</button>
      </div>
    </div>`;

  const close = () => {
    scrim.hidden = true;
    scrim.innerHTML = "";
    document.removeEventListener("keydown", onKey, true);
  };
  const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
  document.addEventListener("keydown", onKey, true);

  const results = $("#imp-results", scrim);
  const go = $('[data-act="import"]', scrim);
  const chosen = () => $$("[data-pick]:checked", results).map((c) => imp.books[Number(c.dataset.pick)]);
  const MODES = [
    ["copy", "Copy in", "Copies them into your library folder and leaves the originals alone."],
    ["move", "Move in", "Moves them into your library folder, organized as Author/Title."],
    ["inplace", "Leave where they are", "Adds them to Narratarr without touching the files. Loose audio files are copied in."],
  ];
  const sync = () => {
    const n = chosen().length;
    go.disabled = !n;
    go.textContent = n ? `Import ${n} book${n === 1 ? "" : "s"}` : "Import";
    $$(".imp-mode button", results).forEach((b) => b.classList.toggle("on", b.dataset.mode === imp.mode));
    const hint = $(".imp-mode-hint", results);
    if (hint) hint.textContent = MODES.find((m) => m[0] === imp.mode)[2];
  };

  function render(res) {
    imp.books = res.books;
    imp.folder = res.folder;
    const fresh = res.books.filter((b) => !b.in_library).length;
    if (!res.books.length) {
      results.innerHTML = `<div class="imp-empty">${I.search}<p>No books found in that folder. Narratarr looks for MP3, M4B, M4A, FLAC, OGG and Opus audio, and EPUB, MOBI, AZW3, PDF, FB2, CBZ and CBR ebooks.</p></div>`;
      return sync();
    }
    imp.mode = res.books.every((b) => b.inside_library) ? "inplace" : "copy";
    results.innerHTML = `
      <div class="imp-head"><b>${res.books.length} book${res.books.length === 1 ? "" : "s"} found</b>
        <span class="muted">${fresh} new</span>
        <label class="imp-all"><input type="checkbox" id="imp-all" ${fresh ? "checked" : ""}> Select all new</label></div>
      <div class="imp-list">${res.books.map((b, i) => `
        <label class="imp-row ${b.in_library ? "done" : ""}">
          <input type="checkbox" data-pick="${i}" ${b.in_library ? "disabled" : "checked"}>
          <span class="imp-meta">
            <input class="imp-title" data-edit="title" data-i="${i}" value="${esc(b.title)}" aria-label="Title" ${b.in_library ? "disabled" : ""}>
            <input class="imp-author" data-edit="author" data-i="${i}" value="${esc(b.author)}" placeholder="Unknown author" aria-label="Author" ${b.in_library ? "disabled" : ""}>
            <small>${b.in_library ? "Already in your library · " : ""}${b.kind === "ebook" ? `<span class="kind-inline">${I.book}Ebook</span> · ` : `${b.files} file${b.files === 1 ? "" : "s"} · `}${esc(fmtBytes(b.bytes))}${b.format ? ` · ${esc(b.format)}` : ""}</small>
          </span>
        </label>`).join("")}
      </div>
      <div class="imp-mode-row">
        <div class="segmented imp-mode">${MODES.map(([m, label]) => `<button type="button" data-mode="${m}">${label}</button>`).join("")}</div>
        <span class="hint imp-mode-hint"></span>
      </div>`;
    sync();
  }

  async function scan(path) {
    if (!path.trim()) return;
    const btn = $("#imp-scan", scrim);
    spinBtn(btn, true);
    results.innerHTML = `<div class="imp-empty"><span class="spinner"></span><p>Looking through ${esc(path)}…</p></div>`;
    try {
      render(await api("/api/import/scan", { method: "POST", body: { folder: path } }));
    } catch (e) {
      results.innerHTML = `<div class="state failed" style="margin-top:16px">${esc(e.message)}</div>`;
    }
    spinBtn(btn, false);
  }

  $("#imp-form", scrim).onsubmit = (e) => { e.preventDefault(); scan($("#imp-folder", scrim).value); };
  scrim.oninput = (e) => {
    const ed = e.target.closest("[data-edit]");
    if (ed) imp.books[Number(ed.dataset.i)][ed.dataset.edit] = ed.value;
  };
  scrim.onchange = (e) => {
    if (e.target.id === "imp-all") $$("[data-pick]:not(:disabled)", results).forEach((c) => { c.checked = e.target.checked; });
    sync();
  };
  scrim.onclick = async (e) => {
    if (e.target === scrim || e.target.closest('[data-act="cancel"]')) return close();
    const quick = e.target.closest("[data-folder]");
    if (quick) { $("#imp-folder", scrim).value = quick.dataset.folder; return scan(quick.dataset.folder); }
    const mode = e.target.closest("[data-mode]");
    if (mode) { imp.mode = mode.dataset.mode; return sync(); }
    if (e.target.closest('[data-act="import"]')) {
      const books = chosen().map((b) => ({ path: b.path, title: b.title, author: b.author }));
      spinBtn(go, true);
      try {
        const r = await api("/api/import", { method: "POST", body: { folder: imp.folder, mode: imp.mode, books } });
        close();
        await refreshLibrary();
        toast(r.added ? `${r.added} book${r.added === 1 ? "" : "s"} ${imp.mode === "inplace" ? "added to your library" : "on the way into your library"}` : "Those books are already in your library",
          { kind: "ok" });
      } catch (err) {
        toast(err.message, { kind: "err" });
        spinBtn(go, false);
      }
    }
  };
  if (folder) scan(folder); else $("#imp-folder", scrim).focus();
}

// ---------------------------------------------------------------------------
// Authors & series you follow

function portraitHTML(f, cls = "") {
  const initials = (f.name || "?").split(/\s+/).map((w) => w[0]).slice(0, 2).join("").toUpperCase();
  return `<div class="portrait ${f.kind} ${cls}" style="--h:${hue(f.name)}">${f.image
    ? `<img src="${esc(f.image)}" alt="" loading="lazy" onerror="this.remove()">` : ""}<span>${esc(initials)}</span></div>`;
}

function Authors(root, param) {
  if (param) return AuthorDetail(root, Number(param));
  const st = state.settings || {};
  root.innerHTML = `
    <header>
      <div class="eyebrow">Following</div>
      <h1 class="page-title">Authors &amp; <em>series</em></h1>
      <p class="lede">Follow an author or a series and their new books go straight to Wanted, then download as soon as they turn up on your indexers.</p>
      <p class="lede asks-note">Ask an admin to follow someone new. You can still ask for any of their books.</p>
    </header>
    ${!st.indexer_configured ? `
      <div class="banner">
        <div class="ico">${I.key}</div>
        <div><strong>Connect Prowlarr or a Torznab indexer so Narratarr can find these books</strong><p>You can follow authors now. Their books will wait in Wanted until Prowlarr or a Torznab indexer is set up.</p></div>
        <a class="btn" href="#/settings">Open Settings</a>
      </div>` : ""}
    <div class="search-wrap follow-search">
      <form class="search" id="author-form" role="search">
        ${I.search}
        <input id="aq" type="search" placeholder="Follow an author… e.g. Brandon Sanderson" autocomplete="off" aria-label="Find an author to follow" />
        <button class="btn primary" type="submit">Find</button>
      </form>
      <div class="suggest" id="author-results" hidden></div>
    </div>
    <div id="follows"></div>`;

  // "9 of 12 in your library", and what's going on with the rest.
  const totalOf = (f) => Math.max(f.known || 0, f.in_library + f.wanted, 1);
  const tagsOf = (f) => [
    f.new && `<span class="tag accent">New book</span>`,
    f.downloading && `<span class="tag info">Downloading</span>`,
    f.requested && `<span class="tag accent">${f.requested} awaiting approval</span>`,
    f.wanted - f.downloading - f.requested > 0 && `<span class="tag">${f.wanted - f.downloading - f.requested} wanted</span>`,
    f.in_library && f.in_library >= totalOf(f) && `<span class="tag ok">All caught up</span>`,
  ].filter(Boolean).join("");
  const seriesNote = (f) => f.downloading ? ["info", "A book is downloading"]
    : f.requested ? ["accent", `${f.requested} awaiting approval`]
    : f.wanted ? ["", `${f.wanted} wanted`]
    : f.in_library ? ["ok", `All ${f.in_library} in your library`] : ["", "Nothing in your library yet"];
  const stack = (f) => `<span class="stack" aria-hidden="true">${[2, 1, 0].map((i) => {
    const src = f.covers?.[i];
    return `<span class="s${i}" style="--h:${hue(f.name + i)}">${src ? `<img src="${esc(src)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">` : ""}</span>`;
  }).join("")}</span>`;

  function render() {
    const authors = state.follows.filter((f) => f.kind === "author");
    const series = state.follows.filter((f) => f.kind === "series");
    const fresh = authors.filter((f) => f.new).length;
    $("#follows", root).innerHTML = !state.follows.length ? `
      <div class="empty">
        <div class="art">${I.books}</div>
        <h3>Never miss a new release</h3>
        <p>Search for an author above, or open any book from the Audible catalog and choose Follow.</p>
      </div>` : `
      ${authors.length ? `
      <section aria-labelledby="au-title">
        <div class="section-head"><h2 id="au-title">Authors you follow</h2><span class="muted">${authors.length} author${authors.length === 1 ? "" : "s"}${fresh ? ` · ${fresh} with a new book` : ""}</span></div>
        <div class="author-grid">${authors.map((f, i) => `
          <a class="author-card" href="#/authors/${f.id}" style="animation-delay:${Math.min(i, 12) * 35}ms">
            <div class="ac-top">${portraitHTML(f)}<span><b>${esc(f.name)}</b><small>${f.in_library} of ${totalOf(f)} in your library</small></span></div>
            <span class="meter" aria-hidden="true"><i style="width:${Math.round((f.in_library / totalOf(f)) * 100)}%"></i></span>
            <span class="tags">${tagsOf(f)}</span>
          </a>`).join("")}</div>
      </section>` : ""}
      ${series.length ? `
      <section aria-labelledby="se-title">
        <div class="section-head"><h2 id="se-title">Series you follow</h2></div>
        <div class="series-grid">${series.map((f) => {
          const [tone, note] = seriesNote(f);
          return `
          <a class="series-card" href="#/authors/${f.id}">
            ${stack(f)}
            <span class="sc-copy"><b>${esc(f.name)}</b><small class="${tone}">${esc(note)}</small></span>
          </a>`;
        }).join("")}</div>
      </section>` : ""}`;
  }

  const results = $("#author-results", root);
  let timer = 0, token = 0;
  async function find(q) {
    const mine = ++token;
    if (q.trim().length < 2) { results.hidden = true; return; }
    results.hidden = false;
    results.innerHTML = `<div class="label">Authors</div><div style="padding:14px;color:var(--text-3)"><span class="spinner"></span></div>`;
    try {
      const found = await api(`/api/authors/search?${new URLSearchParams({ q })}`);
      if (mine !== token) return;
      results.innerHTML = found.length ? `<div class="label">Authors on Audible</div>` + found.map((a, i) => {
        const f = state.follows.find((x) => x.kind === "author" && x.asin === a.asin);
        return `<button type="button" data-author="${i}">
          ${a.image ? `<img src="${esc(a.image)}" alt="" style="border-radius:50%">` : `<span class="ph" style="border-radius:50%"></span>`}
          <span><b>${esc(a.name)}</b><small>${f ? "Following" : esc((a.description || "").slice(0, 90)) || "Follow"}</small></span>
        </button>`;
      }).join("") : `<div style="padding:14px 16px;color:var(--text-3)">No authors by that name on Audible.</div>`;
      results._found = found;
    } catch (e) {
      results.innerHTML = `<div style="padding:14px 16px;color:var(--err)">${esc(e.message)}</div>`;
    }
  }
  $("#aq", root).oninput = (e) => { clearTimeout(timer); timer = setTimeout(() => find(e.target.value), 350); };
  $("#author-form", root).onsubmit = (e) => { e.preventDefault(); find($("#aq", root).value); };
  results.onclick = async (e) => {
    const b = e.target.closest("[data-author]");
    if (!b) return;
    const a = results._found[Number(b.dataset.author)];
    const existing = state.follows.find((x) => x.kind === "author" && x.asin === a.asin);
    if (existing) { location.hash = `#/authors/${existing.id}`; return; }
    try {
      const f = await confirmFollow({ kind: "author", asin: a.asin, name: a.name });
      if (f) { results.hidden = true; $("#aq", root).value = ""; render(); }
    } catch (err) { toast(err.message, { kind: "err" }); }
  };
  const outside = (e) => { if (!e.target.closest(".follow-search")) results.hidden = true; };
  document.addEventListener("click", outside);

  render();
  refreshFollows().then(render);
  return { destroy() { document.removeEventListener("click", outside); }, update(structural) { if (structural) refreshFollows().then(render); } };
}

function AuthorDetail(root, id) {
  const f0 = state.follows.find((f) => f.id === id);
  let data = null;
  root.innerHTML = `
    <a class="back-link" href="#/authors"><svg viewBox="0 0 24 24"><path d="m14.5 6-6 6 6 6"/></svg> Authors &amp; series</a>
    <header class="author-hero">
      ${portraitHTML(f0 || { name: "", kind: "author" }, "big")}
      <div class="author-copy">
        <div class="eyebrow">${f0?.kind === "series" ? "Series you follow" : "Author you follow"}</div>
        <h1 class="page-title">${esc(f0?.name || "Loading…")}</h1>
        <div class="hero-stats" id="a-stats"></div>
        <p class="bio" id="bio"></p>
      </div>
      <div class="hero-actions" id="hero-actions"></div>
    </header>
    <div id="a-books"><div class="grid">${skeletons(10)}</div></div>`;

  const libFor = (b) => b.library && state.library.find((x) => x.id === b.library.id);
  function render() {
    if (!data) return;
    const books = data.books.map((b) => ({ ...b, source: "audible", posted: b.released }));
    const have = books.filter((b) => libFor(b));
    const missing = books.length - have.length;
    const mine = [...new Map(have.map((b) => [libFor(b).id, libFor(b)])).values()];  // one per library book
    $("#a-stats", root).innerHTML = [
      ["ok", mine.filter((b) => b.status === "completed").length, "In your library"],
      ["info", mine.filter((b) => b.status !== "completed").length, "Wanted or downloading"],
      ["", missing, "Not added yet"],
    ].map(([tone, n, l]) => `<div class="hstat ${tone}"><b>${n}</b><span>${l}</span></div>`).join("");
    $("#hero-actions", root).innerHTML = `
      ${missing ? `<button class="btn primary" data-act="want-all">${I.bookmark} Add ${missing} missing to Wanted</button>` : ""}
      <button class="btn ghost danger" data-act="unfollow">Unfollow</button>`;
    // An author's books by series (each in reading order), then everything else; a series is one group.
    const groups = new Map();
    for (const b of books) {
      const ser = data.kind === "author" ? (b.series || [])[0] : null;
      const key = ser?.title || (data.kind === "series" ? data.name : "");
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(b);
    }
    const seq = (b) => parseFloat((b.series || [])[0]?.sequence) || 999;
    const ordered = [...groups.entries()].sort(([a], [b]) => (a === "") - (b === ""));
    $("#a-books", root).innerHTML = books.length ? ordered.map(([name, list]) => {
      if (name) list.sort((a, b) => seq(a) - seq(b));
      const owned = list.filter((b) => libFor(b)?.status === "completed").length;
      const notYet = list.filter((b) => !libFor(b)).length;
      return `
      <section class="book-group">
        <div class="section-head"><h2>${esc(name || (groups.size > 1 ? "Standalones and others" : "Books"))}</h2>
          <span class="muted">${owned} of ${list.length} in your library${notYet ? ` · ${notYet} not added yet` : ""}</span></div>
        <div class="grid">${list.map((b, i) => cardHTML(b, libFor(b), i).replace('class="card"', `class="card${libFor(b) ? "" : " missing"}"`)).join("")}</div>
      </section>`;
    }).join("") : `<div class="empty"><h3>No books yet</h3><p>Audible doesn't list any books here yet.</p></div>`;
    root._books = books;
  }

  async function load() {
    try {
      data = await api(`/api/follows/${id}`);
    } catch (e) {
      $("#a-books", root).innerHTML = `<div class="empty" style="grid-column:1/-1"><div class="art">${I.alert}</div><h3>Couldn’t load this page</h3><p>${esc(e.message)}</p><a class="btn" href="#/authors">Back to Authors</a></div>`;
      return;
    }
    $(".author-hero", root).querySelector(".portrait").outerHTML = portraitHTML(data, "big");
    $(".author-hero h1", root).textContent = data.name;
    $(".author-hero .eyebrow", root).textContent = data.kind === "series" ? "Series you follow" : "Author you follow";
    const bio = $("#bio", root);
    bio.textContent = data.description || "";
    bio.classList.toggle("clamped", (data.description || "").length > 360);
    render();
  }

  root.onclick = async (e) => {
    const act = e.target.closest("[data-act]");
    if (act?.dataset.act === "unfollow") {
      const res = await modal({
        title: `Unfollow ${data?.name || "this"}?`, body: "New books won't be added to Wanted any more.", confirm: "Unfollow", danger: true,
        extra: `<label class="switch"><input type="checkbox" name="purge"><i></i>Also remove their books that are still only Wanted</label>`,
      });
      if (!res) return;
      await api(`/api/follows/${id}?remove_wanted=${!!res.purge}`, { method: "DELETE" });
      await Promise.all([refreshFollows(), refreshLibrary()]);
      toast(`Unfollowed ${data?.name || ""}`);
      location.hash = "#/authors";
      return;
    }
    if (act?.dataset.act === "want-all") {
      spinBtn(act, true);
      try {
        const r = await api(`/api/follows/${id}/want-all`, { method: "POST" });
        await refreshLibrary();
        toast(`${r.added} book${r.added === 1 ? "" : "s"} added to Wanted`, { kind: "ok" });
        await load();
      } catch (err) { toast(err.message, { kind: "err" }); spinBtn(act, false); }
      return;
    }
    if (e.target.closest("a.quick")) return; // an ebook download: let the link do it
    const play = e.target.closest("[data-play]");
    if (play) return playBook(Number(play.dataset.play));
    const card = e.target.closest(".card[data-url]");
    if (!card || !root._books) return;
    const b = root._books.find((x) => x.url === card.dataset.url);
    const lib = libFor(b);
    if (e.target.closest("[data-quick]")) {
      const q = e.target.closest("[data-quick]");
      spinBtn(q, true);
      try { await addBook(b.url, { cover: b.cover_url, title: b.title, source: "audible" }); await load(); }
      catch (err) { toast(err.message, { kind: "err" }); spinBtn(q, false); }
      return;
    }
    if (lib) openDrawer({ bookId: lib.id });
    else openDrawer({ url: b.url, preview: b, source: "audible" });
  };
  root.onkeydown = (e) => { if (e.key === "Enter" && e.target.matches(".card")) e.target.click(); };

  load();
  return { update(structural) { if (structural) render(); } };
}

// ---------------------------------------------------------------------------
// Activity

function jobDetail(b) {
  const parts = [];
  if (b.status === "queued") parts.push("Waiting for a free slot…");
  else if (b.detail) parts.push(esc(b.detail));
  if (b.speed && ["caching", "downloading"].includes(b.status)) parts.push(`${fmtBytes(b.speed)}/s`);
  if (["caching", "downloading"].includes(b.status)) parts.push(`${(b.progress || 0).toFixed(1)}%`);
  return parts.map((p) => `<span>${p}</span>`).join("");
}

function stepsHTML(b) {
  const phases = phasesFor(b);
  const idx = phases.findIndex(([s]) => s === b.status);
  return `<div class="steps">${phases.map(([, label], i) =>
    `<span class="step ${idx > i ? "done" : idx === i ? "now" : ""}"><i></i>${label}</span>`).join("")}</div>`;
}

function dayLabel(ts) {
  const d = new Date(ts * 1000), today = new Date();
  const y = new Date(); y.setDate(today.getDate() - 1);
  if (d.toDateString() === today.toDateString()) return "Today";
  if (d.toDateString() === y.toDateString()) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" });
}
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });

function Activity(root) {
  root.innerHTML = `
    <header class="page-row">
      <div>
        <div class="eyebrow live"><i aria-hidden="true"></i>Live</div>
        <h1 class="page-title">Activity</h1>
        <p class="lede">Every download, from search result to your library shelf.</p>
      </div>
      <div class="act-stats" id="act-stats"></div>
    </header>
    <div class="act-layout">
      <div class="act-main">
        <section aria-labelledby="ip-title">
          <div class="section-head"><h2 id="ip-title">In progress</h2></div>
          <div class="jobs" id="jobs"></div>
        </section>
        <div id="hist-wrap"></div>
      </div>
      <aside class="act-side" id="act-side"></aside>
    </div>`;

  function sideHTML() {
    const st = state.settings || {};
    const reqs = state.library.filter((b) => b.status === "requested" && (isAdmin() || mine(b)));
    const wanted = state.library.filter((b) => b.status === "wanted");
    let next = "";
    if (st.auto_search && st.indexer_configured && wanted.length) {
      const due = Math.min(...wanted.map((b) => (b.last_searched || 0) + (st.auto_search_hours || 12) * 3600)) - Date.now() / 1000;
      next = due <= 600 ? "Narratarr looks again in a few minutes" : `Narratarr looks again in <b>${esc(fmtLeft(due).replace(" left", ""))}</b>`;
    }
    return `
      ${reqs.length ? `
      <section class="side-card req" aria-labelledby="wait-title">
        <h2 id="wait-title">${I.bell}${isAdmin() ? "Waiting for your approval" : "Your requests"}</h2>
        ${reqs.slice(0, 4).map((b) => `
          <button type="button" class="side-row" data-id="${b.id}">${coverHTML(b)}<span><b>${esc(b.title)}</b><small>${esc(b.requested_by_name || "")}${b.requested_by_name ? " · " : ""}${esc(ago(b.updated_at))}</small></span></button>`).join("")}
        <a class="btn ${isAdmin() ? "primary" : ""} sm" href="#/library" data-review>${isAdmin() ? "Review requests" : "See in Library"}</a>
      </section>` : ""}
      <section class="side-card" aria-labelledby="want-title">
        <h2 id="want-title">Wanted</h2>
        <p>${wanted.length ? `${wanted.length} book${wanted.length === 1 ? "" : "s"}.` : "Nothing wanted right now."}
          ${!st.indexer_configured ? "Connect an indexer in Settings so Narratarr can look for them."
            : !st.auto_search ? "Automatic search is off." : next ? `${next}, a few at a time.` : ""}</p>
        ${wanted.slice(0, 4).map((b) => `
          <button type="button" class="side-line" data-id="${b.id}"><span>${esc(b.title)}</span><small>${b.last_searched ? `looked ${esc(ago(b.last_searched))}` : "not looked yet"}</small></button>`).join("")}
        ${wanted.length ? `<a class="side-link" href="#/library" data-wanted>See all Wanted</a>` : ""}
      </section>`;
  }

  function render() {
    const lib = state.library;
    const active = lib.filter((b) => ACTIVE.includes(b.status))
      .sort((a, b) => ACTIVE.indexOf(b.status) - ACTIVE.indexOf(a.status) || a.updated_at - b.updated_at);
    const recent = lib.filter((b) => ["completed", "failed"].includes(b.status))
      .sort((a, b) => (b.completed_at || b.updated_at) - (a.completed_at || a.updated_at)).slice(0, 30);
    const today = new Date().toDateString();
    const doneToday = lib.filter((b) => b.status === "completed" && b.completed_at && new Date(b.completed_at * 1000).toDateString() === today).length;

    $("#act-stats", root).innerHTML = [
      ["info", active.filter((b) => b.status !== "queued").length, "Downloading"],
      ["", active.filter((b) => b.status === "queued").length, "Waiting for a slot"],
      ["ok", doneToday, "Finished today"],
    ].map(([tone, n, label]) => `<div class="hstat box ${tone}"><b>${n}</b><span>${label}</span></div>`).join("");

    $("#jobs", root).innerHTML = active.length ? active.map((b, i) => `
      <div class="job" data-id="${b.id}" style="animation-delay:${i * 40}ms">
        ${coverHTML(b)}
        <div style="min-width:0">
          <div class="job-top">
            <div style="min-width:0">
              <h3>${esc(b.title)}</h3>
              <div class="by">${esc(b.author)}${b.requested_by_name ? ` · for ${esc(b.requested_by_name)}` : ""}${isEbook(b) ? ` <span class="kind-inline">${I.book}Ebook</span>` : ""}</div>
            </div>
            <button class="btn sm" data-act="stop">${I.stop} Stop</button>
          </div>
          ${stepsHTML(b)}
          <div class="progress ${["caching", "downloading"].includes(b.status) ? "" : "indeterminate"}"><i style="width:${b.progress || 0}%"></i></div>
          <div class="detail">${jobDetail(b)}</div>
        </div>
      </div>`).join("") : `
      <div class="empty" style="padding:50px 20px">
        <div class="art">${I.headphones}</div>
        <h3>All quiet</h3>
        <p>Nothing downloading right now. Queue up your next listen from Discover.</p>
        <a class="btn" href="#/discover">Browse books</a>
      </div>`;

    const days = new Map();
    for (const b of recent) {
      const label = dayLabel(b.completed_at || b.updated_at);
      if (!days.has(label)) days.set(label, []);
      days.get(label).push(b);
    }
    $("#hist-wrap", root).innerHTML = recent.length ? `
      <section aria-labelledby="done-title">
        <div class="section-head"><h2 id="done-title">Recently finished</h2></div>
        ${[...days.entries()].map(([label, list]) => `
          <div class="day"><span class="day-label">${esc(label)}</span>
          <div class="history">${list.map((b) => {
            const failed = b.status === "failed";
            const note = failed ? b.error || "Couldn’t download"
              : [b.author, isEbook(b) ? "ebook" : "", b.requested_by_name ? `for ${b.requested_by_name}` : ""].filter(Boolean).join(" · ");
            return `
            <div class="row ${failed ? "bad" : ""}" data-id="${b.id}">
              ${coverHTML(b)}
              <div style="min-width:0"><div class="t">${esc(b.title)}</div><div class="s">${esc(note)}</div></div>
              <div class="when">
                ${failed ? `<button class="btn sm" data-act="download">${I.retry} Try again</button>` : ""}
                <span class="st ${failed ? "bad" : "ok"}"><i aria-hidden="true"></i>${failed ? "Failed" : "In library"}</span>
                <span class="tm">${esc(clock(b.completed_at || b.updated_at))}</span>
              </div>
            </div>`;
          }).join("")}</div></div>`).join("")}
      </section>` : "";

    $("#act-side", root).innerHTML = sideHTML();
  }

  root.onclick = async (e) => {
    if (e.target.closest("[data-review]")) { state.libFilter = "requested"; return; }
    if (e.target.closest("[data-wanted]")) { state.libFilter = "wanted"; return; }
    const job = e.target.closest("[data-id]");
    if (!job) return;
    const id = Number(job.dataset.id);
    const btn = e.target.closest("[data-act]");
    if (btn) {
      spinBtn(btn, true);
      try { await bookAction(id, btn.dataset.act); } catch (err) { toast(err.message, { kind: "err" }); spinBtn(btn, false); }
      return;
    }
    openDrawer({ bookId: id });
  };

  render();
  return {
    update(structural) {
      if (structural) return render();
      for (const b of state.library.filter((x) => ACTIVE.includes(x.status))) {
        const el = $(`.job[data-id="${b.id}"]`, root);
        if (!el) continue;
        $(".progress i", el).style.width = `${b.progress || 0}%`;
        $(".detail", el).innerHTML = jobDetail(b);
      }
    },
  };
}

// ---------------------------------------------------------------------------
// Settings

// Settings: six short pages (#/settings/<page>) instead of one long one. Every field still lives in
// one form, so moving between pages keeps unsaved changes, and one Save covers them all.
// The folder layout's variables, and the same fill-in rules as narratarr/layout.py, for the live preview.
const LAYOUT_VARS = [
  ["author", "The author"], ["title", "The book's title"], ["narrator", "The narrator (up to two)"],
  ["series", "The series name"], ["series_number", "The book's number in its series, like 1 or 2.5"], ["year", "The year it came out"],
];
// The languages offered under Settings → Search & sources (as narratarr/languages.py COMMON).
const LANGUAGES = ["English", "German", "French", "Spanish", "Italian", "Dutch", "Portuguese", "Polish", "Swedish", "Danish",
  "Norwegian", "Finnish", "Czech", "Hungarian", "Russian", "Japanese", "Chinese", "Korean"];

// One click to a layout that works well: the default, by series, and the one Audiobookshelf reads most from.
const LAYOUT_PRESETS = [
  ["Author / Title", "{author}/{title}"],
  ["Author / Series / Title", "{author}/{series}/{series_number} - {title}"],
  ["Audiobookshelf style", "{author}/{series}/Vol {series_number} - {year} - {title} {{narrator}}"],
];
const LAYOUT_SAMPLE = { author: "Frank Herbert", title: "Dune", narrator: "Scott Brick", series: "Dune Chronicles", series_number: "1", year: "1965" };
const LAYOUT_SAMPLE_SOLO = { author: "Andy Weir", title: "Project Hail Mary", narrator: "Ray Porter", series: "", series_number: "", year: "2021" };
const LAYOUT_EDGES = /^[\s\-–—_.,;:#~]+|[\s\-–—_.,;:#~]+$/g;

function layoutClean(part) {
  return String(part).replace(/[<>:"/\\|?*\x00-\x1f]/g, "").trim().replace(/^\.+|\.+$/g, "").split(/\s+/).filter(Boolean).join(" ").slice(0, 150);
}

function layoutFolders(template, book) {
  const fallback = { author: "Unknown Author", title: "Untitled", narrator: "Unknown Narrator" };
  const out = [];
  for (const part of template.split("/")) {
    if (!part.trim()) continue;
    const bits = part.split(/\{([^{}]*)\}/);
    const literals = bits.filter((_, i) => i % 2 === 0);
    const alone = bits.length === 3 && !(bits[0] + bits[2]).trim(); // "{narrator}" alone may say "Unknown Narrator"
    const vals = bits.filter((_, i) => i % 2 === 1).map((n) => layoutClean(book[n] || "") || (alone && fallback[n]) || "");
    let text = part;
    if (vals.length) {
      const keep = literals.map(() => true);
      for (let i = 0; i < vals.length;) {
        if (vals[i]) { i++; continue; }
        let j = i;
        while (j < vals.length && !vals[j]) j++;
        for (let k = i; k <= j; k++) keep[k] = false;
        if (i > 0 && j < vals.length) keep[j] = true;
        i = j;
      }
      text = literals.map((l, k) => (keep[k] ? l : "") + (k < vals.length ? vals[k] : "")).join("");
      if (vals.some((v) => !v)) text = text.replace(LAYOUT_EDGES, "");
    }
    const name = layoutClean(text);
    if (name) out.push(name);
  }
  return out;
}

const SETTINGS_PAGES = [
  ["general", "General", "Where your books live, how their folders are named, and what comes in by itself.",
    '<svg viewBox="0 0 24 24"><path d="M3 7.5 12 3l9 4.5M5 9.5V19h14V9.5M9 19v-6h6v6"/></svg>'],
  ["search", "Search & sources", "Where Narratarr looks for books, and how often it looks for the ones you want.", I.search],
  ["downloads", "Downloads", "What fetches your books, and where Narratarr finds them when they’re done.", I.down],
  ["abs", "Audiobookshelf", "Every book Narratarr files shows up in Audiobookshelf as soon as it’s done.",
    '<svg viewBox="0 0 24 24"><path d="M4 19V6l8-3 8 3v13M4 19h16M9 19v-7h6v7"/></svg>'],
  ["notifications", "Notifications", "Hear about new requests and finished downloads on your phone, in Discord, or anywhere a webhook goes.",
    '<svg viewBox="0 0 24 24"><path d="M6 9.5a6 6 0 0 1 12 0c0 5 2 6.5 2 6.5H4s2-1.5 2-6.5M10 19.5a2.2 2.2 0 0 0 4 0"/></svg>'],
  ["users", "Users", "Who can sign in. Admins run Narratarr; listeners browse, request and listen, each with their own place in every book.",
    '<svg viewBox="0 0 24 24"><circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0M16 4.6a3.5 3.5 0 0 1 0 6.8M18.5 14.4A6.5 6.5 0 0 1 21.5 20"/></svg>'],
  ["updates", "Updates", "Which version you have, and whether a newer one is out.", I.retry],
];
const SETTINGS_PAGE = Object.fromEntries(SETTINGS_PAGES.map((p) => [p[0], p]));

// The one-line status under each page in the Settings menu: [text, tone] where tone is "ok", "warn" or "".
function settingsStatus(key, users, notifiers) {
  const st = state.settings || {};
  const u = state.updates;
  if (key === "general") return [st.library_dir || "", ""];
  if (key === "search") {
    const on = [st.prowlarr_configured && "Prowlarr", st.torznab_configured && "Torznab"].filter(Boolean);
    return on.length ? [`${on.join(" · ")} connected`, "ok"] : ["No indexer connected", ""];
  }
  if (key === "downloads") {
    const ready = [st.torrent_configured && CLIENTS[st.torrent_client], st.usenet_configured && CLIENTS[st.usenet_client]].filter(Boolean);
    return ready.length ? [ready.join(" · "), "ok"] : ["Set up a download client", "warn"];
  }
  if (key === "abs") return st.audiobookshelf_url && st.audiobookshelf_token ? ["Connected", "ok"] : ["Not connected", ""];
  if (key === "users") return [users == null ? "" : `${users} ${users === 1 ? "person" : "people"}`, ""];
  if (key === "notifications") {
    if (notifiers == null) return ["", ""];
    return notifiers ? [`${notifiers} destination${notifiers === 1 ? "" : "s"}`, "ok"] : ["Not set up", ""];
  }
  if (key === "updates") {
    if (u?.update_available) return [`${u.latest.version} is out`, "warn"];
    return [u?.current ? `${u.current}${u.latest ? " · up to date" : ""}` : "Version and what’s new", ""];
  }
  return ["", ""];
}

// Signing in to a cloud service with a short code: AllDebrid's PIN, TorBox's device code.
function codeCardHTML(p, name, where, url) {
  return `<div class="pin-card" id="${p}-pin-card">
    <div class="pin-start" id="${p}-pin-start">
      <div><b id="${p}-pin-title"></b><p class="desc" id="${p}-pin-desc"></p></div>
      <div class="pin-actions">
        <button class="btn" type="button" data-${p}-test>Test</button>
        <button class="btn" type="button" id="${p}-pin">Connect ${name}</button>
      </div>
    </div>
    <div class="pin-wait" id="${p}-pin-wait" hidden>
      <span class="pin-code" id="${p}-pin-code" aria-label="Your code"></span>
      <div class="pin-steps">
        <b>Enter this code on ${name}</b>
        <p class="desc">Open the page below wherever you’re signed in to ${name}, even on your phone. This page carries on by itself once you have.</p>
        <div class="pin-actions">
          <a class="btn" id="${p}-pin-link" href="${url}" target="_blank" rel="noopener noreferrer">Open ${where}</a>
          <button class="btn ghost" type="button" id="${p}-pin-cancel">Cancel</button>
        </div>
        <small class="pin-status"><span class="spinner" aria-hidden="true"></span><span id="${p}-pin-left">Waiting for the code…</span></small>
      </div>
    </div>
  </div>`;
}

function Settings(root, page) {
  const s = state.settings || {};
  const tile = (client, label, where) => {
    const ready = s[`${SHORT[client] || client}_configured`];
    return `<button type="button" role="radio" data-v="${client}"><b>${label}</b><small>${ready ? `<i class="dot ok"></i>Ready` : where}</small></button>`;
  };
  root.innerHTML = `
    <div class="set-wrap" id="set-wrap">
      <a class="set-back" href="#/settings">${I.chevron}Settings</a>
      <header class="set-head">
        <div class="eyebrow">Settings</div>
        <h1 class="page-title"><span class="t-home">Settings</span><span class="t-page" id="set-title"></span></h1>
        <p class="lede" id="set-lede"></p>
      </header>
      <div class="set-layout">
        <nav class="set-nav" aria-label="Settings">
          ${SETTINGS_PAGES.map(([key, label, , icon]) => `
            <a href="#/settings/${key}" data-page="${key}">
              <span class="ic" aria-hidden="true">${icon}</span>
              <span class="tx"><b>${esc(label)}</b><small><i class="dot"></i><span data-status></span></small></span>
              <span class="chev" aria-hidden="true">${I.chevron}</span>
            </a>`).join("")}
        </nav>
    <form id="settings" class="settings" autocomplete="off">
      <section class="panel" data-page="general">
        <div class="ph"><h3>Library</h3><p class="desc">Finished books are filed here. Audiobookshelf can watch the same folder.</p></div>
        <div class="body">
          <div class="field">
            <label for="library_dir">Library folder</label>
            <input class="input mono" id="library_dir" name="library_dir" value="${esc(s.library_dir)}" />
          </div>
          <div class="field">
            <label for="folder_template">Folder layout</label>
            <div class="layout-presets" role="group" aria-label="Start from a layout">
              ${LAYOUT_PRESETS.map(([name, t]) => `<button type="button" class="fpill ${s.folder_template === t ? "on" : ""}" data-preset="${esc(t)}" aria-pressed="${s.folder_template === t}">${esc(name)}</button>`).join("")}
            </div>
            <input class="input mono" id="folder_template" name="folder_template" value="${esc(s.folder_template)}" />
            <div class="preview-path" id="path-preview"></div>
            <div class="layout-vars" role="group" aria-label="Add a variable">
              ${LAYOUT_VARS.map(([v, tip]) => `<button type="button" class="fill-in" data-var="${v}" title="${esc(tip)}">{${v}}</button>`).join("")}
            </div>
            <span class="hint">Put <code>/</code> between folders. If a book has no series or year, that part is left out, along with the words around it. Double braces keep braces in the name: <code>{{narrator}}</code> becomes <code>{Ray Porter}</code>, which Audiobookshelf reads as the narrator. Ebooks go into the same folder as their audiobook.</span>
          </div>
          <div class="set-row">
            <label for="max_concurrent"><b>Downloads at once</b><small>More finish sooner, but share your bandwidth.</small></label>
            <div class="stepper">
              <button type="button" data-step="-1" aria-label="Fewer">−</button>
              <input id="max_concurrent" name="max_concurrent" type="number" min="1" max="8" value="${esc(s.max_concurrent)}" />
              <button type="button" data-step="1" aria-label="More">+</button>
            </div>
          </div>
        </div>
      </section>

      <section class="panel" data-page="general">
        <div class="ph"><h3>Import folder</h3><p class="desc">Drop books here and they move into your library on their own, once they’ve finished copying. Audiobooks and ebooks both.</p></div>
        <div class="body">
          <div class="field">
            <label for="import_folder">Folder to watch</label>
            <input class="input mono" id="import_folder" name="import_folder" placeholder="e.g. ~/Downloads/Audiobooks to import" value="${esc(s.import_folder)}" />
            <span class="hint">Checked every minute. Must be outside your library folder. Leave it empty to turn this off.</span>
          </div>
          <div><button class="btn" type="button" id="open-import">${I.down} Import books now…</button></div>
        </div>
      </section>

      <section class="panel" data-page="notifications">
        <div class="ph"><h3>Destinations</h3><p class="desc">Where Narratarr tells you when someone adds a book to Wanted, and when downloads start, finish or fail. Every event is also written to Narratarr’s log (<code>docker logs</code>), whether or not anything is set up here.</p></div>
        <div class="body">
          <div class="notify-list" id="notify-list"><span class="spinner"></span></div>
          <div class="notify-editor" id="notify-editor" hidden></div>
          <div><button class="btn" type="button" id="add-notifier">${I.plus} Add a destination</button></div>
        </div>
      </section>
      <section class="panel" data-page="notifications" aria-labelledby="log-title">
        <div class="ph ph-row">
          <div><h3 id="log-title">Event log <span class="live-chip"><i aria-hidden="true"></i>Live</span></h3>
            <p class="desc">The same lines Narratarr writes to <code>docker logs</code>, newest first, with where each one was sent. Open a line to see exactly what happened.</p></div>
          <div class="ph-actions">
            <button class="btn sm" type="button" id="log-copy">${I.copy}Copy</button>
            <a class="btn sm" href="/api/settings/notifications/events.log" download>${I.down} Download .log</a>
          </div>
        </div>
        <div class="body">
          <div class="log-tools">
            <div class="log-filters" id="log-filters" role="radiogroup" aria-label="Show"></div>
            <label class="lib-find">${I.search}<input id="log-q" type="search" placeholder="Find a book or person" aria-label="Find a book or person in the event log" /></label>
          </div>
          <ol class="log-list" id="log-list" aria-label="Events"><li class="log-empty"><span class="spinner"></span></li></ol>
          <div class="log-foot"><span id="log-kept"></span><button class="btn sm" type="button" id="log-more" hidden>Show older</button></div>
        </div>
      </section>

      <section class="panel" data-page="users">
        <div class="ph"><h3>People</h3><p class="desc">Everyone signs in with their own account.</p></div>
        <div class="body">
          <div class="user-list" id="user-list"><span class="spinner"></span></div>
          <div><button class="btn" type="button" id="add-user">${I.plus} Add someone</button></div>
        </div>
      </section>
      <section class="panel" data-page="users">
        <div class="ph"><h3>Requests</h3></div>
        <div class="body">
          <div class="field">
            <label class="switch"><input type="checkbox" name="listener_approval" ${s.listener_approval ? "checked" : ""}><i></i>Approve what listeners ask for</label>
            <span class="hint">A listener’s request waits under Library → Awaiting approval until an admin approves it, and nothing is searched for or downloaded before then. If they picked a release, approving downloads exactly that one. While this is on, only admins follow authors and series. Turn it off to let listeners download straight away.</span>
          </div>
        </div>
      </section>
      <section class="panel" data-page="users">
        <div class="ph"><h3>Home network</h3></div>
        <div class="body">
          <div class="field">
            <label class="switch"><input type="checkbox" name="auth_local_bypass" ${s.auth_local_bypass ? "checked" : ""}><i></i>Don’t ask for a password on my home network</label>
            <span class="hint">Phones and computers on this network open Narratarr signed in as the first admin. Leave this off if Narratarr can be reached from the internet. Anything that comes through a reverse proxy always has to sign in.</span>
          </div>
        </div>
      </section>

      <section class="panel" data-page="updates">
        <div class="ph"><h3>Updates</h3><p class="desc">You have Narratarr <b>${esc(state.updates?.current || "")}</b>.${state.updates?.update_available ? ` Version <b>${esc(state.updates.latest.version)}</b> is out.` : state.updates?.latest ? " You’re up to date." : ""}</p></div>
        <div class="body">
          <div class="field">
            <label class="switch"><input type="checkbox" name="check_updates" ${s.check_updates ? "checked" : ""}><i></i>Check for new versions</label>
            <span class="hint">One small request a day to GitHub’s releases page. Nothing about you or your library is sent.</span>
          </div>
          <div class="cta-row" style="justify-content:flex-start">
            <button class="btn" type="button" id="check-updates">${I.retry} Check now</button>
            <button class="btn" type="button" id="whats-new">${I.list} What’s new</button>
            <a class="btn" href="${DISCORD_URL}" target="_blank" rel="noopener noreferrer">${I.chat} Join the Discord</a>
          </div>
          <div id="update-result"></div>
        </div>
      </section>

      <section class="panel" data-page="search">
        <div class="ph ph-row">
          <div><h3>Prowlarr</h3><p class="desc">Searches the indexers you’ve added to Prowlarr, for Discover and for your Wanted list. Results download through your clients under Downloads.</p></div>
          <span class="state-chip ok" id="prowlarr-chip" ${s.prowlarr_configured ? "" : "hidden"}><i class="dot ok"></i>Connected</span>
        </div>
        <div class="body">
          <div class="set-grid">
            <div class="field"><label for="prowlarr_url">Address</label><input class="input mono" id="prowlarr_url" name="prowlarr_url" placeholder="http://localhost:9696" value="${esc(s.prowlarr_url)}" /></div>
            <div class="field">
              <label for="prowlarr_api_key">API key</label>
              <div class="input-row">
                <input class="input mono" id="prowlarr_api_key" name="prowlarr_api_key" type="password" placeholder="Prowlarr → Settings → General" value="${esc(s.prowlarr_api_key)}" />
                <button class="btn" type="button" id="test-prowlarr">Test</button>
              </div>
            </div>
          </div>
          <div id="prowlarr-result"></div>
          <details class="set-more">
            <summary>Categories</summary>
            <div class="set-grid">
              <div class="field">
                <label for="prowlarr_categories">Audiobooks</label>
                <input class="input mono" id="prowlarr_categories" name="prowlarr_categories" value="${esc(s.prowlarr_categories)}" />
                <span class="hint"><code>3030</code> is Audio › Audiobook. Separate several with commas.</span>
              </div>
              <div class="field">
                <label for="prowlarr_ebook_categories">Ebooks</label>
                <input class="input mono" id="prowlarr_ebook_categories" name="prowlarr_ebook_categories" value="${esc(s.prowlarr_ebook_categories)}" />
                <span class="hint"><code>7020</code> is Books › EBook.</span>
              </div>
            </div>
          </details>
        </div>
      </section>

      <section class="panel" data-page="search">
        <div class="ph ph-row">
          <div><h3>Torznab</h3><p class="desc">Optional. Any indexer that speaks Torznab: <b>Jackett</b>, Prowlarr’s own Torznab endpoint, or a standalone indexer. Searched alongside Prowlarr, for Discover and for your Wanted list.</p></div>
          <span class="state-chip ok" id="torznab-chip" ${s.torznab_configured ? "" : "hidden"}><i class="dot ok"></i>Connected</span>
        </div>
        <div class="body">
          <div class="set-grid">
            <div class="field"><label for="torznab_url">Address</label><input class="input mono" id="torznab_url" name="torznab_url" placeholder="http://localhost:9117/api/v2.0/indexers/all/results/torznab/api" value="${esc(s.torznab_url)}" /><span class="hint">The full address ending in <code>/torznab/api</code>, not the Jackett dashboard.</span></div>
            <div class="field">
              <label for="torznab_api_key">API key</label>
              <div class="input-row">
                <input class="input mono" id="torznab_api_key" name="torznab_api_key" type="password" placeholder="Jackett → the key at the top of its dashboard" value="${esc(s.torznab_api_key)}" />
                <button class="btn" type="button" id="test-torznab">Test</button>
              </div>
            </div>
          </div>
          <div id="torznab-result"></div>
          <details class="set-more">
            <summary>Categories</summary>
            <div class="set-grid">
              <div class="field">
                <label for="torznab_categories">Audiobooks</label>
                <input class="input mono" id="torznab_categories" name="torznab_categories" value="${esc(s.torznab_categories)}" />
                <span class="hint"><code>3030</code> is Audio › Audiobook. Separate several with commas.</span>
              </div>
              <div class="field">
                <label for="torznab_ebook_categories">Ebooks</label>
                <input class="input mono" id="torznab_ebook_categories" name="torznab_ebook_categories" value="${esc(s.torznab_ebook_categories)}" />
                <span class="hint"><code>7020</code> is Books › EBook.</span>
              </div>
            </div>
          </details>
        </div>
      </section>

      <section class="panel" data-page="downloads">
        <div class="ph"><h3>Torrents</h3><p class="desc">Real-Debrid, AllDebrid, TorBox and Premiumize fetch them for you, so nothing is shared from your computer. qBittorrent, Transmission and Deluge download on your own machine and keep seeding, which shows your IP address to other peers; many people run them behind a VPN.</p></div>
        <div class="body">
          <div class="tile-groups" data-choice="torrent_client" role="radiogroup" aria-label="Download torrents with">
            <div class="tile-group"><span class="tile-label" aria-hidden="true">In the cloud</span>
              <div class="choice-tiles">${tile("realdebrid", "Real-Debrid", "Premium account")}${tile("alldebrid", "AllDebrid", "Premium account")}${tile("torbox", "TorBox", "TorBox account")}${tile("premiumize", "Premiumize", "Premium account")}</div></div>
            <div class="tile-group"><span class="tile-label" aria-hidden="true">On this machine</span>
              <div class="choice-tiles">${tile("qbittorrent", "qBittorrent", "Seeds from here")}${tile("transmission", "Transmission", "Seeds from here")}${tile("deluge", "Deluge", "Seeds from here")}</div></div>
          </div>
          <input type="hidden" name="torrent_client" value="${esc(s.torrent_client)}" />
          <div class="client-fields" data-for="torrent_client:realdebrid">
            <div class="field">
              <label for="rd_api_key">Real-Debrid API key</label>
              <div class="input-row">
                <input class="input mono" id="rd_api_key" name="rd_api_key" type="password" placeholder="Paste your private API token" value="${esc(s.rd_api_key)}" />
                <button class="btn" type="button" id="test-rd">Test</button>
              </div>
              <span class="hint">Find it at <a href="https://real-debrid.com/apitoken" target="_blank" rel="noopener" style="color:var(--accent)">real-debrid.com/apitoken</a>.</span>
            </div>
            <div id="rd-result"></div>
            <label class="switch"><input type="checkbox" name="delete_from_rd_after" ${s.delete_from_rd_after ? "checked" : ""}><i></i>Remove torrents from Real-Debrid after downloading</label>
          </div>
          <div class="client-fields" data-for="torrent_client:alldebrid">
            ${codeCardHTML("ad", "AllDebrid", "alldebrid.com/pin", "https://alldebrid.com/pin/")}
            <div id="ad-result"></div>
            <details class="set-more">
              <summary>Use an API key instead</summary>
              <div class="field">
                <label for="ad_api_key">AllDebrid API key</label>
                <div class="input-row">
                  <input class="input mono" id="ad_api_key" name="ad_api_key" type="password" placeholder="Paste an API key" value="${esc(s.ad_api_key)}" />
                  <button class="btn" type="button" data-ad-test>Test</button>
                </div>
                <span class="hint">Create one at <a href="https://alldebrid.com/apikeys" target="_blank" rel="noopener" style="color:var(--accent)">alldebrid.com/apikeys</a>.</span>
              </div>
            </details>
            <label class="switch"><input type="checkbox" name="delete_from_ad_after" ${s.delete_from_ad_after ? "checked" : ""}><i></i>Remove torrents from AllDebrid after downloading</label>
          </div>
          <div class="client-fields" data-for="torrent_client:torbox">
            ${codeCardHTML("tb", "TorBox", "TorBox’s sign-in page", "https://torbox.app/")}
            <div id="tb-result"></div>
            <details class="set-more">
              <summary>Use an API key instead</summary>
              <div class="field">
                <label for="tb_api_key">TorBox API key</label>
                <div class="input-row">
                  <input class="input mono" id="tb_api_key" name="tb_api_key" type="password" placeholder="Paste an API key" value="${esc(s.tb_api_key)}" />
                  <button class="btn" type="button" data-tb-test>Test</button>
                </div>
                <span class="hint">Find it in TorBox under <a href="https://torbox.app/settings" target="_blank" rel="noopener" style="color:var(--accent)">Settings → API key</a>.</span>
              </div>
            </details>
            <label class="switch"><input type="checkbox" name="delete_from_tb_after" ${s.delete_from_tb_after ? "checked" : ""}><i></i>Remove torrents from TorBox after downloading</label>
          </div>
          <div class="client-fields" data-for="torrent_client:premiumize">
            <div class="field">
              <label for="pm_api_key">Premiumize API key</label>
              <div class="input-row">
                <input class="input mono" id="pm_api_key" name="pm_api_key" type="password" placeholder="Paste your API key" value="${esc(s.pm_api_key)}" />
                <button class="btn" type="button" id="test-pm">Test</button>
              </div>
              <span class="hint">Find it at <a href="https://www.premiumize.me/account" target="_blank" rel="noopener" style="color:var(--accent)">premiumize.me/account</a>.</span>
            </div>
            <div id="pm-result"></div>
            <label class="switch"><input type="checkbox" name="delete_from_pm_after" ${s.delete_from_pm_after ? "checked" : ""}><i></i>Remove torrents and their files from Premiumize after downloading</label>
          </div>
          <div class="client-fields" data-for="torrent_client:qbittorrent">
            <div class="field">
              <label for="qbittorrent_url">qBittorrent Web UI address</label>
              <div class="input-row">
                <input class="input mono" id="qbittorrent_url" name="qbittorrent_url" placeholder="http://localhost:8080" value="${esc(s.qbittorrent_url)}" />
                <button class="btn" type="button" data-test-client="qbittorrent">Test</button>
              </div>
              <div data-result="qbittorrent"></div>
            </div>
            <div class="set-grid three">
              <div class="field"><label for="qbittorrent_username">Username</label><input class="input mono" id="qbittorrent_username" name="qbittorrent_username" value="${esc(s.qbittorrent_username)}" autocomplete="off" /></div>
              <div class="field"><label for="qbittorrent_password">Password</label><input class="input mono" id="qbittorrent_password" name="qbittorrent_password" type="password" value="${esc(s.qbittorrent_password)}" /></div>
              <div class="field"><label for="qbittorrent_category">Category</label><input class="input mono" id="qbittorrent_category" name="qbittorrent_category" value="${esc(s.qbittorrent_category)}" /></div>
            </div>
            <div class="field">
              <label for="qbittorrent_folder">Download folder</label>
              <input class="input mono" id="qbittorrent_folder" name="qbittorrent_folder" placeholder="qBittorrent’s default" value="${esc(s.qbittorrent_folder)}" />
              <span class="hint">Optional. Where qBittorrent saves Narratarr’s books, as qBittorrent sees it, e.g. <code>/data/torrents/books</code>. Keeps them apart from your other downloads.</span>
            </div>
          </div>
          <div class="client-fields" data-for="torrent_client:transmission">
            <div class="field">
              <label for="transmission_url">Transmission address</label>
              <div class="input-row">
                <input class="input mono" id="transmission_url" name="transmission_url" placeholder="http://localhost:9091" value="${esc(s.transmission_url)}" />
                <button class="btn" type="button" data-test-client="transmission">Test</button>
              </div>
              <div data-result="transmission"></div>
            </div>
            <div class="set-grid">
              <div class="field"><label for="transmission_username">Username</label><input class="input mono" id="transmission_username" name="transmission_username" value="${esc(s.transmission_username)}" autocomplete="off" placeholder="If you set one" /></div>
              <div class="field"><label for="transmission_password">Password</label><input class="input mono" id="transmission_password" name="transmission_password" type="password" value="${esc(s.transmission_password)}" /></div>
            </div>
            <div class="field">
              <label for="transmission_label">Label</label>
              <input class="input mono" id="transmission_label" name="transmission_label" value="${esc(s.transmission_label)}" />
              <span class="hint">Optional. Added to every torrent Narratarr sends, so you can filter them in Transmission 3.0 or later.</span>
            </div>
            <div class="field">
              <label for="transmission_folder">Download folder</label>
              <input class="input mono" id="transmission_folder" name="transmission_folder" placeholder="Transmission’s default" value="${esc(s.transmission_folder)}" />
              <span class="hint">Optional. Where Transmission saves Narratarr’s books, as Transmission sees it, e.g. <code>/data/torrents/books</code>. Keeps them apart from your other downloads.</span>
            </div>
          </div>
          <div class="client-fields" data-for="torrent_client:deluge">
            <div class="field">
              <label for="deluge_url">Deluge Web UI address</label>
              <div class="input-row">
                <input class="input mono" id="deluge_url" name="deluge_url" placeholder="http://localhost:8112" value="${esc(s.deluge_url)}" />
                <button class="btn" type="button" data-test-client="deluge">Test</button>
              </div>
              <div data-result="deluge"></div>
            </div>
            <div class="field">
              <label for="deluge_password">Password</label>
              <input class="input mono" id="deluge_password" name="deluge_password" type="password" value="${esc(s.deluge_password)}" />
              <span class="hint">Deluge’s web password (it has no username). Set it under Preferences → Interface → Password.</span>
            </div>
            <div class="field">
              <label for="deluge_folder">Download folder</label>
              <input class="input mono" id="deluge_folder" name="deluge_folder" placeholder="Deluge’s default" value="${esc(s.deluge_folder)}" />
              <span class="hint">Optional. Where Deluge saves Narratarr’s books, as Deluge sees it, e.g. <code>/data/torrents/books</code>. Keeps them apart from your other downloads.</span>
            </div>
          </div>
        </div>
      </section>

      <section class="panel" data-page="downloads">
        <div class="ph ph-row">
          <div><h3>Usenet</h3><p class="desc">For the Usenet results Prowlarr finds.</p></div>
          <div class="segmented" data-choice="usenet_client" role="radiogroup" aria-label="Download Usenet releases with">
            <button type="button" role="radio" data-v="sabnzbd">SABnzbd</button>
            <button type="button" role="radio" data-v="nzbget">NZBGet</button>
          </div>
        </div>
        <div class="body">
          <input type="hidden" name="usenet_client" value="${esc(s.usenet_client)}" />
          <div class="client-fields" data-for="usenet_client:sabnzbd">
            <div class="set-grid">
              <div class="field"><label for="sabnzbd_url">SABnzbd address</label><input class="input mono" id="sabnzbd_url" name="sabnzbd_url" placeholder="http://localhost:8080" value="${esc(s.sabnzbd_url)}" /></div>
              <div class="field">
                <label for="sabnzbd_api_key">API key</label>
                <div class="input-row">
                  <input class="input mono" id="sabnzbd_api_key" name="sabnzbd_api_key" type="password" placeholder="Config → General → API Key" value="${esc(s.sabnzbd_api_key)}" />
                  <button class="btn" type="button" data-test-client="sabnzbd">Test</button>
                </div>
              </div>
            </div>
            <div data-result="sabnzbd"></div>
            <div class="field">
              <label for="sabnzbd_category">Category</label>
              <input class="input mono" id="sabnzbd_category" name="sabnzbd_category" placeholder="audiobooks" value="${esc(s.sabnzbd_category)}" style="max-width:240px" />
              <span class="hint">Optional. Must already exist in SABnzbd. Leave it empty to use its default.</span>
            </div>
          </div>
          <div class="client-fields" data-for="usenet_client:nzbget">
            <div class="field">
              <label for="nzbget_url">NZBGet address</label>
              <div class="input-row">
                <input class="input mono" id="nzbget_url" name="nzbget_url" placeholder="http://localhost:6789" value="${esc(s.nzbget_url)}" />
                <button class="btn" type="button" data-test-client="nzbget">Test</button>
              </div>
              <div data-result="nzbget"></div>
            </div>
            <div class="set-grid three">
              <div class="field"><label for="nzbget_username">Username</label><input class="input mono" id="nzbget_username" name="nzbget_username" value="${esc(s.nzbget_username)}" placeholder="nzbget" autocomplete="off" /></div>
              <div class="field"><label for="nzbget_password">Password</label><input class="input mono" id="nzbget_password" name="nzbget_password" type="password" value="${esc(s.nzbget_password)}" /></div>
              <div class="field"><label for="nzbget_category">Category</label><input class="input mono" id="nzbget_category" name="nzbget_category" placeholder="Audiobooks" value="${esc(s.nzbget_category)}" /></div>
            </div>
            <span class="hint">The username and password are NZBGet’s ControlUsername and ControlPassword (Settings → Security).</span>
          </div>
        </div>
      </section>

      <section class="panel" data-page="downloads">
        <div class="ph"><h3>Download folders</h3><p class="desc">Only needed when your download clients see the downloads folder at a different path, such as in separate Docker containers.</p></div>
        <div class="body">
          <div class="path-map">
            <div class="field"><label for="path_remote">Your client saves to</label><input class="input mono" id="path_remote" name="path_remote" placeholder="e.g. /downloads" value="${esc(s.path_remote)}" /></div>
            <span class="arrow" aria-hidden="true">${I.chevron}</span>
            <div class="field"><label for="path_local">Narratarr sees it at</label><input class="input mono" id="path_local" name="path_local" placeholder="e.g. /data/downloads" value="${esc(s.path_local)}" /></div>
          </div>
        </div>
      </section>

      <section class="panel" data-page="search">
        <div class="ph"><h3>Languages</h3><p class="desc">The languages your books should be in. Leave them all off for any language.</p></div>
        <div class="body">
          <div class="field">
          <input type="hidden" name="languages" id="languages" value="${esc(s.languages)}" />
          <div class="lang-chips" id="lang-chips" role="group" aria-label="Languages">
            ${[...new Set([...LANGUAGES, ...(s.languages ? s.languages.split(/,\s*/) : [])])].map((l) => {
              const on = (s.languages || "").split(/,\s*/).includes(l);
              return `<button type="button" class="fpill ${on ? "on" : ""}" data-lang="${esc(l)}" aria-pressed="${on}">${esc(l)}</button>`;
            }).join("")}
          </div>
          <span class="hint">Discover hides books in other languages (one click shows them), and automatic search won’t grab them. A book whose language Narratarr can’t tell is never hidden. For the Audible catalog, also choose the Audible store in that language under Wanted &amp; following.</span>
          </div>
        </div>
      </section>

      <section class="panel" data-page="search">
        <div class="ph"><h3>Wanted &amp; following</h3><p class="desc">Books you want but don’t have yet, and new books from authors and series you follow.</p></div>
        <div class="body">
          <div class="set-row">
            <label class="switch"><input type="checkbox" name="auto_search" ${s.auto_search ? "checked" : ""}><i></i><span><b>Look for Wanted books by itself</b><small>A few at a time, to stay within your indexers’ limits.</small></span></label>
            <div class="inline-field">
              <label for="auto_search_hours">every</label>
              <input class="input" id="auto_search_hours" name="auto_search_hours" type="number" min="1" max="168" value="${esc(s.auto_search_hours)}" />
              <span>hours</span>
            </div>
          </div>
          <div class="set-row">
            <label for="audible_region"><b>Audible store for book details</b><small>Covers, series and release dates come from here. Nothing is downloaded from Audible.</small></label>
            <select class="select" id="audible_region" name="audible_region">
              ${[["us", "United States"], ["uk", "United Kingdom"], ["ca", "Canada"], ["au", "Australia"], ["in", "India"], ["de", "Germany"], ["fr", "France"], ["it", "Italy"], ["es", "Spain"], ["jp", "Japan"]]
                .map(([v, l]) => `<option value="${v}" ${s.audible_region === v ? "selected" : ""}>${l}</option>`).join("")}
            </select>
          </div>
        </div>
      </section>

      <section class="panel" data-page="search">
        <div class="ph"><h3>Rating metadata</h3><p class="desc">The star pill on every Discover card. Ratings are read from a local cache, never fetched while a page loads: a book with no rating yet is filled in the background and appears on your next search.</p></div>
        <div class="body">
          <div class="set-row">
            <label for="ratings_provider"><b>Ratings provider</b><small>A book is only scored when its title and author both match exactly; a near miss shows <b>No Rating</b> rather than the wrong score.</small></label>
            <select class="select" id="ratings_provider" name="ratings_provider">
              ${[["audnex", "Audnex (Audible)"], ["bookmarks", "Bookmarks"], ["goodreads", "Goodreads (unofficial)"], ["", "Off"]]
                .map(([v, l]) => `<option value="${v}" ${(s.ratings_provider ?? "audnex") === v ? "selected" : ""}>${l}</option>`).join("")}
            </select>
          </div>
          <div class="set-row">
            <label><b>Audible storefronts</b><small>Ratings only exist in the regions where a title is sold, so each storefront is tried in turn and the first that has the book wins. More regions means more coverage and more requests. The one that answered is remembered for that book.</small></label>
            <div id="audnex-regions"></div>
            <input type="hidden" name="audnex_regions" value="${esc((s.audnex_regions || ["uk"]).join(","))}" />
            <div><button class="btn" type="button" id="add-region">Add a region</button></div>
          </div>
          <div class="set-row">
            <label><b>Goodreads</b><small>Unofficial: their terms forbid collecting listings with extraction tools, so enabling it is your own call, not something Narratarr recommends.</small></label>
          </div>
        </div>
      </section>

      <section class="panel" data-page="search">
        <div class="ph"><h3>AudiobookBay</h3><p class="desc">The AudiobookBay tab on Discover. Change the address if the site moves.</p></div>
        <div class="body">
          <div class="field">
            <label for="abb_base_url">Address</label>
            <input class="input mono" id="abb_base_url" name="abb_base_url" value="${esc(s.abb_base_url)}" />
          </div>
          <div class="field">
            <label>Search with</label>
            <div class="segmented" id="provider" role="radiogroup" aria-label="Search provider">
              <button type="button" role="radio" data-v="audiobookbay">AudiobookBay’s own search</button>
              <button type="button" role="radio" data-v="brave">Brave Search</button>
            </div>
            <input type="hidden" name="search_provider" value="${esc(s.search_provider)}" />
            <span class="hint" id="provider-hint"></span>
          </div>
          <div class="field" id="brave-field">
            <label for="brave_api_key">Brave Search API key</label>
            <div class="input-row">
              <input class="input mono" id="brave_api_key" name="brave_api_key" type="password" placeholder="Paste your Brave Search API key" value="${esc(s.brave_api_key)}" />
              <button class="btn" type="button" id="test-brave">Test</button>
            </div>
            <span class="hint">Get one at <a href="https://api-dashboard.search.brave.com" target="_blank" rel="noopener" style="color:var(--accent)">api-dashboard.search.brave.com</a>. Each search uses one query from your Brave plan.</span>
            <div id="brave-result"></div>
          </div>
        </div>
      </section>

      <section class="panel" data-page="search">
        <div class="ph"><h3>Smart search</h3></div>
        <div class="body">
          <div class="field">
            <label class="switch"><input type="checkbox" name="smart_search" ${s.smart_search ? "checked" : ""}><i></i>Fix typos and suggest titles as you type</label>
            <span class="hint">Uses Apple’s public audiobook catalog, so what you type in the search box is sent to Apple.</span>
          </div>
        </div>
      </section>

      <section class="panel" data-page="abs">
        <div class="ph abs-connected-head" id="abs-head" hidden>
          <span class="abs-badge" aria-hidden="true">${SETTINGS_PAGE.abs[3]}</span>
          <div class="abs-who"><h3>Connected</h3><p class="desc">To <code id="abs-where"></code></p></div>
          <button class="btn" type="button" id="abs-disconnect">Disconnect</button>
        </div>
        <div class="ph" id="abs-intro"><h3>Connect Audiobookshelf</h3><p class="desc">Books are already filed the way Audiobookshelf expects. Connect it and Audiobookshelf picks up each new book as soon as it has downloaded.</p></div>
        <div class="body">
          <div class="field" id="abs-connected" hidden>
            <div class="set-row">
              <label for="audiobookshelf_library_id"><b>Library to rescan</b><small>After every download. It should watch Narratarr’s library folder.</small></label>
              <div class="input-row">
                <select class="select abs-select" id="audiobookshelf_library_id" name="audiobookshelf_library_id">
                  <option value="${esc(s.audiobookshelf_library_id)}">${s.audiobookshelf_library_id ? "Loading…" : "Choose a library"}</option>
                </select>
                <button class="btn" type="button" id="test-audiobookshelf">Test</button>
              </div>
            </div>
            <div id="audiobookshelf-result"></div>
          </div>

          <div class="field" id="abs-setup">
            <label for="audiobookshelf_url">Server</label>
            <div class="input-row">
              <input class="input mono" id="audiobookshelf_url" name="audiobookshelf_url" placeholder="Empty to search, or an IP or domain" value="${esc(s.audiobookshelf_url)}" />
              <button class="btn" type="button" id="abs-find">${I.search} Find</button>
            </div>
            <span class="hint">Empty: looks on this computer, in Docker, then across your home network. With an address (a LAN or public IP, or a domain): finds Audiobookshelf there, trying https first.</span>
            <div id="abs-found"></div>
          </div>

          <div class="field" id="abs-signin" hidden>
            <label for="abs-user">Sign in to Audiobookshelf</label>
            <div class="input-row two">
              <input class="input" id="abs-user" placeholder="Admin username" autocomplete="username" autocapitalize="off" spellcheck="false" />
              <input class="input" id="abs-pass" type="password" placeholder="Password" autocomplete="current-password" />
            </div>
            <label class="switch" id="abs-insecure" hidden><input type="checkbox" id="abs-insecure-ok"><i></i>This address is on the internet without https, so the password could be read on the way. Send it anyway.</label>
            <div><button class="btn primary" type="button" id="abs-sign-in">${I.key} Connect</button></div>
            <span class="hint">Used once, to create an API key called “Narratarr” in Audiobookshelf. Your password isn’t saved, and you can revoke the key in Audiobookshelf under Settings → Users → API Keys.</span>
            <div id="abs-signin-result"></div>
          </div>

          <details class="abs-manual set-more" id="abs-manual">
            <summary>Paste an API key instead</summary>
            <div class="field">
              <label for="audiobookshelf_token">API key</label>
              <input class="input mono" id="audiobookshelf_token" name="audiobookshelf_token" type="password" value="${esc(s.audiobookshelf_token)}" />
              <span class="hint">For single sign-on (OpenID) accounts, or older Audiobookshelf versions. Create one in Audiobookshelf under Settings → Users → API Keys, then save.</span>
            </div>
          </details>
        </div>
      </section>

      <section class="panel" data-page="abs" id="abs-check-panel" hidden>
        <div class="ph ph-row">
          <div><h3>Can it see your books?</h3><p class="desc">Asks Audiobookshelf to rescan, then counts which of Narratarr’s books it found.</p></div>
          <button class="btn primary" type="button" id="abs-check">Check now</button>
        </div>
        <div class="body" id="abs-check-result" hidden></div>
      </section>

      <button class="panel guide-card" type="button" data-page="abs" data-guide>
        <span class="ic" aria-hidden="true">${I.book}</span>
        <span class="tx"><b>Setup guide</b><small>Step by step, for Docker or a direct install, on this machine or another.</small></span>
        <span class="chev" aria-hidden="true">${I.chevron}</span>
      </button>
    </form>
      </div>
    </div>
    <div class="save-float" id="save-bar" role="status" hidden>
      <i class="dot warn" aria-hidden="true"></i>
      <span id="save-count"></span>
      <button class="btn ghost" type="button" id="discard">Discard</button>
      <button class="btn primary" type="submit" form="settings" id="save">Save<span class="long"> changes</span></button>
    </div>`;

  const form = $("#settings", root);
  const wrap = $("#set-wrap", root);
  let userCount = null;
  let notifyCount = null;
  let saved = null; // the settings as last saved; the save bar shows when the form differs

  // Which page shows. No page: on a phone the menu itself; on a wide screen, General.
  function go(p) {
    const key = SETTINGS_PAGE[p] ? p : "";
    const shown = key || "general";
    const [, label, lede] = SETTINGS_PAGE[shown];
    wrap.classList.toggle("home", !key);
    $("#set-title", root).textContent = label;
    $("#set-lede", root).textContent = lede;
    $$("[data-page]", form).forEach((el) => { el.hidden = el.dataset.page !== shown || (el.id === "abs-check-panel" && !absConnected()); });
    $$(".set-nav a", root).forEach((a) => {
      const on = a.dataset.page === shown;
      a.classList.toggle("on", on);
      a.setAttribute("aria-current", on ? "page" : "false");
    });
    page = key;
  }
  function statuses() {
    $$(".set-nav a", root).forEach((a) => {
      const [text, tone] = settingsStatus(a.dataset.page, userCount, notifyCount);
      $("[data-status]", a).textContent = text;
      $(".dot", a).className = `dot ${tone}`;
    });
  }

  const preview = () => {
    const dir = form.library_dir.value.replace(/\/+$/, "");
    const template = form.folder_template.value || "{author}/{title}";
    const row = (label, book) => `<div><span>${label}</span>${esc(dir)}/<b>${esc(layoutFolders(template, book).join("/"))}</b></div>`;
    const usesSeries = /\{(series|series_number|year)\}/.test(template);
    $("#path-preview", root).innerHTML = usesSeries
      ? row("In a series", LAYOUT_SAMPLE) + row("On its own", LAYOUT_SAMPLE_SOLO)
      : row("", LAYOUT_SAMPLE);
  };
  $$(".layout-vars [data-var]", root).forEach((b) => b.addEventListener("click", () => {
    const input = form.folder_template;
    const at = input.selectionStart ?? input.value.length;
    const end = input.selectionEnd ?? at;
    const text = `{${b.dataset.var}}`;
    input.value = input.value.slice(0, at) + text + input.value.slice(end);
    input.focus();
    input.setSelectionRange(at + text.length, at + text.length);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }));
  $("#lang-chips", root).onclick = (e) => {
    const b = e.target.closest("[data-lang]");
    if (!b) return;
    const on = b.getAttribute("aria-pressed") !== "true";
    b.classList.toggle("on", on);
    b.setAttribute("aria-pressed", String(on));
    form.languages.value = $$("[data-lang][aria-pressed=true]", root).map((x) => x.dataset.lang).join(", ");
    form.languages.dispatchEvent(new Event("input", { bubbles: true }));
  };

  const presetsBox = $(".layout-presets", root);
  const markPreset = () => $$("[data-preset]", presetsBox).forEach((b) => {
    const on = b.dataset.preset === form.folder_template.value.trim();
    b.classList.toggle("on", on);
    b.setAttribute("aria-pressed", String(on));
  });
  presetsBox.onclick = (e) => {
    const b = e.target.closest("[data-preset]");
    if (!b) return;
    form.folder_template.value = b.dataset.preset;
    form.folder_template.dispatchEvent(new Event("input", { bubbles: true }));
  };
  form.folder_template.oninput = () => { preview(); markPreset(); };
  form.library_dir.oninput = preview;
  preview();

  const hints = {
    audiobookbay: "Searches the site directly. Free, but needs exact spelling (smart search fixes that) and stops working when the site is down.",
    brave: "Searches Brave’s copy of the site. Faster, fixes spelling on its own, and keeps working when AudiobookBay is slow. Very new uploads may not be indexed yet.",
  };
  const setProvider = (v) => {
    form.search_provider.value = v;
    $$("#provider button", root).forEach((b) => {
      b.classList.toggle("on", b.dataset.v === v);
      b.setAttribute("aria-checked", b.dataset.v === v);
    });
    $("#provider-hint", root).textContent = hints[v];
    $("#brave-field", root).hidden = v !== "brave";
    dirty();
  };
  setProvider(s.search_provider || "audiobookbay");
  $("#provider", root).onclick = (e) => { const b = e.target.closest("[data-v]"); if (b) setProvider(b.dataset.v); };

  // Client pickers: show the fields for the chosen client only.
  const choose = (group, v) => {
    form[group].value = v;
    $$(`[data-choice="${group}"] button`, root).forEach((b) => {
      b.classList.toggle("on", b.dataset.v === v);
      b.setAttribute("aria-checked", b.dataset.v === v);
    });
    $$(`[data-for^="${group}:"]`, root).forEach((el) => { el.hidden = el.dataset.for !== `${group}:${v}`; });
    dirty();
  };
  $$("[data-choice]", root).forEach((seg) => {
    choose(seg.dataset.choice, form[seg.dataset.choice].value);
    seg.onclick = (e) => { const b = e.target.closest("[data-v]"); if (b) choose(seg.dataset.choice, b.dataset.v); };
  });

  // Audible storefronts: an ordered list, because availability is per item and the order is the
  // search order. Up/down move a row, Test asks that storefront about one known book.
  const CODES = ["us", "uk", "ca", "au", "in", "de", "fr", "it", "es", "jp"];
  const NAMES = { us: "United States", uk: "United Kingdom", ca: "Canada", au: "Australia",
    in: "India", de: "Germany", fr: "France", it: "Italy", es: "Spain", jp: "Japan" };
  const regionBox = $("#audnex-regions", root);
  const loadRegions = () => (String(form.audnex_regions.value || "uk").split(",").map((r) => r.trim()).filter(Boolean));
  const storeRegions = (list) => { form.audnex_regions.value = list.join(","); dirty(); };

  const drawRegions = () => {
    const list = loadRegions();
    regionBox.innerHTML = list.map((code, i) => `
      <div class="region-row" data-i="${i}" style="display:flex;align-items:center;gap:6px;margin:4px 0">
        <select class="select" data-region style="max-width:220px" aria-label="Storefront for row ${i + 1}">
          ${CODES.map((c) => `<option value="${c}" ${c === code ? "selected" : ""}>${esc(NAMES[c])} (${c})</option>`).join("")}
        </select>
        <button class="btn" type="button" data-act="up" ${i === 0 ? "disabled" : ""} aria-label="Move up">↑</button>
        <button class="btn" type="button" data-act="down" ${i === list.length - 1 ? "disabled" : ""} aria-label="Move down">↓</button>
        <button class="btn" type="button" data-act="test">Test</button>
        <button class="btn" type="button" data-act="del" aria-label="Remove">✕</button>
        <span class="hint" data-out></span>
      </div>`).join("");
  };

  regionBox.onclick = async (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const row = btn.closest("[data-i]");
    const i = Number(row.dataset.i);
    const list = loadRegions();
    const act = btn.dataset.act;
    if (act === "del") { list.splice(i, 1); storeRegions(list.length ? list : ["uk"]); drawRegions(); return; }
    if (act === "up" && i > 0) { [list[i - 1], list[i]] = [list[i], list[i - 1]]; storeRegions(list); drawRegions(); return; }
    if (act === "down" && i < list.length - 1) { [list[i + 1], list[i]] = [list[i], list[i + 1]]; storeRegions(list); drawRegions(); return; }
    if (act === "test") {
      const out = $("[data-out]", row);
      spinBtn(btn, true);
      try {
        const got = await api("/api/settings/test-audnex-region", { method: "POST", body: { region: list[i] } });
        out.innerHTML = got.ok
          ? `<div class="rd-result"><span>${got.rating ? `<b>${esc(got.rating)}</b> average` : "no ratings yet"}</span></div>`
          : `<div class="rd-result bad">${got.reason === "region unavailable"
              ? "Region unavailable, this book isn&#39;t sold here" : esc(got.reason)}</div>`;
      } catch (err) {
        out.innerHTML = `<div class="rd-result bad">${esc(err.message)}</div>`;
      }
      spinBtn(btn, false);
    }
  };

  regionBox.onchange = (e) => {
    const sel = e.target.closest("[data-region]");
    if (!sel) return;
    const list = loadRegions();
    const i = Number(sel.closest("[data-i]").dataset.i);
    if (list.includes(sel.value)) { sel.value = list[i]; return; }  // no duplicates: it is an order
    list[i] = sel.value;
    storeRegions(list);
    drawRegions();
  };

  $("#add-region", root).onclick = () => {
    const list = loadRegions();
    const spare = CODES.find((c) => !list.includes(c));
    if (!spare) return;
    storeRegions([...list, spare]);
    drawRegions();
  };
  drawRegions();
  $("#open-import", root).onclick = async () => { await save(true); openImport(form.import_folder.value); };
  $("#whats-new", root).onclick = () => openWhatsNew();
  $("#check-updates", root).onclick = async (e) => {
    spinBtn(e.currentTarget, true);
    const u = await checkUpdates(true);
    const out = $("#update-result", root);
    out.innerHTML = !u?.latest ? `<div class="rd-result bad">Couldn’t reach GitHub to check.</div>`
      : u.update_available ? okLine(`Narratarr <b>${esc(u.latest.version)}</b> is out · <a href="#" id="see-new" style="color:var(--accent)">see what’s new</a>`)
      : okLine(`You have the latest version, <b>${esc(u.current)}</b>`);
    $("#see-new", out)?.addEventListener("click", (ev) => { ev.preventDefault(); openWhatsNew(); });
    spinBtn(e.currentTarget, false);
  };

  const collect = () => {
    const data = Object.fromEntries(new FormData(form));
    data.delete_from_rd_after = form.delete_from_rd_after.checked;
    data.delete_from_ad_after = form.delete_from_ad_after.checked;
    data.delete_from_tb_after = form.delete_from_tb_after.checked;
    data.delete_from_pm_after = form.delete_from_pm_after.checked;
    data.smart_search = form.smart_search.checked;
    data.auto_search = form.auto_search.checked;
    data.check_updates = form.check_updates.checked;
    data.auth_local_bypass = form.auth_local_bypass.checked;
    data.listener_approval = form.listener_approval.checked;
    data.max_concurrent = Number(data.max_concurrent) || 1;
    data.auto_search_hours = Number(data.auto_search_hours) || 12;
    // A bare address ("abs.example.com") is something to Find, not a setting to save yet.
    if (data.audiobookshelf_url && !/^https?:\/\//.test(data.audiobookshelf_url.trim())) delete data.audiobookshelf_url;
    return data;
  };

  async function save(quiet) {
    state.settings = await api("/api/settings", { method: "PUT", body: collect() });
    form.rd_api_key.value = state.settings.rd_api_key;
    form.ad_api_key.value = state.settings.ad_api_key;
    form.tb_api_key.value = state.settings.tb_api_key;
    form.pm_api_key.value = state.settings.pm_api_key;
    form.audiobookshelf_token.value = state.settings.audiobookshelf_token;
    form.brave_api_key.value = state.settings.brave_api_key;
    for (const key of ["prowlarr_api_key", "torznab_api_key", "sabnzbd_api_key", "nzbget_password", "qbittorrent_password", "transmission_password", "deluge_password"]) {
      form[key].value = state.settings[key];
    }
    state.sources = await api("/api/sources"); // the Prowlarr tab appears once it's set up
    updateRdPill();
    if (absConnected() === $("#abs-connected", root).hidden) absShow(); // a pasted API key just connected it
    $("#prowlarr-chip", root).hidden = !state.settings.prowlarr_configured;
    $("#torznab-chip", root).hidden = !state.settings.torznab_configured;
    saved = snapshot();
    dirty();
    statuses();
    if (!quiet) toast("Settings saved", { kind: "ok" });
  }

  // The save bar: shown only when something differs from what's saved.
  const snapshot = () => collect();
  function dirty() {
    if (!saved) return;
    const now = collect();
    const n = [...new Set([...Object.keys(now), ...Object.keys(saved)])]
      .filter((k) => JSON.stringify(now[k]) !== JSON.stringify(saved[k])).length;
    $("#save-bar", root).hidden = !n;
    $("#save-count", root).textContent = `${n} unsaved change${n === 1 ? "" : "s"}`;
  }
  form.addEventListener("input", dirty);
  form.addEventListener("change", dirty);
  $("#discard", root).onclick = () => { state.view = Settings(root, page); };
  $$(".stepper [data-step]", form).forEach((b) => {
    b.onclick = () => {
      const input = form.max_concurrent;
      input.value = Math.max(1, Math.min(8, (Number(input.value) || 1) + Number(b.dataset.step)));
      dirty();
    };
  });

  form.onsubmit = async (e) => {
    e.preventDefault();
    const btn = $("#save", root);
    spinBtn(btn, true);
    try { await save(); } catch (err) { toast(err.message, { kind: "err" }); }
    spinBtn(btn, false);
  };

  $("#test-brave", root).onclick = async (e) => {
    const btn = e.currentTarget;
    const out = $("#brave-result", root);
    spinBtn(btn, true);
    try {
      await save(true);
      const r = await api("/api/settings/test-brave", { method: "POST" });
      out.innerHTML = `<div class="rd-result">${I.check.replace("<svg", '<svg width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"')}
        <span>Brave is working · found <b>${r.results}</b> AudiobookBay results for “dune”</span></div>`;
    } catch (err) {
      out.innerHTML = `<div class="rd-result bad">${esc(err.message)}</div>`;
    }
    spinBtn(btn, false);
  };

  const okLine = (html) => `<div class="rd-result">${I.check.replace("<svg", '<svg width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"')}<span>${html}</span></div>`;
  const tester = (name, describe) => {
    $(`#test-${name}`, root).onclick = async (e) => {
      const btn = e.currentTarget;
      const out = $(`#${name}-result`, root);
      spinBtn(btn, true);
      try {
        await save(true);
        out.innerHTML = okLine(describe(await api(`/api/settings/test-${name}`, { method: "POST" })));
      } catch (err) {
        out.innerHTML = `<div class="rd-result bad">${esc(err.message)}</div>`;
      }
      spinBtn(btn, false);
    };
  };
  tester("prowlarr", (r) => {
    const cats = (r.ebook_categories || []).join(", ");
    const ebooks = r.ebook_indexers?.length
      ? `<br><small>Ebooks from <b>${r.ebook_indexers.map(esc).join(", ")}</b></small>`
      : `<br><small class="warn">None of them offer ebooks (category ${esc(cats)}). Add an indexer that does in Prowlarr to find ebooks.</small>`;
    return `Connected to Prowlarr ${esc(r.version)} · <b>${r.indexers}</b> indexer${r.indexers === 1 ? "" : "s"} enabled${ebooks}`;
  });
  tester("torznab", (r) => `Connected to ${esc(r.title)} · book search ${r.book_search ? "<b>available</b>" : "not offered"} · <b>${r.categories}</b> categories`);
  $$("[data-test-client]", root).forEach((btn) => {
    btn.onclick = async () => {
      const name = btn.dataset.testClient;
      const out = $(`[data-result="${name}"]`, root);
      spinBtn(btn, true);
      try {
        await save(true);
        const r = await api(`/api/settings/test-client/${name}`, { method: "POST" });
        out.innerHTML = okLine(`Connected to ${esc(r.label)} ${esc(r.version)}`);
      } catch (err) {
        out.innerHTML = `<div class="rd-result bad">${esc(err.message)}</div>`;
      }
      spinBtn(btn, false);
    };
  });

  $("#test-rd", root).onclick = async (e) => {
    const btn = e.currentTarget;
    const out = $("#rd-result", root);
    spinBtn(btn, true);
    try {
      await save(true);
      const u = await api("/api/settings/test-rd", { method: "POST" });
      const exp = u.expiration ? new Date(u.expiration).toLocaleDateString(undefined, { dateStyle: "medium" }) : "";
      out.innerHTML = `<div class="rd-result">${I.check.replace("<svg", '<svg width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"')}
        <span>Connected as <b>${esc(u.username)}</b> · ${esc(u.type)}${exp ? ` until ${esc(exp)}` : ""}</span></div>`;
    } catch (err) {
      out.innerHTML = `<div class="rd-result bad">${esc(err.message)}</div>`;
    }
    spinBtn(btn, false);
  };

  // Notifications: each destination saves on its own, like people on the Users page, not with the save bar.
  // The editor's fields have no names, so the Settings form (and its save bar) never sees them.
  const notifyList = $("#notify-list", root);
  const editor = $("#notify-editor", root);
  let notifyMeta = null; // events, kinds, fill-ins and the default webhook body, from the server
  const KIND_INFO = {
    ntfy: { sub: "Phone and desktop", url: "https://ntfy.sh/your-topic", urlHint: "The server and the topic. ntfy.sh works, or your own server.",
      token: "Access token (optional)", tokenHint: "Only for a protected topic: an access token (tk_…), or user:password." },
    gotify: { sub: "Self-hosted", url: "https://gotify.example.com", urlHint: "Your Gotify server.",
      token: "Application token", tokenHint: "In Gotify: Apps → Create application, then copy its token." },
    discord: { sub: "A channel", url: "https://discord.com/api/webhooks/…",
      urlHint: "In Discord: Server settings → Integrations → Webhooks → New webhook → Copy webhook URL." },
    webhook: { sub: "Anything else", url: "https://",
      urlHint: "Narratarr POSTs the JSON below: Home Assistant, Apprise, n8n, your own scripts…" },
  };
  const KIND_ICON = {
    ntfy: I.bell, gotify: I.bell,
    discord: '<svg viewBox="0 0 24 24"><path d="M4 5.5h16v10H9l-5 4v-14Z"/></svg>',
    webhook: '<svg viewBox="0 0 24 24"><path d="M9 7 4 12l5 5M15 7l5 5-5 5"/></svg>',
  };
  const lastLine = (n) => n.last_error ? `<span class="bad"><i class="dot warn"></i>Last one failed: ${esc(n.last_error)}</span>`
    : n.last_at ? `<span><i class="dot ok"></i>Last sent ${esc(ago(n.last_at))}</span>` : "<span>Nothing sent yet</span>";
  const renderNotifiers = async () => {
    try {
      notifyMeta = await api("/api/settings/notifications");
      const items = notifyMeta.items;
      notifyCount = items.length;
      statuses();
      notifyList.innerHTML = items.length ? items.map((n) => `
        <div class="notify-row ${n.enabled ? "" : "off"} ${n.enabled && n.last_error ? "failing" : ""}" data-id="${n.id}">
          <span class="kind-badge" aria-hidden="true">${KIND_ICON[n.kind]}</span>
          <span class="name"><b>${esc(n.name)}</b>
            <small>${esc(notifyMeta.kinds[n.kind])} · ${esc(n.events.map((e) => notifyMeta.events[e]).join(", "))}</small>
            <small class="last">${lastLine(n)}</small></span>
          <span class="tools">
            <label class="switch" title="${n.enabled ? "On" : "Off"}"><input type="checkbox" data-enable ${n.enabled ? "checked" : ""} aria-label="Send to ${esc(n.name)}"><i></i></label>
            <button class="btn sm" type="button" data-test>Test</button>
            <button class="btn sm ghost" type="button" data-edit>Edit</button>
            <button class="btn sm ghost icon danger" type="button" data-remove aria-label="Remove ${esc(n.name)}">${I.trash}</button>
          </span>
        </div>`).join("")
        : `<p class="muted notify-empty">Nothing set up yet. Add ntfy, Gotify, Discord or a webhook to hear when someone adds a book to Wanted.</p>`;
    } catch (err) {
      notifyList.innerHTML = `<div class="rd-result bad">${esc(err.message)}</div>`;
    }
  };
  const closeEditor = () => {
    editor.hidden = true;
    editor.innerHTML = "";
    $("#add-notifier", root).hidden = false;
  };
  const headerRow = (h = { name: "", value: "" }) => `
    <div class="hrow">
      <input class="input mono" data-h="name" placeholder="Authorization" value="${esc(h.name)}" aria-label="Header name" />
      <input class="input mono" data-h="value" type="password" placeholder="Bearer …" value="${esc(h.value)}" aria-label="Header value" autocomplete="off" />
      <button class="btn sm ghost icon" type="button" data-drop aria-label="Remove this header">${I.x}</button>
    </div>`;
  const openEditor = (n) => {
    let kind = n?.kind || "ntfy";
    const events = n?.events || ["wanted", "completed", "failed"];
    $("#add-notifier", root).hidden = true;
    editor.hidden = false;
    editor.innerHTML = `
      <h4>${n ? `Edit ${esc(n.name)}` : "Add a destination"}</h4>
      <div class="choice-tiles" id="ne-kind" role="radiogroup" aria-label="Send to">
        ${Object.entries(notifyMeta.kinds).map(([k, label]) => `<button type="button" role="radio" data-v="${k}"><b>${esc(label)}</b><small>${KIND_INFO[k].sub}</small></button>`).join("")}
      </div>
      <div class="set-grid">
        <div class="field"><label for="ne-name">Name</label><input class="input" id="ne-name" maxlength="60" value="${esc(n?.name || "")}" placeholder="My phone" /></div>
        <div class="field"><label for="ne-url">Address</label><input class="input mono" id="ne-url" value="${esc(n?.url || "")}" autocomplete="off" /></div>
      </div>
      <span class="hint" id="ne-url-hint"></span>
      <div class="field" id="ne-token-field">
        <label for="ne-token" id="ne-token-label"></label>
        <input class="input mono" id="ne-token" type="password" value="${esc(n?.token || "")}" autocomplete="off" />
        <span class="hint" id="ne-token-hint"></span>
      </div>
      <div id="ne-webhook">
        <div class="field">
          <label>Headers</label>
          <div id="ne-headers">${(n?.headers || []).map(headerRow).join("")}</div>
          <div><button class="btn sm ghost" type="button" id="ne-add-header">${I.plus} Add a header</button></div>
          <span class="hint">Optional. For a token, e.g. <code>Authorization</code> · <code>Bearer …</code>. Values are kept hidden.</span>
        </div>
        <div class="field">
          <label for="ne-body">Body</label>
          <textarea class="input mono" id="ne-body" rows="9" spellcheck="false"></textarea>
          <span class="hint">JSON. Fill-ins (tap to add): ${notifyMeta.variables.map((v) => `<button type="button" class="fill-in" data-var="${v}">{${v}}</button>`).join(" ")}</span>
        </div>
      </div>
      <fieldset class="ne-events">
        <legend>Tell me when</legend>
        ${Object.entries(notifyMeta.events).map(([k, label]) => `<label class="switch"><input type="checkbox" value="${k}" ${events.includes(k) ? "checked" : ""}><i></i>${{
          wanted: "Someone adds a book to Wanted", grabbed: "A download starts", completed: "A book is in the library", failed: "A download fails",
        }[k] || esc(label)}</label>`).join("")}
      </fieldset>
      <div class="cta-row" style="justify-content:flex-start">
        <button class="btn primary" type="button" id="ne-save">Save</button>
        <button class="btn ghost" type="button" id="ne-cancel">Cancel</button>
      </div>`;
    $("#ne-body", editor).value = n?.body || notifyMeta.default_body;
    const setKind = (k) => {
      kind = k;
      $$("#ne-kind [data-v]", editor).forEach((b) => {
        b.classList.toggle("on", b.dataset.v === k);
        b.setAttribute("aria-checked", b.dataset.v === k);
      });
      const info = KIND_INFO[k];
      $("#ne-url", editor).placeholder = info.url;
      $("#ne-url-hint", editor).textContent = info.urlHint;
      $("#ne-token-field", editor).hidden = !info.token;
      $("#ne-token-label", editor).textContent = info.token || "";
      $("#ne-token-hint", editor).textContent = info.tokenHint || "";
      $("#ne-webhook", editor).hidden = k !== "webhook";
    };
    setKind(kind);
    $("#ne-kind", editor).onclick = (e) => { const b = e.target.closest("[data-v]"); if (b) setKind(b.dataset.v); };
    $("#ne-add-header", editor).onclick = () => $("#ne-headers", editor).insertAdjacentHTML("beforeend", headerRow());
    $("#ne-headers", editor).onclick = (e) => e.target.closest("[data-drop]")?.closest(".hrow").remove();
    editor.onclick = (e) => {
      const fill = e.target.closest("[data-var]");
      if (!fill) return;
      const box = $("#ne-body", editor);
      const at = box.selectionStart ?? box.value.length;
      box.setRangeText(`{${fill.dataset.var}}`, at, box.selectionEnd ?? at, "end");
      box.focus();
    };
    // Enter in a field here must not submit the Settings form around it.
    editor.onkeydown = (e) => { if (e.key === "Enter" && e.target.tagName !== "TEXTAREA") e.preventDefault(); };
    $("#ne-cancel", editor).onclick = closeEditor;
    $("#ne-save", editor).onclick = async (e) => {
      const btn = e.currentTarget;
      const body = {
        kind,
        name: $("#ne-name", editor).value,
        url: $("#ne-url", editor).value,
        token: KIND_INFO[kind].token ? $("#ne-token", editor).value : "",
        headers: $$("#ne-headers .hrow", editor).map((r) => ({ name: $('[data-h="name"]', r).value, value: $('[data-h="value"]', r).value })),
        body: kind === "webhook" ? $("#ne-body", editor).value : "",
        events: $$(".ne-events input:checked", editor).map((i) => i.value),
      };
      spinBtn(btn, true);
      try {
        await api(n ? `/api/settings/notifications/${n.id}` : "/api/settings/notifications", { method: n ? "PUT" : "POST", body });
        closeEditor();
        await renderNotifiers();
        toast(n ? "Destination saved" : "Destination added. Choose Test to send one now.", { kind: "ok" });
      } catch (err) {
        toast(err.message, { kind: "err" });
        spinBtn(btn, false);
      }
    };
    $("#ne-name", editor).focus();
  };
  $("#add-notifier", root).onclick = () => notifyMeta && openEditor(null);
  notifyList.onchange = async (e) => {
    const box = e.target.closest("[data-enable]");
    if (!box) return;
    const id = box.closest(".notify-row").dataset.id;
    try {
      await api(`/api/settings/notifications/${id}`, { method: "PUT", body: { enabled: box.checked } });
    } catch (err) {
      toast(err.message, { kind: "err" });
    }
    renderNotifiers();
  };
  notifyList.onclick = async (e) => {
    const row = e.target.closest(".notify-row");
    if (!row) return;
    const n = notifyMeta.items.find((x) => String(x.id) === row.dataset.id);
    if (e.target.closest("[data-edit]")) return openEditor(n);
    if (e.target.closest("[data-test]")) {
      const btn = e.target.closest("[data-test]");
      spinBtn(btn, true);
      try {
        await api(`/api/settings/notifications/${n.id}/test`, { method: "POST" });
        toast(`Sent a test to ${n.name}`, { kind: "ok" });
      } catch (err) {
        toast(err.message, { kind: "err" });
      }
      spinBtn(btn, false);
      return renderNotifiers();
    }
    if (e.target.closest("[data-remove]")) {
      const ok = await modal({ title: `Remove ${n.name}?`, body: "Narratarr will stop sending notifications there.", confirm: "Remove", danger: true });
      if (!ok) return;
      try {
        await api(`/api/settings/notifications/${n.id}`, { method: "DELETE" });
        if (!editor.hidden) closeEditor();
      } catch (err) {
        toast(err.message, { kind: "err" });
      }
      renderNotifiers();
    }
  };
  renderNotifiers();

  // The event log: what Narratarr announced, and where each event went. Refreshes itself while open.
  const LOG_EVENTS = [["", "All", ""], ["wanted", "Requests", "accent"], ["grabbed", "Started", "info"], ["completed", "Finished", "ok"], ["failed", "Failed", "bad"]];
  const logState = { event: "", q: "", items: [], more: false, open: new Set(), counts: {}, kept: 500 };
  const logList = $("#log-list", root);
  const logStamp = (at) => {
    const d = new Date(at * 1000);
    const time = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    return d.toDateString() === new Date().toDateString() ? time : `${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${time}`;
  };
  const sentChip = (d) => {
    const [cls, mark] = { sent: ["ok", "✓"], failed: ["bad", "✕"], sending: ["", "…"] }[d.state] || ["", "–"];
    return `<span class="sent-chip ${cls}">${esc(d.name)} ${mark}</span>`;
  };
  const detailLine = (d) => {
    const kind = notifyMeta?.kinds?.[d.kind] || d.kind;
    const text = d.state === "sent" ? `Sent in ${(d.ms / 1000).toFixed(1)} s` : d.state === "failed" ? d.error
      : d.state === "sending" ? "Sending…" : d.error || "Not sent";
    return `<div class="log-dest ${d.state === "failed" ? "bad" : d.state === "sent" ? "ok" : ""}"><i aria-hidden="true"></i>
      <span><b>${esc(d.name)}</b> <small>· ${esc(kind)}</small><br><span class="why">${esc(text)}</span></span>
      ${d.state === "failed" ? `<button class="btn sm" type="button" data-edit-dest="${d.id}">Edit destination</button>` : ""}</div>`;
  };
  const renderLog = () => {
    const total = Object.values(logState.counts).reduce((a, b) => a + b, 0);
    $("#log-filters", root).innerHTML = LOG_EVENTS.map(([key, label, tone]) => `
      <button type="button" role="radio" aria-checked="${logState.event === key}" class="fpill ${logState.event === key ? "on" : ""}" data-log-event="${key}">
        ${tone ? `<i class="dot ${tone}" aria-hidden="true"></i>` : ""}${label} <small>${key ? logState.counts[key] || 0 : total}</small></button>`).join("");
    $("#log-kept", root).textContent = `Narratarr keeps the most recent ${logState.kept} events here. Older ones are still in docker logs.`;
    $("#log-more", root).hidden = !logState.more;
    logList.innerHTML = logState.items.length ? logState.items.map((e) => {
      const open = logState.open.has(e.id);
      const inLib = e.book_id && state.library.some((b) => b.id === e.book_id);
      return `
      <li class="log-row ${open ? "open" : ""}" data-eid="${e.id}">
        <button type="button" class="log-line" aria-expanded="${open}">
          <time>${esc(logStamp(e.at))}</time>
          <span class="ev ev-${esc(e.event)}">${esc(e.label)}</span>
          <span class="msg">${esc(e.message)}</span>
          <span class="sent">${e.deliveries.length ? e.deliveries.map(sentChip).join("") : '<span class="sent-chip">Log only</span>'}</span>
          ${I.chevron}
        </button>
        ${open ? `<div class="log-detail">
          <pre>${esc(e.line)}</pre>
          ${e.deliveries.map(detailLine).join("")}
          ${inLib ? `<button type="button" class="link" data-open-book="${e.book_id}">Open the book</button>` : ""}
        </div>` : ""}
      </li>`;
    }).join("") : `<li class="log-empty">${logState.q || logState.event ? "Nothing matches." : "Nothing has happened yet. Requests, downloads and failures show up here as they happen."}</li>`;
  };
  const loadLog = async (older = false) => {
    const params = new URLSearchParams({ event: logState.event, q: logState.q, limit: "50" });
    if (older && logState.items.length) params.set("before", logState.items[logState.items.length - 1].id);
    try {
      const r = await api(`/api/settings/notifications/events?${params}`);
      logState.items = older ? [...logState.items, ...r.items] : r.items;
      logState.more = r.more;
      logState.counts = r.counts;
      logState.kept = r.kept;
      renderLog();
    } catch (err) {
      logList.innerHTML = `<li class="log-empty bad">${esc(err.message)}</li>`;
    }
  };
  $("#log-filters", root).onclick = (e) => {
    const b = e.target.closest("[data-log-event]");
    if (!b) return;
    logState.event = b.dataset.logEvent;
    loadLog();
  };
  let logTimer = 0;
  const logQ = $("#log-q", root);
  logQ.oninput = () => { clearTimeout(logTimer); logTimer = setTimeout(() => { logState.q = logQ.value.trim(); loadLog(); }, 300); };
  logQ.onkeydown = (e) => { if (e.key === "Enter") e.preventDefault(); }; // it lives inside the settings form
  $("#log-more", root).onclick = () => loadLog(true);
  $("#log-copy", root).onclick = async (e) => {
    const btn = e.currentTarget;
    try {
      const text = await fetch("/api/settings/notifications/events.log", { credentials: "same-origin" }).then((r) => {
        if (!r.ok) throw new Error("Couldn’t read the event log.");
        return r.text();
      });
      await navigator.clipboard.writeText(text);
      toast("Event log copied", { kind: "ok" });
    } catch (err) {
      toast(err.message || "Couldn’t copy the event log.", { kind: "err" });
    }
    btn.blur();
  };
  logList.onclick = (e) => {
    const edit = e.target.closest("[data-edit-dest]");
    if (edit) {
      const n = notifyMeta?.items.find((x) => x.id === Number(edit.dataset.editDest));
      if (n) { openEditor(n); editor.scrollIntoView({ behavior: "smooth", block: "center" }); }
      else toast("That destination has been removed.", { kind: "err" });
      return;
    }
    const book = e.target.closest("[data-open-book]");
    if (book) return openDrawer({ bookId: Number(book.dataset.openBook) });
    const row = e.target.closest(".log-line");
    if (!row) return;
    const id = Number(row.closest("[data-eid]").dataset.eid);
    if (logState.open.has(id)) logState.open.delete(id); else logState.open.add(id);
    renderLog();
  };
  loadLog();
  // Live: check for new events every few seconds while the Notifications page is showing.
  const logLive = setInterval(() => {
    if (!logList.isConnected) return clearInterval(logLive);
    if (document.hidden || !logList.offsetParent || logState.items.length > 50) return;
    loadLog();
  }, 8000);

  // AllDebrid and TorBox: connect with a short code the user enters on their site, or with a pasted key.
  const codeConnect = ({ p, name, client, site, start, check, line }) => {
    const out = () => $(`#${p}-result`, root);
    const card = () => {
      const on = Boolean(state.settings[`${p}_configured`]);
      $(`#${p}-pin-title`, root).textContent = on ? `${name} is connected` : "Connect with a code";
      $(`#${p}-pin-desc`, root).textContent = on
        ? "Test checks the account. To use a different one, connect again."
        : `Narratarr shows a short code, you enter it on ${site}, and you’re connected. There’s no key to copy, and your password never comes here.`;
      $(`#${p}-pin`, root).textContent = on ? "Connect again" : `Connect ${name}`;
      $(`#${p}-pin`, root).classList.toggle("primary", !on);
      $(`#${p}-pin-start [data-${p}-test]`, root).hidden = !on;
      if (on) $(`[data-v="${client}"] small`, root).innerHTML = `<i class="dot ok"></i>Ready`;
    };
    card();
    $$(`[data-${p}-test]`, root).forEach((btn) => {
      btn.onclick = async () => {
        spinBtn(btn, true);
        try {
          await save(true);
          card();
          out().innerHTML = line(await api(`/api/settings/test-${p}`, { method: "POST" }));
        } catch (err) {
          out().innerHTML = badLine(err.message);
        }
        spinBtn(btn, false);
      };
    });
    let timer = 0;
    const stop = () => {
      clearTimeout(timer);
      $(`#${p}-pin-wait`, root).hidden = true;
      $(`#${p}-pin-start`, root).hidden = false;
    };
    $(`#${p}-pin-cancel`, root).onclick = stop;
    $(`#${p}-pin`, root).onclick = async (e) => {
      const btn = e.currentTarget;
      const wait = $(`#${p}-pin-wait`, root);
      out().innerHTML = "";
      spinBtn(btn, true);
      let got;
      try {
        got = await api(start, { method: "POST" });
      } catch (err) {
        out().innerHTML = badLine(err.message);
        spinBtn(btn, false);
        return;
      }
      spinBtn(btn, false);
      const code = got.pin || got.code;
      $(`#${p}-pin-code`, root).textContent = code;
      $(`#${p}-pin-link`, root).href = got.user_url;
      $(`#${p}-pin-start`, root).hidden = true;
      wait.hidden = false;
      const ends = Date.now() + got.expires_in * 1000;
      const every = Math.max(3, got.interval || 3) * 1000;
      const tick = async () => {
        if (!wait.isConnected || wait.hidden) return; // cancelled, or left Settings
        const left = Math.ceil((ends - Date.now()) / 60000);
        if (left <= 0) {
          stop();
          out().innerHTML = badLine(`That code has expired. Choose Connect ${name} to get a new one.`);
          return;
        }
        $(`#${p}-pin-left`, root).textContent = `Waiting for the code · expires in ${left} min`;
        try {
          const r = await api(check, { method: "POST", body: got.pin ? { pin: code } : { code } });
          if (!wait.isConnected || wait.hidden) return;
          if (r.connected) {
            // The server saved the key and chose this service; keep anything else typed here as it was.
            const key = `${p}_api_key`;
            const { torrent_client, torrent_configured } = r.settings;
            Object.assign(state.settings, { [key]: r.settings[key], [`${p}_configured`]: true, torrent_client, torrent_configured });
            form[key].value = r.settings[key];
            if (saved) Object.assign(saved, { [key]: r.settings[key], torrent_client });
            choose("torrent_client", torrent_client);
            stop();
            card();
            updateRdPill();
            statuses();
            out().innerHTML = line(r);
            return;
          }
        } catch (err) {
          stop();
          out().innerHTML = badLine(err.message);
          return;
        }
        timer = setTimeout(tick, every);
      };
      timer = setTimeout(tick, every);
    };
  };
  const badLine = (text) => `<div class="rd-result bad">${esc(text)}</div>`;
  codeConnect({
    p: "ad", name: "AllDebrid", client: "alldebrid", site: "alldebrid.com",
    start: "/api/settings/alldebrid/pin", check: "/api/settings/alldebrid/pin/check",
    line: (u) => {
      if (u.warning) return badLine(u.warning);
      if (!u.premium && !u.trial) return badLine(`Connected as ${u.username}, but the account isn’t premium. AllDebrid needs premium for torrents.`);
      const until = u.premium_until ? new Date(u.premium_until * 1000).toLocaleDateString(undefined, { dateStyle: "medium" }) : "";
      return okLine(`Connected as <b>${esc(u.username)}</b> · ${u.trial ? "free trial" : "premium"}${until ? ` until ${esc(until)}` : ""}`);
    },
  });
  codeConnect({
    p: "tb", name: "TorBox", client: "torbox", site: "TorBox",
    start: "/api/settings/torbox/code", check: "/api/settings/torbox/code/check",
    line: (u) => {
      if (u.warning) return badLine(u.warning);
      const until = u.premium_until ? new Date(u.premium_until).toLocaleDateString(undefined, { dateStyle: "medium" }) : "";
      return okLine(`Connected${u.email ? ` as <b>${esc(u.email)}</b>` : ""}${u.plan ? ` · ${esc(u.plan)} plan` : ""}${until ? ` until ${esc(until)}` : ""}`);
    },
  });
  $("#test-pm", root).onclick = async (e) => {
    const btn = e.currentTarget;
    const out = $("#pm-result", root);
    spinBtn(btn, true);
    try {
      await save(true);
      const u = await api("/api/settings/test-pm", { method: "POST" });
      const until = u.premium_until ? new Date(u.premium_until * 1000).toLocaleDateString(undefined, { dateStyle: "medium" }) : "";
      out.innerHTML = u.premium ? okLine(`Connected · premium${until ? ` until ${esc(until)}` : ""}`)
        : badLine("Connected, but the account isn’t premium. Premiumize needs premium for torrents.");
      if (u.premium) $('[data-v="premiumize"] small', root).innerHTML = `<i class="dot ok"></i>Ready`;
    } catch (err) {
      out.innerHTML = badLine(err.message);
    }
    spinBtn(btn, false);
  };

  // Audiobookshelf: find the server, sign in once (which creates an API key), pick a library.
  let absServer = null; // what Find last found: {url, version, ready, password_login, insecure}
  const absConnected = () => Boolean(state.settings.audiobookshelf_url && state.settings.audiobookshelf_token);
  const absShow = async (libs) => {
    const on = absConnected();
    $("#abs-connected", root).hidden = !on;
    $("#abs-head", root).hidden = !on;
    $("#abs-intro", root).hidden = on;
    $("#abs-check-panel", root).hidden = !on || page !== "abs";
    statuses();
    $("#abs-setup", root).hidden = on;
    $("#abs-manual", root).hidden = on;
    $("#abs-signin", root).hidden = on || !(absServer?.ready && absServer?.password_login);
    if (!on) return;
    $("#abs-where", root).textContent = state.settings.audiobookshelf_url;
    try {
      const r = libs || (await api("/api/audiobookshelf/libraries"));
      const sel = form.audiobookshelf_library_id;
      const known = r.libraries.some((l) => l.id === r.library_id);
      sel.innerHTML = (known ? "" : `<option value="">Choose a library</option>`)
        + r.libraries.map((l) => `<option value="${esc(l.id)}" ${l.id === r.library_id ? "selected" : ""}>${esc(l.name)}</option>`).join("");
    } catch (err) {
      $("#audiobookshelf-result", root).innerHTML = badLine(err.message);
    }
  };
  const absFound = (server) => {
    absServer = server;
    form.audiobookshelf_url.value = server.url;
    $("#abs-found", root).innerHTML = okLine(`Found Audiobookshelf <b>${esc(server.version)}</b> at <code>${esc(server.url)}</code>`)
      + (server.ready ? "" : badLine("Finish setting up Audiobookshelf first: it doesn’t have an admin account yet."))
      + (server.password_login ? "" : badLine("This server only allows single sign-on, so paste an API key below instead."));
    $("#abs-insecure", root).hidden = !server.insecure;
    $("#abs-insecure-ok", root).checked = false;
    if (!server.password_login) $("#abs-manual", root).open = true;
    absShow();
    if (!$("#abs-signin", root).hidden) $("#abs-user", root).focus();
  };

  $("#abs-find", root).onclick = async (e) => {
    const btn = e.currentTarget;
    const out = $("#abs-found", root);
    const address = form.audiobookshelf_url.value.trim();
    absServer = null;
    absShow();
    spinBtn(btn, true);
    out.innerHTML = `<span class="hint">${address ? `Looking for Audiobookshelf at ${esc(address)}…` : "Looking on this computer, in Docker, then across your home network…"}</span>`;
    try {
      const { servers } = await api("/api/audiobookshelf/find", { method: "POST", body: { address } });
      if (servers.length === 1) {
        absFound(servers[0]);
      } else if (servers.length) {
        out.innerHTML = `<span class="hint">Found ${servers.length} Audiobookshelf servers. Which one?</span>
          <div class="abs-pick">${servers.map((sv, i) => `<button class="btn" type="button" data-i="${i}"><span class="mono">${esc(sv.url)}</span><small>${esc(sv.version)}</small></button>`).join("")}</div>`;
        $$(".abs-pick [data-i]", out).forEach((b) => { b.onclick = () => absFound(servers[b.dataset.i]); });
      } else {
        out.innerHTML = badLine(address
          ? `Audiobookshelf didn’t answer at ${address}. Check the address, and that this machine can reach it.`
          : "No Audiobookshelf found nearby. Type its address and press Find.");
      }
    } catch (err) {
      out.innerHTML = badLine(err.message);
    }
    spinBtn(btn, false);
  };
  form.audiobookshelf_url.onkeydown = (e) => {
    if (e.key === "Enter") { e.preventDefault(); $("#abs-find", root).click(); }
  };

  $("#abs-sign-in", root).onclick = async (e) => {
    const btn = e.currentTarget;
    const out = $("#abs-signin-result", root);
    const insecure = Boolean(absServer?.insecure);
    if (insecure && !$("#abs-insecure-ok", root).checked) {
      out.innerHTML = badLine("Use an https address, or tick the box above to send the password anyway.");
      return;
    }
    spinBtn(btn, true);
    try {
      const r = await api("/api/audiobookshelf/sign-in", {
        method: "POST",
        body: { url: absServer.url, username: $("#abs-user", root).value.trim(), password: $("#abs-pass", root).value, allow_insecure: insecure },
      });
      state.settings = r.settings;
      form.audiobookshelf_token.value = r.settings.audiobookshelf_token;
      absServer = null;
      out.innerHTML = $("#abs-found", root).innerHTML = "";
      await absShow(r);
      toast(r.library_id ? "Connected to Audiobookshelf" : "Connected. Now pick the library Narratarr’s books go into.", { kind: "ok" });
    } catch (err) {
      out.innerHTML = badLine(err.message);
    }
    $("#abs-pass", root).value = "";
    spinBtn(btn, false);
  };
  $("#abs-pass", root).onkeydown = (e) => {
    if (e.key === "Enter") { e.preventDefault(); $("#abs-sign-in", root).click(); }
  };

  form.audiobookshelf_library_id.onchange = async () => {
    $("#audiobookshelf-result", root).innerHTML = "";
    try { await save(true); } catch (err) { toast(err.message, { kind: "err" }); }
  };
  $("#abs-check", root).onclick = async (e) => {
    const btn = e.currentTarget;
    const out = $("#abs-check-result", root);
    spinBtn(btn, true);
    out.hidden = false;
    out.innerHTML = `<span class="hint"><span class="spinner"></span> Asking Audiobookshelf to rescan…</span>`;
    try {
      const r = await api("/api/audiobookshelf/check", { method: "POST", body: { scan: true } });
      const all = r.seen === r.books;
      out.innerHTML = `<div class="abs-count ${all ? "ok" : ""}"><b>${r.seen}<small> / ${r.books}</small></b>
        <span>${!r.books ? "Narratarr hasn’t downloaded any books yet."
          : all ? "All of your books are in Audiobookshelf."
          : `Audiobookshelf can’t see ${r.books - r.seen} of them yet, such as ${r.missing.slice(0, 3).map((t) => `“${esc(t)}”`).join(", ")}. The setup guide shows how to fix that.`}</span></div>`;
    } catch (err) {
      out.innerHTML = badLine(err.message);
    }
    spinBtn(btn, false);
  };
  tester("audiobookshelf", (r) => `Connected to Audiobookshelf ${esc(r.version)} · rescans <b>${esc(r.library)}</b> after each download`);
  $("#abs-disconnect", root).onclick = async (e) => {
    e.preventDefault();
    state.settings = await api("/api/settings", { method: "PUT", body: { audiobookshelf_token: "", audiobookshelf_library_id: "" } });
    form.audiobookshelf_token.value = "";
    $("#audiobookshelf-result", root).innerHTML = "";
    $("#abs-check-result", root).hidden = true;
    absShow();
    saved = snapshot();
    dirty();
    toast("Disconnected. Narratarr’s key is still listed in Audiobookshelf, where you can revoke it.");
  };
  absShow();
  $$("[data-guide]", root).forEach((el) => {
    el.onclick = (e) => {
      e.preventDefault();
      openAbsGuide({
        onConnect: () => {
          location.hash = "#/settings/abs";
          $("#abs-setup", root).scrollIntoView({ behavior: "smooth", block: "center" });
          form.audiobookshelf_url.focus({ preventScroll: true });
        },
      });
    };
  });

  // Users: who can sign in, and as what.
  const userList = $("#user-list", root);
  const renderUsers = async () => {
    try {
      const all = await api("/api/users");
      userCount = all.length;
      statuses();
      userList.innerHTML = all.map((u) => `
        <div class="user-row" data-id="${u.id}" data-name="${esc(u.username)}">
          <span class="avatar">${esc(u.username.slice(0, 1))}</span>
          <span class="name"><b>${esc(u.username)}</b><small>${u.id === state.user.id ? "You · " : ""}${u.role === "admin" ? "Admin" : "Listener"}</small></span>
          <span class="tools">
            <select class="select" data-role aria-label="Role for ${esc(u.username)}">
              <option value="admin" ${u.role === "admin" ? "selected" : ""}>Admin</option>
              <option value="listener" ${u.role === "listener" ? "selected" : ""}>Listener</option>
            </select>
            <button class="btn sm ghost" type="button" data-reset>Reset password</button>
            <button class="btn sm ghost icon danger" type="button" data-remove aria-label="Remove ${esc(u.username)}">${I.trash}</button>
          </span>
        </div>`).join("");
    } catch (err) {
      userList.innerHTML = `<div class="rd-result bad">${esc(err.message)}</div>`;
    }
  };
  userList.onchange = async (e) => {
    const sel = e.target.closest("[data-role]");
    if (!sel) return;
    const row = sel.closest(".user-row");
    try {
      await api(`/api/users/${row.dataset.id}`, { method: "PATCH", body: { role: sel.value } });
      if (Number(row.dataset.id) === state.user.id) return location.reload(); // you just changed what you can do
      toast(`${row.dataset.name} is now ${sel.value === "admin" ? "an admin" : "a listener"}`, { kind: "ok" });
    } catch (err) {
      toast(err.message, { kind: "err" });
    }
    renderUsers();
  };
  userList.onclick = async (e) => {
    const row = e.target.closest(".user-row");
    if (!row) return;
    const name = row.dataset.name;
    if (e.target.closest("[data-reset]")) {
      formModal({
        title: `New password for ${name}`,
        body: "They’ll be signed out everywhere and sign in again with this one.",
        fields: passwordFields(),
        confirm: "Set password",
        onSubmit: async (f) => {
          matching(f);
          await api(`/api/users/${row.dataset.id}`, { method: "PATCH", body: { password: f.password.value } });
          toast(`New password set for ${name}`, { kind: "ok" });
        },
      });
    } else if (e.target.closest("[data-remove]")) {
      const ok = await modal({ title: `Remove ${name}?`, body: "They won’t be able to sign in, and their listening progress is deleted. Books stay in the library.", confirm: "Remove", danger: true });
      if (!ok) return;
      try {
        await api(`/api/users/${row.dataset.id}`, { method: "DELETE" });
        toast(`Removed ${name}`);
      } catch (err) {
        toast(err.message, { kind: "err" });
      }
      renderUsers();
    }
  };
  $("#add-user", root).onclick = () => formModal({
    title: "Add someone",
    body: "Give them the username and password; they can change the password themselves.",
    fields: `
      <div class="field"><label for="nu-name">Username</label><input class="input" id="nu-name" name="username" autocomplete="off" autocapitalize="off" spellcheck="false" /></div>
      ${passwordFields()}
      <div class="field"><label for="nu-role">Role</label>
        <select class="select abs-select" id="nu-role" name="role">
          <option value="listener">Listener: browses, requests and listens</option>
          <option value="admin">Admin: also runs settings and users</option>
        </select>
      </div>`,
    confirm: `${I.plus} Add`,
    onSubmit: async (f) => {
      matching(f);
      const u = await api("/api/users", { method: "POST", body: { username: f.username.value.trim(), password: f.password.value, role: f.role.value } });
      toast(`Added ${u.username}`, { kind: "ok" });
      renderUsers();
    },
  });
  renderUsers();

  go(page);
  statuses();
  saved = snapshot();
  return { go };
}

// ---------------------------------------------------------------------------
// Drawer (book details)

const drawer = $("#drawer");
const scrim = $("#drawer-scrim");

function closeDrawer() {
  drawer.classList.remove("open");
  scrim.classList.remove("open");
  document.body.style.overflow = "";
  state.drawer = null;
}
scrim.onclick = closeDrawer;

function drawerBook() {
  const d = state.drawer;
  if (!d) return null;
  return d.bookId ? state.library.find((b) => b.id === d.bookId) : libFor(byUrl(), d.url);
}

function wantedNote(lib) {
  const st = state.settings || {};
  const out = Date.parse(lib.released || "");
  if (out > Date.now()) return `Comes out ${releaseLabel(lib.released, true).replace(/^Out /, "")}. Narratarr will start looking then.`;
  if (!st.indexer_configured) return "Connect Prowlarr or a Torznab indexer in Settings so Narratarr can look for this book.";
  const last = lib.last_searched ? `Last looked ${ago(lib.last_searched)}. ${lib.search_note || ""}` : "";
  const next = st.auto_search ? `Narratarr looks again every ${st.auto_search_hours} hours.` : "Automatic search is off.";
  return `${last} ${next}`.trim();
}

// Audiobook, ebook or both: what a catalog book goes to Wanted as. Remembered for next time.
function formatPickHTML() {
  const format = formatFor(state.drawer?.source);
  const notes = {
    audiobook: "Narratarr will look for the audiobook on your indexers and download it when it turns up.",
    ebook: "Narratarr will look for the ebook on your indexers and download it when it turns up.",
    both: "Narratarr will look for both, and file the ebook next to the audiobook so Audiobookshelf shows them together.",
  };
  return `<div class="format-pick" role="radiogroup" aria-label="Want it as">
      ${Object.entries(FORMATS).map(([k, f]) => `
        <button type="button" role="radio" aria-checked="${format === k}" class="${format === k ? "on" : ""}" data-format="${k}">
          <span class="ic">${f.icon ? I[f.icon] : I.headphones + I.book}</span><b>${f.label}</b>
        </button>`).join("")}
    </div>
    <div class="cta"><button class="btn primary" data-act="add">${I.bookmark} Add to Wanted</button></div>
    <p class="cta-note">${notes[format]}</p>`;
}

// The book in both formats: where each one is, and a way to want the one you don't have.
function formatsHTML(lib) {
  const other = lib.companion_id ? state.library.find((b) => b.id === lib.companion_id) : null;
  const pair = { audiobook: isEbook(lib) ? other : lib, ebook: isEbook(lib) ? lib : other };
  const tile = (kind) => {
    const b = pair[kind], f = FORMATS[kind];
    if (!b) {
      return `<button class="fmt add" data-act="other-format" title="Add the ${f.label.toLowerCase()} to Wanted too">
        ${I[f.icon]}<span><b>${f.label}</b><small>Want it too</small></span><i class="plus">${I.plus}</i></button>`;
    }
    const here = b.id === lib.id;
    return `<button class="fmt ${here ? "on" : ""}" ${here ? 'aria-current="true"' : `data-open-book="${b.id}"`} title="${here ? "Showing" : "Show"} the ${f.label.toLowerCase()}">
      ${I[f.icon]}<span><b>${f.label}</b><small>${statusTag(b.status)}</small></span></button>`;
  };
  return `<div class="formats" aria-label="Formats">${tile("audiobook")}${tile("ebook")}</div>`;
}

function liveHTML(lib) {
  if (!lib && isCatalog(state.drawer?.source)) return formatPickHTML();
  return lib ? formatsHTML(lib) + libraryLiveHTML(lib) : libraryLiveHTML(lib);
}

function libraryLiveHTML(lib) {
  if (!lib) {
    if (asksFirst()) {
      return `<div class="cta"><button class="btn primary" data-act="add">${I.bell} Ask for it</button></div>
        <p class="cta-note">An admin approves requests before anything downloads.</p>`;
    }
    return `<div class="cta">
      <button class="btn primary" data-act="add">${I.down} Download</button>
      <button class="btn" data-act="want">${I.bookmark} Add to Wanted</button>
    </div>`;
  }
  if (lib.status === "requested") {
    const who = lib.requested_by_name ? esc(lib.requested_by_name) : "A listener";
    const picked = !isCatalog(lib.source) && (lib.release || lib.title);
    if (isAdmin()) {
      return `<div class="state requested">
          <div class="top"><span>${I.bell} ${who} asked for this</span><span>${esc(ago(lib.updated_at || lib.added_at))}</span></div>
          ${reqStepsHTML(lib)}
          <p>${picked ? `They picked <b>${esc(picked)}</b>${lib.size ? ` (${esc(lib.size)})` : ""}. Approve it to download that release, or pick another.`
            : "Approve it and Narratarr starts looking for a release, or pick one yourself."}</p>
        </div>
        <div class="cta">
          <button class="btn primary" data-act="approve">${I.check} Approve</button>
          <button class="btn" data-act="releases">${I.list} ${picked ? "Pick another" : "Pick a release"}</button>
          <button class="btn ghost danger" data-act="remove">${I.trash} Decline</button>
        </div>`;
    }
    return `<div class="state requested">
        <div class="top"><span>${I.bell} Awaiting approval</span><span>${esc(ago(lib.updated_at || lib.added_at))}</span></div>
        ${reqStepsHTML(lib)}
        <p>${mine(lib) ? "You asked for this" : `${who} asked for this`}${picked ? `, as <b>${esc(picked)}</b>` : ""}. It’ll download once an admin approves it.</p>
      </div>
      ${mine(lib) ? `<div class="cta"><button class="btn ghost danger" data-act="remove" data-mine>${I.trash} Take back request</button></div>` : ""}`;
  }
  if (lib.status === "wanted" && isCatalog(lib.source)) {
    return `<div class="state wanted">
        <div class="top"><span>${I.bookmark} Wanted</span><span>${lib.last_searched ? esc(ago(lib.last_searched)) : "Not searched yet"}</span></div>
        ${!isAdmin() && mine(lib) ? reqStepsHTML(lib) : ""}
        <p>${esc(wantedNote(lib))}</p>
      </div>
      <div class="cta">
        <button class="btn primary" data-act="download">${asksFirst() ? `${I.bell} Ask for it now` : `${I.search} Search now`}</button>
        <button class="btn" data-act="releases">${I.list} Pick a release</button>
        <button class="btn ghost danger" data-act="remove" ${mine(lib) ? "data-mine" : ""}>${I.trash} ${isAdmin() ? "Remove" : "Take back request"}</button>
      </div>`;
  }
  if (ACTIVE.includes(lib.status)) {
    const measurable = ["caching", "downloading"].includes(lib.status);
    return `<div class="state">
        <div class="top"><span>${esc(STATUS[lib.status])}</span><span>${measurable ? `${(lib.progress || 0).toFixed(0)}%${lib.speed ? ` · ${fmtBytes(lib.speed)}/s` : ""}` : ""}</span></div>
        <div class="progress ${measurable ? "" : "indeterminate"}"><i style="width:${lib.progress || 0}%"></i></div>
        <div class="top" style="margin-top:10px"><span style="color:var(--text-3);font-size:12.5px">${esc(lib.status === "queued" ? "Waiting for a free slot…" : lib.detail)}</span></div>
      </div>
      <div class="cta"><button class="btn" data-act="stop">${I.stop} Stop</button>
        ${!isAdmin() && mine(lib) ? `<button class="btn ghost danger" data-act="remove" data-mine>${I.trash} Take back request</button>` : ""}</div>`;
  }
  if (lib.status === "completed" && isEbook(lib)) {
    const local = lib.source === "local";
    const label = !lib.finished && underway(lib) ? `${I.book} Continue reading · ${esc(readLeft(lib))}`
      : `${I.book} ${lib.finished ? "Read again" : "Read"}`;
    return `<div class="cta">
        ${canRead(lib) ? `<button class="btn primary" data-act="play">${label}</button>
        <a class="btn" href="/api/library/${lib.id}/ebook" download>${I.down} Download</a>`
        : `<a class="btn primary" href="/api/library/${lib.id}/ebook" download>${I.down} Download ebook</a>`}
        <button class="btn ghost" data-act="toggle-finished">${lib.finished ? "Mark unread" : "Mark read"}</button>
      </div>
      <p class="cta-note">${canRead(lib) ? `Reads right here, and opens in Audiobookshelf or any reading app too.`
        : `${esc(lib.format || "This format")} doesn’t open in Narratarr. It works with ${esc(readers(lib.format))}.`}</p>
      <div class="state done">${local ? "Imported" : "Downloaded"} ${esc(ago(lib.completed_at))}${local && lib.import_mode === "inplace" ? ", left where it was," : ""} to<code>${esc(lib.path)}</code></div>
      <div class="cta" style="margin-top:14px">
        ${local ? "" : `<button class="btn sm ghost" data-act="download">${I.retry} Download again</button>`}
        <button class="btn sm ghost" data-act="edit">${I.pencil} Edit details</button>
        <button class="btn sm ghost danger" data-act="remove">${I.trash} Remove</button>
      </div>`;
  }
  if (lib.status === "completed") {
    const current = player.book?.id === lib.id;
    const local = lib.source === "local";
    const left = lib.listen_duration - lib.listen_position;
    const label = current && player.playing ? `${I.pause} Pause`
      : !lib.finished && lib.listen_position > 30 && lib.listen_duration ? `${I.play} Resume · ${esc(fmtLeft(left))}`
      : `${I.play} ${lib.finished ? "Listen again" : "Play"}`;
    return `<div class="cta">
        <button class="btn primary" data-act="play">${label}</button>
        <button class="btn ghost" data-act="toggle-finished">${lib.finished ? "Mark unfinished" : "Mark finished"}</button>
      </div>
      <div class="state done">${local ? "Imported" : "Downloaded"} ${esc(ago(lib.completed_at))}${local && lib.import_mode === "inplace" ? ", left where it was," : ""} to<code>${esc(lib.path)}</code></div>
      <div class="cta" style="margin-top:14px">
        ${local ? "" : `<button class="btn sm ghost" data-act="download">${I.retry} Download again</button>`}
        <button class="btn sm ghost" data-act="edit">${I.pencil} Edit details</button>
        <button class="btn sm ghost danger" data-act="remove">${I.trash} Remove</button>
      </div>`;
  }
  if (lib.status === "failed") {
    return `<div class="state failed">${esc(lib.error || "Something went wrong.")}</div>
      <div class="cta">
        <button class="btn primary" data-act="download">${I.retry} ${asksFirst() ? "Ask to try again" : "Try again"}</button>
        ${lib.source !== "local" ? `<button class="btn" data-act="releases">${I.list} Pick another release</button>` : ""}
        <button class="btn ghost danger" data-act="remove" ${mine(lib) ? "data-mine" : ""}>${I.trash} ${isAdmin() ? "Remove" : "Take back request"}</button>
      </div>`;
  }
  return `<div class="cta">
      <button class="btn primary" data-act="download">${asksFirst() ? `${I.bell} Ask for it` : `${I.down} Download now`}</button>
      <button class="btn" data-act="releases">${I.list} Other releases</button>
      <button class="btn ghost danger" data-act="remove" ${mine(lib) ? "data-mine" : ""}>${I.trash} ${isAdmin() ? "Remove" : "Take back request"}</button>
    </div>`;
}

function readers(format) {
  return {
    EPUB: "Apple Books, Google Play Books, Kobo and most e-readers",
    AZW3: "Kindle devices and the Kindle app",
    AZW: "Kindle devices and the Kindle app",
    MOBI: "older Kindles and Calibre",
    PDF: "any PDF viewer",
    CBZ: "comic readers",
    CBR: "comic readers",
  }[(format || "").toUpperCase()] || "Calibre and most reading apps";
}

// ---- releases (pick one yourself) ----

function releasesHTML(res) {
  if (!res.items.length) {
    return `<div class="releases"><h4>Releases</h4><p class="muted">${isEbook(drawerBook())
      ? "Nothing in your indexers’ ebook categories looks like this book yet."
      : "Nothing on your indexers or LibriVox looks like this book yet."}</p></div>`;
  }
  const label = (r) => r.source === "prowlarr" || r.source === "torznab" ? r.indexer || sourceInfo(r.source).label : sourceInfo(r.source).label;
  return `<div class="releases">
    <h4>Releases <small>${res.items.length} found · best matches first</small></h4>
    ${res.items.map((r, i) => {
      const quality = r.match >= res.auto_match ? "good" : r.match >= 75 ? "ok" : "weak";
      const facts = [label(r), r.protocol === "usenet" ? "Usenet" : r.protocol === "torrent" ? "Torrent" : "Direct",
        r.format, r.size, r.language, r.seeders != null ? `${r.seeders} seeder${r.seeders === 1 ? "" : "s"}` : "", r.posted].filter(Boolean);
      return `<div class="rel ${r.language_ok === false ? "other-lang" : ""}" ${r.language_ok === false ? 'title="Not in your languages: automatic search skips it, but you can still grab it"' : ""}>
        <div class="rel-main"><b title="${esc(r.release || r.title)}">${esc(r.release || r.title)}</b>
          <small>${facts.map(esc).join(" · ")}</small></div>
        <span class="match ${quality}" title="How closely it matches the title and author">${r.match}%</span>
        <button class="btn sm" data-grab="${i}" ${r.usable ? "" : `disabled title="Set up a download client for ${r.protocol === "usenet" ? "Usenet" : "torrents"} first"`}>Grab</button>
      </div>`;
    }).join("")}
  </div>`;
}

async function showReleases(lib) {
  const box = $("#drawer-releases");
  if (!box) return;
  box.innerHTML = `<div class="releases"><h4>Releases</h4><p class="muted"><span class="spinner"></span> Searching your indexers… this can take a minute.</p></div>`;
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
  try {
    const res = await api(`/api/library/${lib.id}/releases`);
    if (state.drawer?.bookId !== lib.id && drawerBook()?.id !== lib.id) return;
    state.drawer.releases = res.items;
    box.innerHTML = releasesHTML(res);
  } catch (e) {
    box.innerHTML = `<div class="releases"><h4>Releases</h4><div class="state failed">${esc(e.message)}</div></div>`;
  }
}

// ---- correcting a book's details ----

function editDetails(lib) {
  const head = $("#meta-head", drawer);
  if (!lib || !head || $("#meta-edit", head)) return;
  const shown = head.innerHTML;
  head.innerHTML = `<form class="meta-edit" id="meta-edit" autocomplete="off">
      <h4>Edit details</h4>
      <div class="field"><label for="me-title">Title</label><input class="input" id="me-title" name="title" value="${esc(lib.title)}" required /></div>
      <div class="field"><label for="me-author">Author</label><input class="input" id="me-author" name="author" value="${esc(lib.author || "")}" /></div>
      <div class="field"><label for="me-narrators">Narrated by</label><input class="input" id="me-narrators" name="narrators" value="${esc((lib.narrators || []).join(", "))}" />
        <span class="hint">Separate several names with commas.</span></div>
      <p class="hint">This changes what Narratarr shows, and how it matches authors you follow. Your files and folders stay as they are.</p>
      <div class="cta"><button class="btn primary" type="submit">Save</button><button class="btn ghost" type="button" data-cancel>Cancel</button></div>
    </form>`;
  const form = $("#meta-edit", head);
  const f = form.elements;
  f.title.focus();
  $("[data-cancel]", form).onclick = () => { head.innerHTML = shown; };
  form.onsubmit = async (e) => {
    e.preventDefault();
    const btn = $('[type="submit"]', form);
    spinBtn(btn, true);
    try {
      const body = {
        title: f.title.value,
        author: f.author.value,
        narrators: f.narrators.value.split(/[,;]/).map((n) => n.trim()).filter(Boolean),
      };
      const saved = await api(`/api/library/${lib.id}`, { method: "PATCH", body });
      await refreshLibrary();
      toast("Details saved", { kind: "ok" });
      openDrawer({ bookId: saved.id });
    } catch (err) {
      toast(err.message, { kind: "err" });
      spinBtn(btn, false);
    }
  };
}

// ---- following from the drawer ----

function followHTML(b) {
  if (!(b.source === "audible" || b.asin) || asksFirst()) return "";
  const series = (b.series || [])[0];
  const author = (b.author || "").split(",")[0].trim();
  const followed = (kind, key) => state.follows.find((f) => f.kind === kind && (kind === "series" ? f.asin === key : f.name === key));
  const btn = (kind, key, label, data) => {
    const f = followed(kind, key);
    return f ? `<a class="btn sm ghost" href="#/authors/${f.id}">${I.check} Following ${esc(label)}</a>`
      : `<button class="btn sm ghost" data-act="follow-${kind}" ${data}>${I.plus} Follow ${esc(label)}</button>`;
  };
  return `<div class="follow-row">
    ${author ? btn("author", author, author, `data-name="${esc(author)}"`) : ""}
    ${series?.asin ? btn("series", series.asin, series.title, `data-asin="${esc(series.asin)}" data-name="${esc(series.title)}"`) : ""}
  </div>`;
}

async function followFromDrawer(kind, el) {
  let asin = el.dataset.asin, name = el.dataset.name;
  if (kind === "author") {
    const found = await api(`/api/authors/search?${new URLSearchParams({ q: name })}`);
    const hit = found.find((a) => a.name.toLowerCase() === name.toLowerCase()) || found[0];
    if (!hit) throw new Error(`Couldn't find ${name} in Audible's catalog.`);
    asin = hit.asin;
    name = hit.name;
  }
  return confirmFollow({ kind, asin, name });
}

async function confirmFollow({ kind, asin, name }) {
  const res = await modal({
    title: `Follow ${name}?`,
    body: kind === "author"
      ? "Their new books will be added to Wanted and downloaded as soon as they show up on your indexers."
      : "New books in this series will be added to Wanted and downloaded as soon as they show up on your indexers.",
    confirm: "Follow",
    extra: `<label class="switch"><input type="checkbox" name="existing"><i></i>Also add the books already out to Wanted</label>`,
  });
  if (!res) return null;
  const f = await api("/api/follows", { method: "POST", body: { kind, asin, name, add_existing: !!res.existing } });
  await Promise.all([refreshFollows(), refreshLibrary()]);
  toast(`Following ${f.name}${f.added ? ` · ${f.added} book${f.added === 1 ? "" : "s"} added to Wanted` : ""}`, { kind: "ok", cover: f.image });
  return f;
}

function updateDrawerLive() {
  const live = $("#drawer-live");
  if (!live || !state.drawer || state.drawer.loading) return; // keep the spinner until details arrive
  const html = liveHTML(drawerBook());
  if (live.dataset.sig === html) return;
  live.dataset.sig = html;
  live.innerHTML = html;
}

// Where a request is: Asked → Approval → Downloading → In your library.
function reqStepsHTML(lib) {
  const at = lib.status === "requested" ? 1 : lib.status === "completed" ? 4 : 2;
  return `<ol class="req-steps" aria-label="Where this request is">${["Asked", "Approval", "Downloading", "In your library"].map((label, i) =>
    `<li class="${i < at ? "done" : i === at ? "now" : ""}"${i === at ? ' aria-current="step"' : ""}><i aria-hidden="true"></i>${label}</li>`).join("")}</ol>`;
}

// The rest of the series, as far as the library knows it.
function seriesShelfHTML(b) {
  const ser = (b.series || [])[0];
  if (!ser?.title) return "";
  const same = (x) => (x.series || []).some((s) => (ser.asin && s.asin === ser.asin) || s.title === ser.title);
  const seq = (x) => parseFloat((x.series || []).find((s) => s.title === ser.title || s.asin === ser.asin)?.sequence) || 999;
  const here = b.id || b.source_url || b.url;
  const books = state.library.filter((x) => same(x) && !isEbook(x)).sort((a, c) => seq(a) - seq(c));
  if (books.length < (books.some((x) => x.id === b.id || x.source_url === here) ? 2 : 1)) return "";
  return `<div class="series-shelf"><h4>${esc(ser.title)}</h4>${books.map((x) => {
    const mineHere = x.id === b.id || x.source_url === here;
    const n = seq(x);
    return `<button type="button" class="series-row ${mineHere ? "here" : ""}" data-open-book="${x.id}">${coverHTML(x)}
      <span><b>${esc(x.title)}</b><small>${n !== 999 ? `Book ${n}` : "In the series"}${mineHere ? " · this one" : ""}</small></span>
      ${stBadge(x).replace("st-badge", "st-badge inline")}</button>`;
  }).join("")}</div>`;
}

function drawerHTML(b, loading) {
  const narrators = b.narrators || [];
  const pills = [
    b.length && [/pages$/.test(b.length) ? I.book : I.clock, b.length],
    b.format && [I.file, b.format],
    b.bitrate && b.bitrate !== "?" && [I.wave, b.bitrate],
    b.size && [I.disc, b.size],
    isEbook(b) && [I.book, "Ebook"],
    b.language && [I.globe, b.language],
    isDirect(b.source) && [I.check, "Free & legal"],
    releaseLabel(b.released) && [I.clock, releaseLabel(b.released)],
    b.protocol === "usenet" && [I.down, "Usenet"],
    b.protocol === "torrent" && [I.down, b.seeders != null ? `Torrent · ${b.seeders} seeder${b.seeders === 1 ? "" : "s"}` : "Torrent"],
  ].filter(Boolean);
  const desc = (b.description || "").trim();
  return `
    <div class="backdrop"><div></div></div>
    <button class="btn icon close" data-close aria-label="Close">${I.x}</button>
    <div class="drawer-body">
      ${coverHTML(b, "hero-cover")}
      <div id="meta-head">
      <h2>${esc(b.title)}</h2>
      <div class="by">by <strong>${esc(b.author || "Unknown author")}</strong></div>
      ${narrators.length ? `<div class="narr">Narrated by ${esc(narrators.slice(0, 3).join(", "))}${narrators.length > 3 ? ` +${narrators.length - 3} more` : ""}</div>` : ""}
      </div>
      ${pills.length ? `<div class="pills">${pills.map(([ic, t]) => `<span class="pill">${ic}${esc(t)}</span>`).join("")}</div>` : ""}
      <div id="drawer-live">${loading ? '<div class="cta"><span class="spinner" style="color:var(--text-3)"></span></div>' : ""}</div>
      ${!loading ? followHTML(b) : ""}
      <div id="drawer-releases"></div>
      ${!loading ? seriesShelfHTML(b) : ""}
      ${desc ? `<div class="about"><h4>About this book</h4><p class="${desc.length > 520 ? "clamped" : ""}">${esc(desc)}</p>${desc.length > 520 ? '<button class="more" data-more>Read more</button>' : ""}</div>` : ""}
      ${!loading ? `<dl>
        ${(b.series || []).length ? `<dt>Series</dt><dd>${b.series.map((x) => esc(x.sequence ? `${x.title} #${x.sequence}` : x.title)).join(" · ")}</dd>` : ""}
        ${(b.categories || []).length && b.source !== "audible" ? `<dt>${b.source === "openlibrary" ? "Subjects" : "Genres"}</dt><dd>${esc(b.categories.join(", "))}</dd>` : ""}
        ${b.released ? `<dt>Released</dt><dd>${esc(releaseLabel(b.released, true))}</dd>` : ""}
        ${b.posted && !b.released ? `<dt>${b.source === "openlibrary" ? "First published" : "Posted"}</dt><dd>${esc(b.posted)}</dd>` : ""}
        ${b.info_hash ? `<dt>Info hash</dt><dd class="mono">${esc(b.info_hash)}</dd>` : ""}
        ${(b.files || []).length ? `<dt>Files</dt><dd>${b.files.length} ${esc(b.format || "audio")} file${b.files.length === 1 ? "" : "s"}${b.size ? ` · ${esc(b.size)}` : ""}</dd>` : ""}
        ${b.release ? `<dt>Release</dt><dd class="mono">${esc(b.release)}</dd>` : ""}
        ${b.indexer ? `<dt>Indexer</dt><dd>${esc(b.indexer)}</dd>` : ""}
        <dt>Source</dt><dd>${b.source === "local" ? `Your files${b.import_mode === "inplace" ? " (left in place)" : ""}`
          : /^https?:/.test(b.url || b.source_url || "")
          ? `<a href="${esc((b.url || b.source_url).split("#")[0])}" target="_blank" rel="noopener noreferrer">${esc(sourceInfo(b.source).label)}${isDirect(b.source) ? " on Internet Archive" : ""} ↗</a>`
          : esc(sourceInfo(b.source).label)}</dd>
      </dl>` : ""}
    </div>`;
}

function paintDrawer(b, loading) {
  drawer.innerHTML = drawerHTML(b, loading);
  const bg = $(".backdrop div", drawer);
  if (/^https?:\/\//.test(b.cover_url || "")) bg.style.backgroundImage = `url(${JSON.stringify(b.cover_url)})`;
  else bg.style.background = `hsl(${hue(b.title + b.author)} 60% 40%)`;
  if (!loading) updateDrawerLive();
}

async function openDrawer({ url, bookId, preview, source, format }) {
  state.drawer = { url, bookId, source, format, protocol: preview?.protocol || "" };
  drawer.classList.add("open");
  scrim.classList.add("open");
  document.body.style.overflow = "hidden";
  drawer.scrollTop = 0;

  const lib = drawerBook();
  if (lib) {
    // Library books already have everything we need.
    state.drawer.url = lib.source_url;
    state.drawer.source = lib.source;
    state.drawer.protocol = lib.protocol;
    paintDrawer(lib, false);
    $("[data-close]", drawer)?.focus({ preventScroll: true });
    return;
  }

  state.drawer.loading = true;
  paintDrawer({ title: "Loading…", author: "", ...preview, source }, true);
  $("[data-close]", drawer)?.focus({ preventScroll: true });
  try {
    const details = await api(`/api/details?${new URLSearchParams({ url, source: source || "" })}`);
    if (state.drawer?.url !== url) return;
    state.drawer.loading = false;
    // Keep listing facts (like size) when the book page leaves them blank.
    const merged = { ...preview };
    for (const [k, v] of Object.entries(details)) if (v !== "" && !(Array.isArray(v) && !v.length)) merged[k] = v;
    merged.library = details.library;
    state.drawer.details = merged;
    paintDrawer(merged, false);
  } catch (e) {
    if (state.drawer?.url !== url) return;
    state.drawer.loading = false;
    $("#drawer-live").innerHTML = `<div class="state failed">${esc(e.message)}</div>`;
  }
}

drawer.onclick = async (e) => {
  if (e.target.closest("[data-close]")) return closeDrawer();
  if (e.target.closest("[data-more]")) {
    const p = $(".about p", drawer);
    p.classList.remove("clamped");
    e.target.remove();
    return;
  }
  const grabBtn = e.target.closest("[data-grab]");
  if (grabBtn && state.drawer?.releases) {
    const r = state.drawer.releases[Number(grabBtn.dataset.grab)];
    const lib = drawerBook();
    spinBtn(grabBtn, true);
    try {
      const got = await api(`/api/library/${lib.id}/grab`, { method: "POST", body: { url: r.url, source: r.source } });
      $("#drawer-releases").innerHTML = "";
      await refreshLibrary();
      toast(got.status === "requested" ? `Asked for ${r.release || r.title}. It’ll download once an admin approves it.`
        : `Grabbed ${r.release || r.title}`, { kind: "ok", cover: lib.cover_url });
    } catch (err) {
      toast(err.message, { kind: "err" });
      spinBtn(grabBtn, false);
    }
    return;
  }
  const pick = e.target.closest("[data-format]");
  if (pick && state.drawer) {
    state.format = pick.dataset.format;
    try { localStorage.setItem("narratarr.format", state.format); } catch { /* private mode */ }
    return updateDrawerLive();
  }
  const open = e.target.closest("[data-open-book]");
  if (open) return openDrawer({ bookId: Number(open.dataset.openBook) });
  const btn = e.target.closest("[data-act]");
  if (!btn || !state.drawer) return;
  const act = btn.dataset.act;
  if (act === "releases") return showReleases(drawerBook());
  if (act === "edit") return editDetails(drawerBook());
  spinBtn(btn, true);
  try {
    if (act === "add" || act === "want") {
      const url = state.drawer.url;
      if (act === "want") {
        const saved = await api("/api/library", { method: "POST", body: { url, download: false, source: state.drawer.source, ...(state.drawer.format ? { format: state.drawer.format } : {}) } });
        await refreshLibrary();
        toast(saved.status === "requested" ? "Asked an admin to approve it" : "Saved to Wanted", { kind: "ok" });
      } else {
        await addBook(url, { cover: $(".hero-cover img:not(.blur)", drawer)?.src, source: state.drawer.source, protocol: state.drawer.protocol, format: state.drawer.format });
      }
    } else if (act === "other-format") {
      const lib = drawerBook();
      const other = await api(`/api/library/${lib.id}/other-format`, { method: "POST" });
      await refreshLibrary();
      toast(other.status === "requested" ? `Asked for the ${isEbook(other) ? "ebook" : "audiobook"} of ${other.title} too`
        : `Looking for the ${isEbook(other) ? "ebook" : "audiobook"} of ${other.title} too`, { kind: "ok", cover: other.cover_url });
    } else if (act === "follow-author" || act === "follow-series") {
      spinBtn(btn, false);
      if (await followFromDrawer(act.slice(7), btn)) {
        const lib = drawerBook();
        const fr = $(".follow-row", drawer);
        if (fr) fr.outerHTML = followHTML(lib || { ...state.drawer.details, source: state.drawer.source });
      }
    } else if (act === "play") {
      spinBtn(btn, false);
      await playBook(drawerBook()?.id);
    } else if (act === "toggle-finished") {
      const lib = drawerBook();
      await api(`/api/library/${lib.id}/progress`, { method: "PUT", body: { position: 0, finished: !lib.finished } });
      if (player.book?.id === lib.id) { player.book.finished = !lib.finished; player.render(); }
      await refreshLibrary();
    } else {
      const lib = drawerBook();
      if (lib && (await bookAction(lib.id, act)) && act === "remove") closeDrawer();
    }
  } catch (err) {
    toast(err.message, { kind: "err" });
  }
  spinBtn(btn, false);
  updateDrawerLive();
};

// ---------------------------------------------------------------------------
// boot

document.addEventListener("keydown", (e) => {
  if (document.body.classList.contains("reading")) return; // the reader has the keys while a book is open
  if (e.key === "Escape" && state.drawer) closeDrawer();
  const typing = e.target.matches("input, textarea, select");
  if (!typing && (e.key === "/" || (e.key === "k" && (e.metaKey || e.ctrlKey)))) {
    e.preventDefault();
    if (!$("#q")) location.hash = "#/discover";
    requestAnimationFrame(() => $("#q")?.focus());
  }
});

// ---------------------------------------------------------------------------
// Accounts: the sign-in / first-run screen, and the account menu

function showGate(status) {
  const setup = status.setup;
  document.body.classList.add("gated");
  const gate = document.createElement("div");
  gate.className = "gate";
  gate.innerHTML = `
    <form class="gate-card" autocomplete="on" novalidate>
      <img src="/static/icon.svg" alt="" width="56" height="56" />
      <h1>${setup ? "Welcome to Narrat<em>arr</em>" : "Sign in"}</h1>
      <p>${setup
        ? "Create the admin account. It runs Narratarr: settings, downloads, and who else can sign in. Add family and friends as listeners afterwards."
        : "Narratarr needs to know who’s listening."}</p>
      <div class="field">
        <label for="g-user">Username</label>
        <input class="input" id="g-user" name="username" autocomplete="username" autocapitalize="off" spellcheck="false" required />
      </div>
      <div class="field">
        <label for="g-pass">Password</label>
        <input class="input" id="g-pass" name="password" type="password" autocomplete="${setup ? "new-password" : "current-password"}" required />
        ${setup ? `<span class="hint">At least 8 characters.</span>` : ""}
      </div>
      ${setup ? `<div class="field">
        <label for="g-again">Password again</label>
        <input class="input" id="g-again" type="password" autocomplete="new-password" required />
      </div>` : ""}
      ${status.setup_code_needed ? `<div class="field">
        <label for="g-code">Setup code</label>
        <input class="input mono" id="g-code" name="code" inputmode="numeric" placeholder="1234-5678" autocomplete="off" />
        <span class="hint">You’re not on Narratarr’s network, so it asks for the code it printed when it started. Find it with <code>docker logs narratarr</code>, or in <code>~/.narratarr/narratarr.log</code>.</span>
      </div>` : ""}
      <div class="form-error" id="g-error" role="alert"></div>
      <button class="btn primary" type="submit">${setup ? `${I.check} Create admin account` : "Sign in"}</button>
    </form>`;
  document.body.append(gate);
  const form = $("form", gate);
  $("#g-user", gate).focus();
  form.onsubmit = async (e) => {
    e.preventDefault();
    const err = $("#g-error", gate);
    const body = { username: form.username.value.trim(), password: form.password.value, code: form.code?.value || "" };
    if (setup && body.password !== $("#g-again", gate).value) { err.textContent = "The passwords don’t match."; return; }
    const btn = $('button[type="submit"]', gate);
    spinBtn(btn, true);
    try {
      await api(setup ? "/api/auth/setup" : "/api/auth/login", { method: "POST", body });
      location.reload();
    } catch (ex) {
      err.textContent = ex.message;
      spinBtn(btn, false);
    }
  };
}

function renderAccount() {
  const u = state.user;
  const btn = $("#account");
  btn.hidden = false;
  $("#account-avatar").textContent = u.username.slice(0, 1);
  $("#account-name").textContent = u.username;
  $("#account-role").textContent = u.role === "admin" ? "Admin" : "Listener";
  btn.onclick = openAccount;
}

// A modal with fields that stays open until its action works, showing any error inside it.
function formModal({ title, body = "", fields, confirm, danger = false, onSubmit, extra = "" }) {
  const scrim = $("#modal");
  scrim.hidden = false;
  scrim.innerHTML = `
    <form class="modal" role="dialog" aria-modal="true" aria-labelledby="fm-title" novalidate>
      <h3 id="fm-title">${esc(title)}</h3>
      ${body ? `<p>${body}</p>` : ""}
      ${fields}
      <div class="form-error" id="fm-error" role="alert" style="margin-top:14px"></div>
      <div class="actions">
        ${extra}
        <button class="btn ghost" type="button" data-act="cancel">Cancel</button>
        <button class="btn ${danger ? "danger solid" : "primary"}" type="submit">${confirm}</button>
      </div>
    </form>`;
  const form = $("form", scrim);
  const close = () => { scrim.hidden = true; scrim.innerHTML = ""; document.removeEventListener("keydown", onKey, true); };
  const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
  document.addEventListener("keydown", onKey, true);
  scrim.onclick = (e) => { if (e.target === scrim || e.target.closest('[data-act="cancel"]')) close(); };
  form.onsubmit = async (e) => {
    e.preventDefault();
    const btn = $('button[type="submit"]', form);
    spinBtn(btn, true);
    try {
      if ((await onSubmit(form)) !== false) close();
    } catch (ex) {
      $("#fm-error", form).textContent = ex.message;
    }
    spinBtn(btn, false);
  };
  $("input, select", form)?.focus();
  return form;
}

const passwordFields = (current = false) => `
  ${current ? `<div class="field"><label for="pw-current">Current password</label><input class="input" id="pw-current" name="current" type="password" autocomplete="current-password" /></div>` : ""}
  <div class="field"><label for="pw-new">New password</label><input class="input" id="pw-new" name="password" type="password" autocomplete="new-password" /><span class="hint">At least 8 characters.</span></div>
  <div class="field"><label for="pw-again">New password again</label><input class="input" id="pw-again" name="again" type="password" autocomplete="new-password" /></div>`;

function matching(form) {
  if (form.password.value !== form.again.value) throw new Error("The new passwords don’t match.");
}

function openAccount() {
  const u = state.user;
  const form = formModal({
    title: u.username,
    body: u.role === "admin"
      ? "Admin: runs settings, downloads and who can sign in."
      : "Listener: browses, requests and listens. Your place in every book is your own.",
    fields: `<a class="community-row" href="${DISCORD_URL}" target="_blank" rel="noopener noreferrer">${I.chat}
        <span><b>Join the Narratarr Discord</b><small>Help, ideas and news about new versions</small></span><span class="go" aria-hidden="true">↗</span></a>
      <div class="apps-block" id="my-apps" hidden>
        <h4>Apps signed in</h4>
        <div class="app-list" id="app-list"></div>
        <span class="hint">Apps like Fable Frog sign in with your account to request books. Disconnecting one stops it at once; changing your password disconnects them all.</span>
      </div>
      <h4 style="margin:22px 0 0;font-size:14px">Change your password</h4>${passwordFields(true)}`,
    confirm: "Change password",
    extra: `<button class="btn ghost danger" type="button" id="sign-out" style="margin-right:auto">Sign out</button>`,
    onSubmit: async (f) => {
      matching(f);
      await api("/api/auth/password", { method: "PUT", body: { current: f.current.value, password: f.password.value } });
      toast("Password changed. You’re signed out on your other devices.", { kind: "ok" });
    },
  });
  $("#sign-out", form).onclick = async () => {
    await api("/api/auth/logout", { method: "POST" }).catch(() => {});
    location.reload();
  };
  renderApps(form);
}

// Apps signed in to this account through the API: what they are, when they were last used, and a way out.
async function renderApps(form) {
  const block = $("#my-apps", form);
  let apps;
  try { apps = await api("/api/auth/apps"); } catch { return; }
  if (!block.isConnected) return;
  block.hidden = !apps.length;
  $("#app-list", form).innerHTML = apps.map((a) => `
    <div class="app-row" title="Signed in ${esc(new Date(a.created_at * 1000).toLocaleString())}">
      <span class="app-icon">${I.headphones}</span>
      <span class="app-who"><b>${esc(a.name)}</b><small>Used ${esc(ago(a.last_used_at))}${a.last_ip ? ` from ${esc(a.last_ip)}` : ""}</small></span>
      <button class="btn sm ghost danger" type="button" data-revoke="${a.id}">Disconnect</button>
    </div>`).join("");
  $$("[data-revoke]", form).forEach((b) => {
    b.onclick = async () => {
      spinBtn(b, true);
      try {
        await api(`/api/auth/apps/${b.dataset.revoke}`, { method: "DELETE" });
        toast("Disconnected. That app has to sign in again to use your account.", { kind: "ok" });
        renderApps(form);
      } catch (e) {
        toast(e.message, { kind: "err" });
        spinBtn(b, false);
      }
    };
  });
}

window.addEventListener("narratarr:signed-out", () => {
  if (!document.body.classList.contains("gated")) location.reload();
});

async function boot() {
  let status;
  try {
    status = await api("/api/auth/status");
  } catch (e) {
    toast(`Couldn’t reach Narratarr: ${e.message}`, { kind: "err" });
    return;
  }
  if (status.setup || !status.user) return showGate(status);
  state.user = status.user;
  document.body.classList.toggle("listener", state.user.role !== "admin");
  renderAccount();
  try {
    [state.settings, state.sources, state.library, state.follows] = await Promise.all([
      api("/api/settings"),
      api("/api/sources"),
      api("/api/library"),
      api("/api/follows").catch(() => []),
    ]);
  } catch (e) {
    toast(`Couldn’t reach Narratarr: ${e.message}`, { kind: "err" });
  }
  state.libraryKey = state.library.map((b) => `${b.id}:${b.status}`).join("|");
  document.body.classList.toggle("asks-first", asksFirst());
  updateBadges();
  updateRdPill();
  window.addEventListener("hashchange", route);
  route();
  player.onChange = () => updateDrawerLive();
  reader.onChange = () => { if (!reader.book) refreshLibrary(); }; // the shelf and cards show how far you got
  player.restore(state.library);
  setInterval(refreshLibrary, 2000);
  checkUpdates();
  setInterval(checkUpdates, 6 * 60 * 60 * 1000);
}

boot();
