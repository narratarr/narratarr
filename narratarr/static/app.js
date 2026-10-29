// Narratarr web UI — no build step, just modules.

import { $, $$, esc, I, api, hue, coverHTML, firstAuthor, fmtBytes, fmtLeft, ago, toast } from "./util.js";
import { player } from "./player.js";
import { openAbsGuide } from "./guide.js";


const ACTIVE = ["queued", "resolving", "caching", "downloading", "organizing"];
const STATUS = {
  wanted: "Wanted",
  queued: "Queued",
  resolving: "Resolving",
  caching: "On Real-Debrid",
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
const CLIENTS = { sabnzbd: "SABnzbd", nzbget: "NZBGet", realdebrid: "Real-Debrid", qbittorrent: "qBittorrent", transmission: "Transmission", deluge: "Deluge" };


// ---------------------------------------------------------------------------
// state + api

const state = {
  settings: null,
  sources: [],
  library: [],
  libraryKey: "",
  discover: { q: "", category: "", page: 0, total: 1, items: [], loading: false, error: "", token: 0, dym: "", lang: "", fmt: "", sort: "", source: lastSource() },
  libFilter: "all",
  libSearch: "",
  drawer: null, // { url, bookId }
  view: null,
  follows: [],
  updates: null,
};


const byUrl = () => new Map(state.library.map((b) => [b.source_url, b]));

// ---------------------------------------------------------------------------
// helpers


function statusTag(status) {
  return `<span class="status-tag s-${status}"><i></i>${esc(STATUS[status] || status)}</span>`;
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

function lastSource() {
  try { return localStorage.getItem("narratarr.source") || localStorage.getItem("audiobookeer.source") || "librivox"; } catch { return "librivox"; }
}
const FALLBACK_SOURCES = {
  local: { label: "Your files", delivery: "local" },
  audible: { label: "Audible catalog", delivery: "wanted" },
};
const sourceInfo = (name) => state.sources.find((s) => s.name === name)
  || { name, label: name, delivery: "torrent", categories: [], ...FALLBACK_SOURCES[name] };
const isDirect = (name) => sourceInfo(name).delivery === "direct";

// Which download client a book needs that isn't set up yet ("" when it can download now).
function missingClient(source, protocol) {
  if (isDirect(source) || source === "audible" || source === "local") return "";
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
    : b.download_client || (st.torrent_client !== "realdebrid" ? st.torrent_client : "");
  if (client && client !== "realdebrid") return [["resolving", "Resolve"], ["downloading", CLIENTS[client]], ["organizing", "Organize"]];
  return PHASES;
}

// ---------------------------------------------------------------------------
// library actions (shared by every view)

async function addBook(url, { cover = "", title = "", source = "audiobookbay", protocol = "" } = {}) {
  if (source === "audible") {
    // Audible is only the catalog: the book goes to Wanted and Narratarr looks for a release.
    const book = await api("/api/library", { method: "POST", body: { url, download: true, source } });
    await refreshLibrary();
    toast(book.status === "wanted" ? `${book.title} is on your Wanted list. Looking for it now…` : `${book.title} is already in your library`,
      { kind: "ok", cover: cover || book.cover_url });
    return book;
  }
  // Free sources download directly; torrents and Usenet releases need a download client.
  const missing = missingClient(source, protocol);
  const download = !missing;
  const book = await api("/api/library", { method: "POST", body: { url, download, source } });
  await refreshLibrary();
  toast(
    download ? `${book.title || title} is on its way` : `Saved ${book.title || title} to Wanted. Connect ${missing} in Settings to download.`,
    { kind: "ok", cover: cover || book.cover_url }
  );
  return book;
}

async function bookAction(id, action) {
  if (action === "remove") {
    const book = state.library.find((b) => b.id === id);
    const res = await modal({
      title: "Remove from library?",
      body: `“${book?.title}” will be removed from Narratarr.`,
      confirm: "Remove",
      danger: true,
      extra: book?.path
        ? `<label class="switch"><input type="checkbox" name="files"><i></i>Also delete its files on disk${book.source === "local" ? " (the ones you imported)" : ""}</label>`
        : "",
    });
    if (!res) return false;
    if (player.book?.id === id) player.close();
    const out = await api(`/api/library/${id}?delete_files=${!!res.files}`, { method: "DELETE" });
    toast(out?.kept ? `Removed. Its files were kept at ${out.kept}` : "Removed from library", out?.kept ? { kind: "ok" } : {});
  } else {
    const book = state.library.find((b) => b.id === id);
    const missing = action === "download" && book && missingClient(book.source, book.protocol);
    if (missing) {
      toast(`Connect ${missing} in Settings first.`, { kind: "err" });
      return false;
    }
    await api(`/api/library/${id}/${action}`, { method: "POST" });
  }
  await refreshLibrary();
  return true;
}

// ---------------------------------------------------------------------------
// polling

async function refreshLibrary() {
  try {
    state.library = await api("/api/library");
  } catch {
    return;
  }
  const active = state.library.filter((b) => ACTIVE.includes(b.status)).length;
  const badge = $("#activity-badge");
  badge.hidden = !active;
  badge.textContent = active;

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
  const View = views[name] || Discover;
  $$("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === name));
  state.view?.destroy?.();
  const root = $("#view");
  root.classList.remove("enter");
  void root.offsetWidth;
  root.classList.add("enter");
  state.view = View(root, param);
  window.scrollTo({ top: 0 });
}

// ---------------------------------------------------------------------------
// Discover

function cardHTML(item, lib, i = 0) {
  const h = hue(item.title + item.author);
  const facts = [
    item.format, item.size, item.language && item.language !== "English" ? item.language : "",
    item.protocol === "usenet" ? "Usenet" : item.seeders != null ? `${item.seeders} seeder${item.seeders === 1 ? "" : "s"}` : "",
    item.source === "audible" ? shortLength(item.length) : "",
    item.source === "audible" ? releaseLabel(item.released || item.posted) : "",
  ].filter(Boolean);
  const active = lib && ACTIVE.includes(lib.status);
  return `
    <article class="card" tabindex="0" data-url="${esc(item.url)}" ${lib ? `data-id="${lib.id}"` : ""} style="--glow:hsl(${h} 80% 55%);animation-delay:${Math.min(i, 12) * 35}ms">
      <div class="cover-wrap">
        ${coverHTML(item)}
        <span class="tag-slot">${lib ? statusTag(lib.status) : ""}</span>
        ${active ? `<div class="bar"><i style="width:${lib.progress || 0}%"></i></div>` : ""}
        ${quickButton(item, lib)}
      </div>
      <div class="meta">
        <div class="title">${esc(item.title)}</div>
        <div class="author">${esc(item.author || (item.indexer ? `via ${item.indexer}` : "Unknown author"))}</div>
        ${facts.length ? `<div class="facts">${facts.map((f) => `<span>${esc(f)}</span>`).join("")}</div>` : ""}
      </div>
    </article>`;
}

const shortLength = (len) => (len || "").replace(/ hours?/, "h").replace(/ and /, " ").replace(/ minutes?/, "m");

function releaseLabel(day, always = false) {
  const t = Date.parse(day || "");
  if (!t) return "";
  const when = new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  return t > Date.now() ? `Out ${when}` : always ? when : "";
}

function quickButton(item, lib) {
  if (!lib && item.source === "audible") return `<button class="quick" data-quick aria-label="Add ${esc(item.title)} to Wanted" title="Add to Wanted">${I.bookmark}</button>`;
  if (!lib) return `<button class="quick" data-quick aria-label="Download ${esc(item.title)}" title="Download">${I.plus}</button>`;
  if (lib.status === "completed") return `<button class="quick play-quick" data-play="${lib.id}" aria-label="Play ${esc(item.title)}" title="Play">${I.play}</button>`;
  return "";
}

const skeletons = (n) => Array.from({ length: n }, () => '<div class="card skeleton"><div class="cover-wrap"></div><div class="line"></div><div class="line short"></div></div>').join("");

function Discover(root) {
  const d = state.discover;
  if (!state.sources.some((s) => s.name === d.source)) d.source = "librivox";
  const src = sourceInfo(d.source);
  const st = state.settings;
  const needsClient = st && (src.delivery === "torrent" ? !st.torrent_configured
    : src.delivery === "indexer" ? !st.torrent_configured && !st.usenet_configured : false);
  const greeting = (() => {
    const h = new Date().getHours();
    return h < 5 ? "Late night listening" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
  })();

  root.innerHTML = `
    <header class="hero">
      <div>
        <div class="eyebrow">${greeting}</div>
        <h1 class="page-title">What are we <em>listening</em> to next?</h1>
      </div>
      <div class="sources" id="sources" role="tablist" aria-label="Where to look">
        ${state.sources.map((s) => `
          <button role="tab" aria-selected="${s.name === d.source}" class="source ${s.name === d.source ? "on" : ""}" data-src="${esc(s.name)}">
            <b>${esc(s.label)}</b><small>${esc({ direct: "Free · direct download", indexer: "Your indexers", wanted: "Add to Wanted" }[s.delivery] || "Torrents")}</small>
          </button>`).join("")}
      </div>
      <div class="search-wrap">
        <form class="search" id="search-form" role="search">
          ${I.search}
          <input id="q" type="search" placeholder="Search titles, authors, narrators… typos are fine" autocomplete="off" value="${esc(d.q)}" aria-label="Search audiobooks" aria-autocomplete="list" aria-controls="suggest" />
          <kbd>/</kbd>
          <button class="btn primary" type="submit">Search</button>
        </form>
        <div class="suggest" id="suggest" role="listbox" hidden></div>
      </div>
      <div class="chips" id="chips">
        <button class="chip ${!d.q && !d.category ? "on" : ""}" data-cat="">${esc(src.default_label || "Fresh arrivals")}</button>
        ${src.categories.map((c) => `<button class="chip ${!d.q && d.category === c.slug ? "on" : ""}" data-cat="${esc(c.slug)}">${esc(c.name)}</button>`).join("")}
      </div>
    </header>
    ${needsClient ? `
      <div class="banner">
        <div class="ico">${I.key}</div>
        <div><strong>Connect a download client to start downloading</strong><p>${src.delivery === "indexer"
          ? "Usenet results go to SABnzbd or NZBGet, torrents to Real-Debrid, qBittorrent, Transmission or Deluge. You can save books to Wanted until then."
          : "Use Real-Debrid, qBittorrent, Transmission or Deluge. You can browse and save books to Wanted until then."}</p></div>
        <a class="btn" href="#/settings">Open Settings</a>
      </div>` : ""}
    <div class="section-head"><h2 id="disc-title"></h2><div class="refine" id="refine"></div></div>
    <div id="dym"></div>
    <div class="grid" id="grid"></div>
    <div class="load-more" id="sentinel"></div>`;

  const grid = $("#grid", root);
  const sentinel = $("#sentinel", root);

  function heading() {
    const cat = src.categories.find((c) => c.slug === d.category);
    $("#disc-title", root).innerHTML = d.q
      ? `Results for <em style="font-style:italic">“${esc(d.q)}”</em> <span class="in-src">in ${esc(src.label)}</span>`
      : esc(cat ? cat.name : src.default_label || "Fresh arrivals");
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
    const sorts = [["", natural], ["newest", "Newest first"], ["largest", "Largest"], ["smallest", "Smallest"], ["title", "Title A–Z"]]
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
    let items = d.items.filter((i) => (!d.lang || i.language === d.lang) && (!d.fmt || i.format.toUpperCase() === d.fmt));
    const by = {
      newest: (a, b) => (Date.parse(b.posted) || 0) - (Date.parse(a.posted) || 0),
      largest: (a, b) => sizeOf(b.size) - sizeOf(a.size),
      smallest: (a, b) => sizeOf(a.size) - sizeOf(b.size),
      title: (a, b) => a.title.localeCompare(b.title),
    }[d.sort];
    return by ? [...items].sort(by) : items;
  }

  function renderItems() {
    const lib = byUrl();
    const items = visibleItems();
    grid.innerHTML = items.map((it, i) => cardHTML(it, lib.get(it.url), i)).join("");
    if (!items.length && !d.loading) {
      const filtered = d.items.length > 0;
      grid.innerHTML = `<div class="empty" style="grid-column:1/-1">
        <div class="art">${d.error ? I.alert : I.search}</div>
        <h3>${d.error ? "Couldn’t load books" : filtered ? "Nothing matches those filters" : "Nothing found"}</h3>
        <p>${esc(d.error || (filtered ? "Try another language or format." : "Try searching by author, or just part of the title."))}</p>
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
    if (reset) { d.items = []; d.page = 0; d.total = 1; d.error = ""; d.dym = ""; grid.innerHTML = skeletons(12); }
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
      const res = await api(`/api/discover?${params}`);
      if (token !== d.token) return;
      const seen = new Set(d.items.map((i) => i.url));
      d.items.push(...res.items.filter((i) => !seen.has(i.url)));
      d.page = res.page;
      d.total = res.total_pages;
      if (reset) d.dym = res.did_you_mean || "";
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

  $("#sources", root).onclick = (e) => {
    const b = e.target.closest("[data-src]");
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
    if (e.target.closest("[data-retry]")) return load(true);
    if (e.target.closest("[data-literal]")) {
      // Re-run without typo correction by searching the exact phrase.
      d.dym = "";
      d.items = d.items.filter((i) => i.score >= 90);
      renderItems(); heading();
      return;
    }
    const play = e.target.closest("[data-play]");
    if (play) return playBook(Number(play.dataset.play));
    const card = e.target.closest(".card[data-url]");
    if (!card) return;
    const item = d.items.find((i) => i.url === card.dataset.url);
    if (e.target.closest("[data-quick]")) {
      const btn = e.target.closest("[data-quick]");
      spinBtn(btn, true);
      try { await addBook(item.url, { cover: item.cover_url, title: item.title, source: d.source, protocol: item.protocol }); }
      catch (err) { toast(err.message, { kind: "err" }); spinBtn(btn, false); }
      return;
    }
    openDrawer({ url: item.url, preview: item, source: d.source });
  };
  root.onkeydown = (e) => {
    if (e.key === "Enter" && e.target.matches(".card")) e.target.click();
  };

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
      $$(".card[data-url]", grid).forEach((card) => {
        const b = lib.get(card.dataset.url);
        const bar = $(".bar i", card);
        if (bar && b) bar.style.width = `${b.progress || 0}%`;
      });
    },
    destroy() { io.disconnect(); },
  };
}

// ---------------------------------------------------------------------------
// Library

function libCardHTML(b, i) {
  const h = hue(b.title + b.author);
  const active = ACTIVE.includes(b.status);
  const listening = b.status === "completed" && !b.finished && b.listen_position > 30 && b.listen_duration;
  return `
    <article class="card" tabindex="0" data-id="${b.id}" style="--glow:hsl(${h} 80% 55%);animation-delay:${Math.min(i, 12) * 30}ms">
      <div class="cover-wrap">
        ${coverHTML(b)}
        ${b.status === "completed" ? "" : statusTag(b.status)}
        ${b.finished ? `<span class="finished-tag" title="Finished">${I.check}</span>` : ""}
        ${active ? `<div class="bar"><i style="width:${b.progress || 0}%"></i></div>` : ""}
        ${listening ? `<div class="listen-bar"><i style="width:${(b.listen_position / b.listen_duration) * 100}%"></i></div>` : ""}
        ${b.status === "completed" ? `<button class="quick play-quick" data-play="${b.id}" aria-label="Play ${esc(b.title)}" title="Play">${I.play}</button>` : ""}
      </div>
      <div class="meta">
        <div class="title">${esc(b.title)}</div>
        <div class="author">${esc(b.author || "Unknown author")}</div>
        <div class="facts">${(listening ? [fmtLeft(b.listen_duration - b.listen_position)] : [b.length ? b.length.replace(/ hours?/, "h").replace(/ and /, " ").replace(/ minutes?/, "m") : "", b.size]).filter(Boolean).map((f) => `<span>${esc(f)}</span>`).join("")}</div>
      </div>
    </article>`;
}

function continueListening() {
  const books = state.library
    .filter((b) => b.status === "completed" && !b.finished && b.listen_position > 30 && b.listen_duration)
    .sort((a, b) => (b.listened_at || 0) - (a.listened_at || 0))
    .slice(0, 8);
  if (!books.length) return "";
  return `
    <div class="section-head"><h2>Continue listening</h2></div>
    <div class="shelf">${books.map((b, i) => `
      <button class="resume" data-play="${b.id}" style="animation-delay:${i * 40}ms">
        ${coverHTML(b)}
        <span style="min-width:0">
          <b>${esc(b.title)}</b>
          <small>${esc(fmtLeft(b.listen_duration - b.listen_position))}</small>
          <span class="progress"><i style="width:${(b.listen_position / b.listen_duration) * 100}%"></i></span>
        </span>
        <span class="go">${I.play}</span>
      </button>`).join("")}
    </div>`;
}

async function playBook(id) {
  const book = state.library.find((b) => b.id === id);
  if (!book) return;
  if (player.book?.id === id) player.toggle();
  else await player.open(book);
}

function Library(root) {
  const filters = [
    ["all", "All", () => true],
    ["completed", "Downloaded", (b) => b.status === "completed"],
    ["active", "In progress", (b) => ACTIVE.includes(b.status)],
    ["wanted", "Wanted", (b) => b.status === "wanted"],
    ["failed", "Failed", (b) => b.status === "failed"],
  ];

  root.innerHTML = `
    <header class="page-head">
      <div>
        <div class="eyebrow">Your collection</div>
        <h1 class="page-title">The <em>Library</em></h1>
      </div>
      <button class="btn" id="import-btn">${I.down} Import books</button>
    </header>
    <div id="shelf"></div>
    <div class="stats" id="stats"></div>
    <div class="toolbar">
      <div class="segmented" id="filters"></div>
      <input class="input" id="lib-search" type="search" placeholder="Filter by title or author" value="${esc(state.libSearch)}" aria-label="Filter library" />
    </div>
    <div class="grid" id="lib-grid"></div>`;

  function render() {
    const lib = state.library;
    const count = (fn) => lib.filter(fn).length;
    $("#shelf", root).innerHTML = continueListening();
    $("#stats", root).innerHTML = [
      [lib.length, "Books in library"],
      [count((b) => b.status === "completed"), "Downloaded"],
      [count((b) => ACTIVE.includes(b.status)), "In progress"],
      [count((b) => b.status === "wanted"), "Wanted"],
    ].map(([n, l]) => `<div class="stat"><b>${n}</b><span>${l}</span></div>`).join("");

    $("#filters", root).innerHTML = filters.map(([k, label, fn]) =>
      `<button class="${state.libFilter === k ? "on" : ""}" data-f="${k}">${label} <small>${count(fn)}</small></button>`).join("");

    const fn = filters.find((f) => f[0] === state.libFilter)[2];
    const q = state.libSearch.toLowerCase();
    const books = lib.filter(fn).filter((b) => !q || `${b.title} ${b.author} ${b.narrators.join(" ")}`.toLowerCase().includes(q));
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
  root.onclick = (e) => {
    if (e.target.closest("#import-btn, [data-import]")) return openImport();
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
        const bar = $(`.card[data-id="${b.id}"] .bar i`, root);
        if (bar) bar.style.width = `${b.progress || 0}%`;
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
        ${latest ? `<a href="${esc(latest.url)}" target="_blank" rel="noopener noreferrer" style="color:var(--accent)">On GitHub ↗</a>` : ""}</div>
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
      <p>Point Narratarr at a folder on this computer. Each folder of audio files becomes one book, named from the files’ tags or the folder names.</p>
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
    ["inplace", "Leave where they are", "Adds them to Narratarr without touching the files. Single loose files are copied in."],
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
      results.innerHTML = `<div class="imp-empty">${I.search}<p>No audiobooks found in that folder. Narratarr looks for MP3, M4B, M4A, FLAC, OGG and Opus files.</p></div>`;
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
            <small>${b.in_library ? "Already in your library · " : ""}${b.files} file${b.files === 1 ? "" : "s"} · ${esc(fmtBytes(b.bytes))}${b.format ? ` · ${esc(b.format)}` : ""}</small>
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
    </header>
    ${!st.prowlarr_configured ? `
      <div class="banner">
        <div class="ico">${I.key}</div>
        <div><strong>Connect Prowlarr so Narratarr can find these books</strong><p>You can follow authors now. Their books will wait in Wanted until Prowlarr is set up.</p></div>
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
    <div class="section-head"><h2>You follow</h2><span class="muted" id="follow-count"></span></div>
    <div class="follow-grid" id="follows"></div>`;

  function render() {
    const list = state.follows;
    $("#follow-count", root).textContent = list.length ? `${list.length}` : "";
    $("#follows", root).innerHTML = list.length ? list.map((f, i) => `
      <a class="follow-card" href="#/authors/${f.id}" style="animation-delay:${Math.min(i, 12) * 35}ms">
        ${portraitHTML(f)}
        <b>${esc(f.name)}</b>
        <small>${f.kind === "author" ? "Author" : "Series"} · ${f.in_library} in library${f.wanted ? ` · ${f.wanted} wanted` : ""}</small>
      </a>`).join("") : `
      <div class="empty" style="grid-column:1/-1">
        <div class="art">${I.books}</div>
        <h3>Never miss a new release</h3>
        <p>Search for an author above, or open any book from the Audible catalog and choose Follow.</p>
      </div>`;
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
        <div class="eyebrow">${f0?.kind === "series" ? "Series" : "Author"}</div>
        <h1 class="page-title">${esc(f0?.name || "Loading…")}</h1>
        <p class="bio" id="bio"></p>
        <div class="hero-actions" id="hero-actions"></div>
      </div>
    </header>
    <div class="stats" id="a-stats"></div>
    <div class="section-head"><h2>Books</h2><span class="muted" id="a-count"></span></div>
    <div class="grid" id="a-books">${skeletons(10)}</div>`;

  const libFor = (b) => b.library && state.library.find((x) => x.id === b.library.id);
  function render() {
    if (!data) return;
    const books = data.books.map((b) => ({ ...b, source: "audible", posted: b.released }));
    const have = books.filter((b) => libFor(b));
    const missing = books.length - have.length;
    const mine = [...new Map(have.map((b) => [libFor(b).id, libFor(b)])).values()];  // one per library book
    $("#a-stats", root).innerHTML = [
      [books.length, data.kind === "author" ? "Books on Audible" : "Books in the series"],
      [mine.filter((b) => b.status === "completed").length, "In your library"],
      [mine.filter((b) => b.status !== "completed").length, "Wanted or downloading"],
      [missing, "Not added yet"],
    ].map(([n, l]) => `<div class="stat"><b>${n}</b><span>${l}</span></div>`).join("");
    $("#a-count", root).textContent = data.kind === "series" ? "In reading order" : "Newest first";
    $("#hero-actions", root).innerHTML = `
      ${missing ? `<button class="btn primary" data-act="want-all">${I.bookmark} Add ${missing} missing to Wanted</button>` : ""}
      <button class="btn ghost danger" data-act="unfollow">Unfollow</button>`;
    $("#a-books", root).innerHTML = books.length ? books.map((b, i) => cardHTML(b, libFor(b), i)).join("")
      : `<div class="empty" style="grid-column:1/-1"><h3>No books yet</h3><p>Audible doesn't list any books here yet.</p></div>`;
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
    $(".author-hero .eyebrow", root).textContent = data.kind === "series" ? "Series" : "Author";
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

function Activity(root) {
  root.innerHTML = `
    <header>
      <div class="eyebrow">Live</div>
      <h1 class="page-title">Activity</h1>
      <p class="lede">Every download, from search result to your library shelf.</p>
    </header>
    <div class="section-head"><h2>In progress</h2><span class="muted" id="act-count"></span></div>
    <div class="jobs" id="jobs"></div>
    <div id="hist-wrap"></div>`;

  function render() {
    const active = state.library.filter((b) => ACTIVE.includes(b.status))
      .sort((a, b) => ACTIVE.indexOf(b.status) - ACTIVE.indexOf(a.status) || a.updated_at - b.updated_at);
    const recent = state.library.filter((b) => ["completed", "failed"].includes(b.status))
      .sort((a, b) => (b.completed_at || b.updated_at) - (a.completed_at || a.updated_at)).slice(0, 25);

    $("#act-count", root).textContent = active.length ? `${active.length} active` : "";
    $("#jobs", root).innerHTML = active.length ? active.map((b, i) => `
      <div class="job" data-id="${b.id}" style="animation-delay:${i * 40}ms">
        ${coverHTML(b)}
        <div style="min-width:0">
          <h3>${esc(b.title)}</h3>
          <div class="by">${esc(b.author)}</div>
          ${stepsHTML(b)}
          <div class="progress ${["caching", "downloading"].includes(b.status) ? "" : "indeterminate"}"><i style="width:${b.progress || 0}%"></i></div>
          <div class="detail">${jobDetail(b)}</div>
        </div>
        <div class="actions"><button class="btn sm" data-act="stop">${I.stop} Stop</button></div>
      </div>`).join("") : `
      <div class="empty" style="padding:50px 20px">
        <div class="art">${I.headphones}</div>
        <h3>All quiet</h3>
        <p>Nothing downloading right now. Queue up your next listen from Discover.</p>
        <a class="btn" href="#/discover">Browse books</a>
      </div>`;

    $("#hist-wrap", root).innerHTML = recent.length ? `
      <div class="section-head"><h2>Recently finished</h2></div>
      <div class="history">${recent.map((b) => `
        <div class="row" data-id="${b.id}">
          ${coverHTML(b)}
          <div style="min-width:0"><div class="t">${esc(b.title)}</div><div class="s">${esc(b.status === "failed" ? b.error : b.author)}</div></div>
          <div class="when">${statusTag(b.status)}<span>${ago(b.completed_at || b.updated_at)}</span></div>
        </div>`).join("")}</div>` : "";
  }

  root.onclick = async (e) => {
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

function Settings(root) {
  const s = state.settings || {};
  root.innerHTML = `
    <header>
      <div class="eyebrow">Preferences</div>
      <h1 class="page-title">Settings</h1>
    </header>
    <form id="settings" class="settings" autocomplete="off">
      <section class="panel">
        <div><h3>Library</h3><p class="desc">Where finished audiobooks go, and how folders are named.</p></div>
        <div class="body">
          <div class="field">
            <label for="library_dir">Library folder</label>
            <input class="input mono" id="library_dir" name="library_dir" value="${esc(s.library_dir)}" />
          </div>
          <div class="field">
            <label for="folder_template">Folder layout</label>
            <input class="input mono" id="folder_template" name="folder_template" value="${esc(s.folder_template)}" />
            <span class="hint">Use <code>{author}</code>, <code>{title}</code> and <code>{narrator}</code>. Separate folders with <code>/</code>.</span>
            <div class="preview-path" id="path-preview"></div>
          </div>
          <div class="field">
            <label for="max_concurrent">Simultaneous downloads</label>
            <input class="input" id="max_concurrent" name="max_concurrent" type="number" min="1" max="8" value="${esc(s.max_concurrent)}" style="width:120px" />
          </div>
        </div>
      </section>

      <section class="panel">
        <div><h3>Users</h3><p class="desc">Who can sign in. Admins run Narratarr; listeners browse, request and listen, each with their own place in every book.</p></div>
        <div class="body">
          <div class="user-list" id="user-list"><span class="spinner"></span></div>
          <div><button class="btn" type="button" id="add-user">${I.plus} Add someone</button></div>
          <div class="field">
            <label class="switch"><input type="checkbox" name="auth_local_bypass" ${s.auth_local_bypass ? "checked" : ""}><i></i>Don’t ask for a password on my home network</label>
            <span class="hint">Phones and computers on this network open Narratarr signed in as the first admin. Leave this off if Narratarr can be reached from the internet. Anything that comes through a reverse proxy always has to sign in.</span>
          </div>
        </div>
      </section>

      <section class="panel">
        <div><h3>Updates</h3><p class="desc">You have Narratarr <b>${esc(state.updates?.current || "")}</b>.${state.updates?.update_available ? ` Version <b>${esc(state.updates.latest.version)}</b> is out.` : state.updates?.latest ? " You’re up to date." : ""}</p></div>
        <div class="body">
          <div class="field">
            <label class="switch"><input type="checkbox" name="check_updates" ${s.check_updates ? "checked" : ""}><i></i>Check for new versions</label>
            <span class="hint">One small request a day to GitHub’s releases page. Nothing about you or your library is sent.</span>
          </div>
          <div class="cta-row" style="justify-content:flex-start">
            <button class="btn" type="button" id="check-updates">${I.retry} Check now</button>
            <button class="btn" type="button" id="whats-new">${I.list} What’s new</button>
          </div>
          <div id="update-result"></div>
        </div>
      </section>

      <section class="panel">
        <div><h3>Prowlarr</h3><p class="desc">Optional. Search the indexers you’ve added to Prowlarr, and find books on your Wanted list. Results download through your Usenet or torrent client below.</p></div>
        <div class="body">
          <div class="field"><label for="prowlarr_url">Prowlarr address</label><input class="input mono" id="prowlarr_url" name="prowlarr_url" placeholder="http://localhost:9696" value="${esc(s.prowlarr_url)}" /></div>
          <div class="field">
            <label for="prowlarr_api_key">API key</label>
            <div class="input-row">
              <input class="input mono" id="prowlarr_api_key" name="prowlarr_api_key" type="password" placeholder="Prowlarr → Settings → General → API Key" value="${esc(s.prowlarr_api_key)}" />
              <button class="btn" type="button" id="test-prowlarr">Test</button>
            </div>
            <div id="prowlarr-result"></div>
          </div>
          <div class="field">
            <label for="prowlarr_categories">Categories</label>
            <input class="input mono" id="prowlarr_categories" name="prowlarr_categories" value="${esc(s.prowlarr_categories)}" style="max-width:220px" />
            <span class="hint"><code>3030</code> is Audio › Audiobook. Separate several with commas.</span>
          </div>
        </div>
      </section>

      <section class="panel">
        <div><h3>Torrents</h3><p class="desc">How torrents download. Real-Debrid fetches them for you, so nothing is shared from your computer. qBittorrent, Transmission and Deluge download on your own machine and keep seeding, which makes your IP address visible to other peers; many people run them behind a VPN.</p></div>
        <div class="body">
          <div class="field">
            <label>Download torrents with</label>
            <div class="segmented" data-choice="torrent_client" role="radiogroup" aria-label="Torrent client">
              <button type="button" role="radio" data-v="realdebrid">Real-Debrid</button>
              <button type="button" role="radio" data-v="qbittorrent">qBittorrent</button>
              <button type="button" role="radio" data-v="transmission">Transmission</button>
              <button type="button" role="radio" data-v="deluge">Deluge</button>
            </div>
            <input type="hidden" name="torrent_client" value="${esc(s.torrent_client)}" />
          </div>
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
          <div class="client-fields" data-for="torrent_client:qbittorrent">
            <div class="field"><label for="qbittorrent_url">qBittorrent Web UI address</label><input class="input mono" id="qbittorrent_url" name="qbittorrent_url" placeholder="http://localhost:8080" value="${esc(s.qbittorrent_url)}" /></div>
            <div class="input-row two">
              <div class="field"><label for="qbittorrent_username">Username</label><input class="input mono" id="qbittorrent_username" name="qbittorrent_username" value="${esc(s.qbittorrent_username)}" autocomplete="off" /></div>
              <div class="field"><label for="qbittorrent_password">Password</label><input class="input mono" id="qbittorrent_password" name="qbittorrent_password" type="password" value="${esc(s.qbittorrent_password)}" /></div>
            </div>
            <div class="field">
              <label for="qbittorrent_category">Category</label>
              <div class="input-row">
                <input class="input mono" id="qbittorrent_category" name="qbittorrent_category" value="${esc(s.qbittorrent_category)}" style="max-width:220px" />
                <button class="btn" type="button" data-test-client="qbittorrent">Test</button>
              </div>
              <div data-result="qbittorrent"></div>
            </div>
          </div>
          <div class="client-fields" data-for="torrent_client:transmission">
            <div class="field"><label for="transmission_url">Transmission address</label><input class="input mono" id="transmission_url" name="transmission_url" placeholder="http://localhost:9091" value="${esc(s.transmission_url)}" /></div>
            <div class="input-row two">
              <div class="field"><label for="transmission_username">Username</label><input class="input mono" id="transmission_username" name="transmission_username" value="${esc(s.transmission_username)}" autocomplete="off" placeholder="If you set one" /></div>
              <div class="field"><label for="transmission_password">Password</label><input class="input mono" id="transmission_password" name="transmission_password" type="password" value="${esc(s.transmission_password)}" /></div>
            </div>
            <div><button class="btn" type="button" data-test-client="transmission">Test connection</button><div data-result="transmission"></div></div>
          </div>
          <div class="client-fields" data-for="torrent_client:deluge">
            <div class="field"><label for="deluge_url">Deluge Web UI address</label><input class="input mono" id="deluge_url" name="deluge_url" placeholder="http://localhost:8112" value="${esc(s.deluge_url)}" /></div>
            <div class="field">
              <label for="deluge_password">Password</label>
              <input class="input mono" id="deluge_password" name="deluge_password" type="password" value="${esc(s.deluge_password)}" />
              <span class="hint">Deluge's web password (it has no username). Set it under Preferences &rarr; Interface &rarr; Password.</span>
            </div>
            <div><button class="btn" type="button" data-test-client="deluge">Test connection</button><div data-result="deluge"></div></div>
          </div>
        </div>
      </section>

      <section class="panel">
        <div><h3>Usenet</h3><p class="desc">Optional. Downloads the Usenet results you pick from Prowlarr, then Narratarr files them into your library.</p></div>
        <div class="body">
          <div class="field">
            <label>Download Usenet releases with</label>
            <div class="segmented" data-choice="usenet_client" role="radiogroup" aria-label="Usenet client">
              <button type="button" role="radio" data-v="sabnzbd">SABnzbd</button>
              <button type="button" role="radio" data-v="nzbget">NZBGet</button>
            </div>
            <input type="hidden" name="usenet_client" value="${esc(s.usenet_client)}" />
          </div>
          <div class="client-fields" data-for="usenet_client:sabnzbd">
            <div class="field"><label for="sabnzbd_url">SABnzbd address</label><input class="input mono" id="sabnzbd_url" name="sabnzbd_url" placeholder="http://localhost:8080" value="${esc(s.sabnzbd_url)}" /></div>
            <div class="field">
              <label for="sabnzbd_api_key">API key</label>
              <div class="input-row">
                <input class="input mono" id="sabnzbd_api_key" name="sabnzbd_api_key" type="password" placeholder="SABnzbd → Config → General → API Key" value="${esc(s.sabnzbd_api_key)}" />
                <button class="btn" type="button" data-test-client="sabnzbd">Test</button>
              </div>
              <div data-result="sabnzbd"></div>
            </div>
            <div class="field">
              <label for="sabnzbd_category">Category</label>
              <input class="input mono" id="sabnzbd_category" name="sabnzbd_category" placeholder="audiobooks" value="${esc(s.sabnzbd_category)}" style="max-width:220px" />
              <span class="hint">Optional. Must already exist in SABnzbd. Leave empty to use its default.</span>
            </div>
          </div>
          <div class="client-fields" data-for="usenet_client:nzbget">
            <div class="field"><label for="nzbget_url">NZBGet address</label><input class="input mono" id="nzbget_url" name="nzbget_url" placeholder="http://localhost:6789" value="${esc(s.nzbget_url)}" /></div>
            <div class="input-row two">
              <div class="field"><label for="nzbget_username">Username</label><input class="input mono" id="nzbget_username" name="nzbget_username" value="${esc(s.nzbget_username)}" placeholder="nzbget" autocomplete="off" /></div>
              <div class="field"><label for="nzbget_password">Password</label><input class="input mono" id="nzbget_password" name="nzbget_password" type="password" value="${esc(s.nzbget_password)}" /></div>
            </div>
            <div class="field">
              <label for="nzbget_category">Category</label>
              <div class="input-row">
                <input class="input mono" id="nzbget_category" name="nzbget_category" placeholder="Audiobooks" value="${esc(s.nzbget_category)}" style="max-width:220px" />
                <button class="btn" type="button" data-test-client="nzbget">Test</button>
              </div>
              <span class="hint">The username and password are NZBGet’s ControlUsername and ControlPassword (Settings → Security).</span>
              <div data-result="nzbget"></div>
            </div>
          </div>
        </div>
      </section>

      <section class="panel">
        <div><h3>Download folders</h3><p class="desc">Only needed when your download clients and Narratarr see the downloads folder at different paths, such as separate Docker containers.</p></div>
        <div class="body">
          <div class="field">
            <label for="path_remote">Path mapping</label>
            <div class="input-row">
              <input class="input mono" id="path_remote" name="path_remote" placeholder="The client's path, e.g. /downloads" value="${esc(s.path_remote)}" />
              <input class="input mono" id="path_local" name="path_local" placeholder="Same folder here, e.g. /data/downloads" value="${esc(s.path_local)}" />
            </div>
            <span class="hint">Narratarr swaps the first for the second when a client reports where it saved a book.</span>
          </div>
        </div>
      </section>

      <section class="panel">
        <div><h3>Wanted &amp; following</h3><p class="desc">Books you want but don’t have yet, including new releases from authors and series you follow.</p></div>
        <div class="body">
          <div class="field">
            <label class="switch"><input type="checkbox" name="auto_search" ${s.auto_search ? "checked" : ""}><i></i>Search for Wanted books automatically</label>
            <span class="hint">Uses Prowlarr. A few books are checked at a time to stay within your indexers’ API limits.</span>
          </div>
          <div class="field">
            <label for="auto_search_hours">Look again every</label>
            <div class="input-row" style="align-items:center;max-width:260px">
              <input class="input" id="auto_search_hours" name="auto_search_hours" type="number" min="1" max="168" value="${esc(s.auto_search_hours)}" style="width:100px;flex:none" />
              <span class="muted">hours</span>
            </div>
          </div>
          <div class="field">
            <label for="audible_region">Audible store for book details</label>
            <select class="select" id="audible_region" name="audible_region" style="max-width:260px">
              ${[["us", "United States"], ["uk", "United Kingdom"], ["ca", "Canada"], ["au", "Australia"], ["in", "India"], ["de", "Germany"], ["fr", "France"], ["it", "Italy"], ["es", "Spain"], ["jp", "Japan"]]
                .map(([v, l]) => `<option value="${v}" ${s.audible_region === v ? "selected" : ""}>${l}</option>`).join("")}
            </select>
            <span class="hint">Only used for titles, covers, series and release dates. Nothing is downloaded from Audible, but what you search on the Audible tab, and the authors you follow, are sent to Audible’s and Audnexus’s public catalogs.</span>
          </div>
        </div>
      </section>

      <section class="panel">
        <div><h3>Import</h3><p class="desc">Bring in audiobooks you already have. Books dropped into the import folder are moved into your library automatically.</p></div>
        <div class="body">
          <div class="field">
            <label for="import_folder">Import folder</label>
            <input class="input mono" id="import_folder" name="import_folder" placeholder="e.g. ~/Downloads/Audiobooks to import" value="${esc(s.import_folder)}" />
            <span class="hint">Checked every minute. A book is moved in once nothing in its folder has changed for two minutes. Must be outside your library folder.</span>
          </div>
          <div><button class="btn" type="button" id="open-import">${I.down} Import books now…</button></div>
        </div>
      </section>

      <section class="panel">
        <div><h3>Source</h3><p class="desc">The site Discover browses. Change this if the domain moves.</p></div>
        <div class="body">
          <div class="field">
            <label for="abb_base_url">AudiobookBay address</label>
            <input class="input mono" id="abb_base_url" name="abb_base_url" value="${esc(s.abb_base_url)}" />
          </div>
          <div class="field">
            <label>Search with</label>
            <div class="segmented" id="provider" role="radiogroup" aria-label="Search provider">
              <button type="button" role="radio" data-v="audiobookbay">AudiobookBay’s search</button>
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
          <div class="field">
            <label class="switch"><input type="checkbox" name="smart_search" ${s.smart_search ? "checked" : ""}><i></i>Smart search</label>
            <span class="hint">Fixes typos and suggests titles as you type, using Apple’s public audiobook catalog. What you type in the search box is sent to Apple.</span>
          </div>
        </div>
      </section>

      <section class="panel">
        <div><h3>Audiobookshelf</h3><p class="desc">Optional. Books are already filed the way Audiobookshelf expects; connect it and Audiobookshelf picks up each new book as soon as it has downloaded.</p>
          <button class="guide-link" type="button" data-guide>${I.books}<span><b>Setup guide</b><small>Get your books into Audiobookshelf, step by step</small></span></button></div>
        <div class="body">
          <div class="field" id="abs-connected" hidden>
            <label for="audiobookshelf_library_id">Library to rescan</label>
            <div class="input-row">
              <select class="select abs-select" id="audiobookshelf_library_id" name="audiobookshelf_library_id">
                <option value="${esc(s.audiobookshelf_library_id)}">${s.audiobookshelf_library_id ? "Loading…" : "Choose a library"}</option>
              </select>
              <button class="btn" type="button" id="test-audiobookshelf">Test</button>
            </div>
            <span class="hint">Connected to <code id="abs-where"></code> · <a href="#" id="abs-disconnect" style="color:var(--accent)">Disconnect</a> · <a href="#" data-guide style="color:var(--accent)">Check your books</a></span>
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

          <details class="abs-manual" id="abs-manual">
            <summary>Paste an API key instead</summary>
            <div class="field">
              <label for="audiobookshelf_token">API key</label>
              <input class="input mono" id="audiobookshelf_token" name="audiobookshelf_token" type="password" value="${esc(s.audiobookshelf_token)}" />
              <span class="hint">For single sign-on (OpenID) accounts, or older Audiobookshelf versions. Create one in Audiobookshelf under Settings → Users → API Keys, then press Save changes.</span>
            </div>
          </details>
        </div>
      </section>

      <div class="save-bar"><button class="btn primary" type="submit" id="save">${I.check} Save changes</button></div>
    </form>`;

  const form = $("#settings", root);
  const preview = () => {
    const dir = form.library_dir.value.replace(/\/+$/, "");
    const sample = { author: "Frank Herbert", title: "Dune", narrator: "Scott Brick" };
    const rel = (form.folder_template.value || "{author}/{title}").replace(/\{(\w+)\}/g, (_, k) => sample[k] ?? `{${k}}`);
    $("#path-preview", root).innerHTML = `${esc(dir)}/<b>${esc(rel)}</b>/Dune.m4b`;
  };
  form.folder_template.oninput = preview;
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
  };
  $$("[data-choice]", root).forEach((seg) => {
    choose(seg.dataset.choice, form[seg.dataset.choice].value);
    seg.onclick = (e) => { const b = e.target.closest("[data-v]"); if (b) choose(seg.dataset.choice, b.dataset.v); };
  });
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
    data.smart_search = form.smart_search.checked;
    data.auto_search = form.auto_search.checked;
    data.check_updates = form.check_updates.checked;
    data.auth_local_bypass = form.auth_local_bypass.checked;
    data.max_concurrent = Number(data.max_concurrent) || 1;
    data.auto_search_hours = Number(data.auto_search_hours) || 12;
    // A bare address ("abs.example.com") is something to Find, not a setting to save yet.
    if (data.audiobookshelf_url && !/^https?:\/\//.test(data.audiobookshelf_url.trim())) delete data.audiobookshelf_url;
    return data;
  };

  async function save(quiet) {
    state.settings = await api("/api/settings", { method: "PUT", body: collect() });
    form.rd_api_key.value = state.settings.rd_api_key;
    form.audiobookshelf_token.value = state.settings.audiobookshelf_token;
    form.brave_api_key.value = state.settings.brave_api_key;
    for (const key of ["prowlarr_api_key", "sabnzbd_api_key", "nzbget_password", "qbittorrent_password", "transmission_password", "deluge_password"]) {
      form[key].value = state.settings[key];
    }
    state.sources = await api("/api/sources"); // the Prowlarr tab appears once it's set up
    updateRdPill();
    if (absConnected() === $("#abs-connected", root).hidden) absShow(); // a pasted API key just connected it
    if (!quiet) toast("Settings saved", { kind: "ok" });
  }

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
  tester("prowlarr", (r) => `Connected to Prowlarr ${esc(r.version)} · <b>${r.indexers}</b> indexer${r.indexers === 1 ? "" : "s"} enabled`);
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

  // Audiobookshelf: find the server, sign in once (which creates an API key), pick a library.
  const badLine = (text) => `<div class="rd-result bad">${esc(text)}</div>`;
  let absServer = null; // what Find last found: {url, version, ready, password_login, insecure}
  const absConnected = () => Boolean(state.settings.audiobookshelf_url && state.settings.audiobookshelf_token);
  const absShow = async (libs) => {
    const on = absConnected();
    $("#abs-connected", root).hidden = !on;
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
  tester("audiobookshelf", (r) => `Connected to Audiobookshelf ${esc(r.version)} · rescans <b>${esc(r.library)}</b> after each download`);
  $("#abs-disconnect", root).onclick = async (e) => {
    e.preventDefault();
    state.settings = await api("/api/settings", { method: "PUT", body: { audiobookshelf_token: "", audiobookshelf_library_id: "" } });
    form.audiobookshelf_token.value = "";
    $("#audiobookshelf-result", root).innerHTML = "";
    absShow();
    toast("Disconnected. Narratarr’s key is still listed in Audiobookshelf, where you can revoke it.");
  };
  absShow();
  $$("[data-guide]", root).forEach((el) => {
    el.onclick = (e) => {
      e.preventDefault();
      openAbsGuide({
        onConnect: () => {
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

  return {};
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
  return d.bookId ? state.library.find((b) => b.id === d.bookId) : byUrl().get(d.url);
}

function wantedNote(lib) {
  const st = state.settings || {};
  const out = Date.parse(lib.released || "");
  if (out > Date.now()) return `Comes out ${releaseLabel(lib.released, true).replace(/^Out /, "")}. Narratarr will start looking then.`;
  if (!st.prowlarr_configured) return "Connect Prowlarr in Settings so Narratarr can look for this book.";
  const last = lib.last_searched ? `Last looked ${ago(lib.last_searched)}. ${lib.search_note || ""}` : "";
  const next = st.auto_search ? `Narratarr looks again every ${st.auto_search_hours} hours.` : "Automatic search is off.";
  return `${last} ${next}`.trim();
}

function liveHTML(lib) {
  if (!lib && state.drawer?.source === "audible") {
    return `<div class="cta">
      <button class="btn primary" data-act="add">${I.bookmark} Add to Wanted</button>
    </div>
    <p class="cta-note">Narratarr will look for it on your indexers and download it when it turns up.</p>`;
  }
  if (!lib) {
    return `<div class="cta">
      <button class="btn primary" data-act="add">${I.down} Download</button>
      <button class="btn" data-act="want">${I.bookmark} Add to Wanted</button>
    </div>`;
  }
  if (lib.status === "wanted" && lib.source === "audible") {
    return `<div class="state wanted">
        <div class="top"><span>${I.bookmark} Wanted</span><span>${lib.last_searched ? esc(ago(lib.last_searched)) : "Not searched yet"}</span></div>
        <p>${esc(wantedNote(lib))}</p>
      </div>
      <div class="cta">
        <button class="btn primary" data-act="download">${I.search} Search now</button>
        <button class="btn" data-act="releases">${I.list} Pick a release</button>
        <button class="btn ghost danger" data-act="remove">${I.trash} Remove</button>
      </div>`;
  }
  if (ACTIVE.includes(lib.status)) {
    const measurable = ["caching", "downloading"].includes(lib.status);
    return `<div class="state">
        <div class="top"><span>${esc(STATUS[lib.status])}</span><span>${measurable ? `${(lib.progress || 0).toFixed(0)}%${lib.speed ? ` · ${fmtBytes(lib.speed)}/s` : ""}` : ""}</span></div>
        <div class="progress ${measurable ? "" : "indeterminate"}"><i style="width:${lib.progress || 0}%"></i></div>
        <div class="top" style="margin-top:10px"><span style="color:var(--text-3);font-size:12.5px">${esc(lib.status === "queued" ? "Waiting for a free slot…" : lib.detail)}</span></div>
      </div>
      <div class="cta"><button class="btn" data-act="stop">${I.stop} Stop</button></div>`;
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
        <button class="btn sm ghost danger" data-act="remove">${I.trash} Remove</button>
      </div>`;
  }
  if (lib.status === "failed") {
    return `<div class="state failed">${esc(lib.error || "Something went wrong.")}</div>
      <div class="cta">
        <button class="btn primary" data-act="download">${I.retry} Try again</button>
        ${lib.source !== "local" ? `<button class="btn" data-act="releases">${I.list} Pick another release</button>` : ""}
        <button class="btn ghost danger" data-act="remove">${I.trash} Remove</button>
      </div>`;
  }
  return `<div class="cta">
      <button class="btn primary" data-act="download">${I.down} Download now</button>
      <button class="btn" data-act="releases">${I.list} Other releases</button>
      <button class="btn ghost danger" data-act="remove">${I.trash} Remove</button>
    </div>`;
}

// ---- releases (pick one yourself) ----

function releasesHTML(res) {
  if (!res.items.length) {
    return `<div class="releases"><h4>Releases</h4><p class="muted">Nothing on your indexers or LibriVox looks like this book yet.</p></div>`;
  }
  const label = (r) => r.source === "prowlarr" ? r.indexer || "Prowlarr" : sourceInfo(r.source).label;
  return `<div class="releases">
    <h4>Releases <small>${res.items.length} found · best matches first</small></h4>
    ${res.items.map((r, i) => {
      const quality = r.match >= res.auto_match ? "good" : r.match >= 75 ? "ok" : "weak";
      const facts = [label(r), r.protocol === "usenet" ? "Usenet" : r.protocol === "torrent" ? "Torrent" : "Direct",
        r.format, r.size, r.seeders != null ? `${r.seeders} seeder${r.seeders === 1 ? "" : "s"}` : "", r.posted].filter(Boolean);
      return `<div class="rel">
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

// ---- following from the drawer ----

function followHTML(b) {
  if (!(b.source === "audible" || b.asin)) return "";
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

function drawerHTML(b, loading) {
  const narrators = b.narrators || [];
  const pills = [
    b.length && [I.clock, b.length],
    b.format && [I.file, b.format],
    b.bitrate && b.bitrate !== "?" && [I.wave, b.bitrate],
    b.size && [I.disc, b.size],
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
      <h2>${esc(b.title)}</h2>
      <div class="by">by <strong>${esc(b.author || "Unknown author")}</strong></div>
      ${narrators.length ? `<div class="narr">Narrated by ${esc(narrators.slice(0, 3).join(", "))}${narrators.length > 3 ? ` +${narrators.length - 3} more` : ""}</div>` : ""}
      ${pills.length ? `<div class="pills">${pills.map(([ic, t]) => `<span class="pill">${ic}${esc(t)}</span>`).join("")}</div>` : ""}
      <div id="drawer-live">${loading ? '<div class="cta"><span class="spinner" style="color:var(--text-3)"></span></div>' : ""}</div>
      ${!loading ? followHTML(b) : ""}
      <div id="drawer-releases"></div>
      ${desc ? `<div class="about"><h4>About this book</h4><p class="${desc.length > 520 ? "clamped" : ""}">${esc(desc)}</p>${desc.length > 520 ? '<button class="more" data-more>Read more</button>' : ""}</div>` : ""}
      ${!loading ? `<dl>
        ${(b.series || []).length ? `<dt>Series</dt><dd>${b.series.map((x) => esc(x.sequence ? `${x.title} #${x.sequence}` : x.title)).join(" · ")}</dd>` : ""}
        ${(b.categories || []).length && b.source !== "audible" ? `<dt>Genres</dt><dd>${esc(b.categories.join(", "))}</dd>` : ""}
        ${b.released ? `<dt>Released</dt><dd>${esc(releaseLabel(b.released, true))}</dd>` : ""}
        ${b.posted && !b.released ? `<dt>Posted</dt><dd>${esc(b.posted)}</dd>` : ""}
        ${b.info_hash ? `<dt>Info hash</dt><dd class="mono">${esc(b.info_hash)}</dd>` : ""}
        ${(b.files || []).length ? `<dt>Files</dt><dd>${b.files.length} ${esc(b.format || "audio")} file${b.files.length === 1 ? "" : "s"}${b.size ? ` · ${esc(b.size)}` : ""}</dd>` : ""}
        ${b.release ? `<dt>Release</dt><dd class="mono">${esc(b.release)}</dd>` : ""}
        ${b.indexer ? `<dt>Indexer</dt><dd>${esc(b.indexer)}</dd>` : ""}
        <dt>Source</dt><dd>${b.source === "local" ? `Your files${b.import_mode === "inplace" ? " (left in place)" : ""}`
          : /^https?:/.test(b.url || b.source_url || "")
          ? `<a href="${esc(b.url || b.source_url)}" target="_blank" rel="noopener noreferrer">${esc(sourceInfo(b.source).label)}${isDirect(b.source) ? " on Internet Archive" : ""} ↗</a>`
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

async function openDrawer({ url, bookId, preview, source }) {
  state.drawer = { url, bookId, source, protocol: preview?.protocol || "" };
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
      await api(`/api/library/${lib.id}/grab`, { method: "POST", body: { url: r.url, source: r.source } });
      $("#drawer-releases").innerHTML = "";
      await refreshLibrary();
      toast(`Grabbed ${r.release || r.title}`, { kind: "ok", cover: lib.cover_url });
    } catch (err) {
      toast(err.message, { kind: "err" });
      spinBtn(grabBtn, false);
    }
    return;
  }
  const btn = e.target.closest("[data-act]");
  if (!btn || !state.drawer) return;
  const act = btn.dataset.act;
  if (act === "releases") return showReleases(drawerBook());
  spinBtn(btn, true);
  try {
    if (act === "add" || act === "want") {
      const url = state.drawer.url;
      if (act === "want") {
        await api("/api/library", { method: "POST", body: { url, download: false, source: state.drawer.source } });
        await refreshLibrary();
        toast("Saved to Wanted", { kind: "ok" });
      } else {
        await addBook(url, { cover: $(".hero-cover img", drawer)?.src, source: state.drawer.source, protocol: state.drawer.protocol });
      }
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
    fields: `<h4 style="margin:22px 0 0;font-size:14px">Change your password</h4>${passwordFields(true)}`,
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
  updateRdPill();
  window.addEventListener("hashchange", route);
  route();
  player.onChange = () => updateDrawerLive();
  player.restore(state.library);
  setInterval(refreshLibrary, 2000);
  checkUpdates();
  setInterval(checkUpdates, 6 * 60 * 60 * 1000);
}

boot();
