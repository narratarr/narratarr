// The built-in ebook reader. An EPUB is shown one chapter file at a time in a frame, laid
// out in CSS columns so it turns like pages: one page on a phone, two side by side on a
// wide screen. The server locks each chapter down (no scripts, nothing from outside
// Narratarr); this module only styles and moves it.
//
// Progress is saved like listening progress: position 0–100 through the book (weighted by
// each chapter's size), so a library card can show "42% read".

import { $, $$, esc, I } from "./util.js";

const STORE = "narratarr.reader";
const SIZES = [80, 90, 100, 110, 120, 135, 150, 170, 190]; // percent
const THEMES = { // "soft" is for small labels, so each passes 4.5:1 on its page
  paper: { label: "Paper", bg: "#fbf8f2", fg: "#29231d", soft: "#6f6458", link: "#b4612a" },
  sepia: { label: "Sepia", bg: "#f2e6cd", fg: "#45331f", soft: "#6e5a3d", link: "#9b5420" },
  night: { label: "Night", bg: "#15120f", fg: "#e2d7ca", soft: "#9a8f82", link: "#f2b468" },
};
const FONTS = {
  original: { label: "Original", css: "" },
  serif: { label: "Serif", css: `"Iowan Old Style", Charter, "Palatino Linotype", Palatino, Georgia, serif` },
  sans: { label: "Sans", css: `"Inter", -apple-system, "Segoe UI", system-ui, sans-serif` },
};
const GAP = 64; // between columns, and the margin each page keeps at its edge
const SAVE_EVERY = 1500;
const WORDS_PER_MINUTE = 250;
const CHARS_PER_WORD = 5.7; // with the space after it
const DEFAULTS = { size: 110, theme: "auto", font: "original", pages: "two" };

const prefs = () => {
  try { return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(STORE) || "{}") }; }
  catch { return { ...DEFAULTS }; }
};
const phone = () => matchMedia("(max-width: 700px)").matches;

// "CHAPTER VII. A Mad Tea-Party" -> { number: "VII", name: "A Mad Tea-Party" }, for the contents and the top bar.
// Some books put a picture's caption in front ("He rode a black horse. CHAPTER III."): that part is dropped.
const NUMBER = /(?:^\s*|[.!?]\s+)chapter\s+([ivxlcdm]+|\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b[.:)\s\u2014\u2013-]*(.*)$/i;
function chapterParts(title) {
  const m = NUMBER.exec(title || "");
  if (!m) return { number: "", name: title || "" };
  const n = m[1];
  const number = /^[ivxlcdm]+$/i.test(n) ? n.toUpperCase() : n[0].toUpperCase() + n.slice(1).toLowerCase();
  const name = m[2].trim();
  return name ? { number, name } : { number: "", name: `Chapter ${number}`, chapter: number };
}

function timeLeft(minutes) {
  if (minutes < 1) return "under a minute left";
  if (minutes < 60) return `about ${Math.round(minutes)} min left`;
  const h = Math.floor(minutes / 60), m = Math.round((minutes % 60) / 5) * 5;
  return `about ${h} hr${m ? ` ${m} min` : ""} left`;
}
const remember = (patch) => { try { localStorage.setItem(STORE, JSON.stringify({ ...prefs(), ...patch })); } catch { /* private mode */ } };
const dark = () => matchMedia("(prefers-color-scheme: dark)").matches;
const pageUrl = (id, href) => `/api/library/${id}/read/${href.split("/").map(encodeURIComponent).join("/")}`;

class Reader {
  constructor() {
    this.book = null;
    this.onChange = () => {};
  }

  get theme() {
    const t = prefs().theme;
    return THEMES[t] ? t : dark() ? "night" : "paper";
  }

  // ---- opening and closing ------------------------------------------------

  async open(book) {
    if (this.book) this.close();
    this.book = book;
    this.pdf = /\.pdf$/i.test(book.path || "");
    this.mount();
    if (this.pdf) return this.openPdf();
    try {
      const res = await fetch(`/api/library/${book.id}/read`);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "This book can't be opened.");
      if (this.book !== book) return;
      this.index = data;
    } catch (e) {
      return this.fail(e.message);
    }
    const sizes = this.index.spine.map((s) => s.size);
    this.total = sizes.reduce((a, b) => a + b, 0);
    this.before = sizes.map((_, i) => sizes.slice(0, i).reduce((a, b) => a + b, 0));
    const parts = this.index.toc.map((t) => chapterParts(t.title));
    const chapters = parts.filter((c) => c.number || c.chapter).length;
    $("[data-bind=count]", this.el).textContent = chapters ? `${chapters} chapters` : "";
    $(".rd-toc ol", this.el).innerHTML = this.index.toc.length
      ? this.index.toc.map((t, i) => `<li><button data-r="toc-go" data-i="${i}" style="--depth:${t.depth}">
          <span class="n">${esc(parts[i].number)}</span><span class="t">${esc(parts[i].name || t.title)}</span><span class="here">You’re here</span></button></li>`).join("")
      : this.index.spine.map((s, i) => `<li><button data-r="spine-go" data-i="${i}"><span class="n">${i + 1}</span><span class="t">Part ${i + 1}</span><span class="here">You’re here</span></button></li>`).join("");
    const at = !book.finished && book.listen_duration ? book.listen_position / book.listen_duration : 0;
    const [chapter, fraction] = this.locate(at);
    this.go(chapter, { fraction });
  }

  close() {
    if (!this.book) return;
    this.save();
    this.ro?.disconnect();
    document.removeEventListener("keydown", this.onKey, true);
    const el = this.el;
    el.classList.remove("open");
    setTimeout(() => el.remove(), 380);
    document.body.classList.remove("reading");
    document.body.style.overflow = "";
    this.book = this.index = this.el = null;
    this.onChange();
  }

  mount() {
    const b = this.book;
    const el = document.createElement("section");
    el.className = this.classes();
    el.tabIndex = -1;
    el.setAttribute("role", "dialog");
    el.setAttribute("aria-modal", "true");
    el.setAttribute("aria-label", `Reading ${b.title}`);
    el.innerHTML = `
      <header class="rd-top">
        <button class="rd-btn" data-r="close" aria-label="Close the book">${I.x}</button>
        <div class="rd-title"><b>${esc(b.title)}</b><small data-bind="chapter">${esc(b.author || "")}</small></div>
        ${this.pdf ? "" : `
        <button class="rd-btn" data-r="toc" aria-label="Contents" aria-expanded="false">${I.list}</button>
        <button class="rd-btn rd-aa" data-r="settings" aria-label="Text and theme" aria-expanded="false"><span aria-hidden="true"><span>A</span><span>a</span></span></button>`}
      </header>
      <div class="rd-stage">
        ${this.pdf ? "" : `<button class="rd-turn prev" data-r="prev" aria-label="Previous page">${I.chevron}</button>`}
        <div class="rd-page"><div class="rd-loading"><span class="spinner"></span></div><iframe title="${esc(b.title)}" ${this.pdf ? "" : 'sandbox="allow-same-origin"'}></iframe><i class="rd-fold" aria-hidden="true"></i></div>
        ${this.pdf ? "" : `<button class="rd-turn next" data-r="next" aria-label="Next page">${I.chevron}</button>`}
      </div>
      ${this.pdf ? "" : `
      <footer class="rd-foot">
        <span data-bind="where"></span>
        <div class="rd-progress" aria-hidden="true"><i></i></div>
        <span data-bind="percent"></span>
      </footer>
      <div class="rd-scrim" data-r="done" aria-hidden="true"></div>
      <aside class="rd-toc" aria-label="Contents">
        <div class="rd-head"><h4>Contents</h4><small data-bind="count"></small><button class="rd-done" data-r="done">Done</button></div>
        <ol></ol>
      </aside>
      <div class="rd-settings" role="dialog" aria-label="Text and theme">${this.settingsHTML()}</div>`}`;
    document.body.append(el);
    document.body.classList.add("reading");
    document.body.style.overflow = "hidden";
    this.el = el;
    this.frame = $("iframe", el);
    el.onclick = (e) => this.handle(e);
    el.oninput = (e) => this.slide(e);
    this.onKey = (e) => this.key(e);
    document.addEventListener("keydown", this.onKey, true);
    if (!this.pdf) {
      this.frame.addEventListener("load", () => this.ready());
      this.ro = new ResizeObserver(() => this.relayout());
      this.ro.observe($(".rd-page", el));
    }
    requestAnimationFrame(() => el.classList.add("open"));
    el.focus({ preventScroll: true }); // the reader, not its close button: no focus ring until the keyboard is used
    this.onChange();
  }

  openPdf() {
    this.frame.src = `/api/library/${this.book.id}/ebook?inline=true`;
    this.frame.addEventListener("load", () => this.el?.classList.add("loaded"), { once: true });
    fetch(`/api/library/${this.book.id}/progress`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ position: 0, duration: 0 }),
    }).catch(() => {});
  }

  fail(message) {
    if (!this.el) return;
    $(".rd-page", this.el).innerHTML = `<div class="rd-error">${I.alert}<p>${esc(message)}</p>
      <a class="btn" href="/api/library/${this.book.id}/ebook" download>${I.down} Download it instead</a></div>`;
  }

  // ---- where we are --------------------------------------------------------

  // A position through the whole book (0–1) as [chapter, fraction of that chapter], and back.
  locate(at) {
    const target = Math.min(Math.max(at, 0), 0.99999) * this.total;
    let i = this.before.findLastIndex((b) => b <= target);
    if (i < 0) i = 0;
    return [i, (target - this.before[i]) / this.index.spine[i].size];
  }

  get progress() {
    if (!this.index || this.chapter == null) return 0;
    const within = this.pages > 1 ? this.page / (this.pages - 1) : 0;
    return (this.before[this.chapter] + within * this.index.spine[this.chapter].size) / this.total;
  }

  get atEnd() {
    return this.chapter === this.index.spine.length - 1 && this.page >= this.pages - 1;
  }

  save({ finished } = {}) {
    if (!this.book || this.pdf || this.chapter == null) return;
    const body = { position: finished ? 0 : Math.round(this.progress * 10000) / 100, duration: 100 };
    if (finished !== undefined) body.finished = finished;
    else if (this.book.finished && this.progress > 0.02) body.finished = false; // reading it again
    this.book.listen_position = body.position;
    this.book.listen_duration = 100;
    if (body.finished !== undefined) this.book.finished = body.finished;
    fetch(`/api/library/${this.book.id}/progress`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), keepalive: true,
    }).catch(() => {});
  }

  saveSoon() {
    clearTimeout(this.saveTimer);
    this.saveTimer = setTimeout(() => this.save(), SAVE_EVERY);
  }

  // ---- loading a chapter and laying it out ---------------------------------

  go(chapter, { fraction = 0, fragment = "", end = false } = {}) {
    if (!this.index) return;
    chapter = Math.max(0, Math.min(chapter, this.index.spine.length - 1));
    this.want = { fraction, fragment, end };
    if (chapter === this.chapter && this.doc) return this.place();
    this.chapter = chapter;
    this.doc = null;
    this.el.classList.remove("loaded");
    this.frame.src = pageUrl(this.book.id, this.index.spine[chapter].href);
  }

  ready() {
    let doc;
    try { doc = this.frame.contentDocument; } catch { doc = null; }
    if (!doc || !this.book || !this.index || doc.URL === "about:blank") return; // the frame's empty first page
    this.doc = doc;
    const link = doc.createElement("link");
    link.rel = "stylesheet";
    link.href = "/static/fonts/fonts.css";
    (doc.head || doc.documentElement).append(link);
    this.styleEl = doc.createElement("style");
    (doc.head || doc.documentElement).append(this.styleEl);
    doc.addEventListener("click", (e) => this.tap(e));
    doc.addEventListener("keydown", (e) => this.key(e));
    doc.addEventListener("touchstart", (e) => { this.touch = e.touches[0] ? [e.touches[0].clientX, e.touches[0].clientY] : null; }, { passive: true });
    doc.addEventListener("touchend", (e) => this.swipe(e), { passive: true });
    this.layout();
    // Fonts and pictures change how many pages there are; settle once they've loaded.
    (doc.fonts?.ready || Promise.resolve()).then(() => this.doc === doc && this.relayout());
    doc.addEventListener("load", () => this.doc === doc && this.relayout(), true);
    this.place();
    this.el.classList.add("loaded");
  }

  layout() {
    const doc = this.doc;
    if (!doc) return;
    const box = $(".rd-page", this.el).getBoundingClientRect();
    const width = Math.floor(box.width), height = Math.floor(box.height);
    // A page that's only a picture (a cover, a plate) gets the whole spread, centred.
    const text = (doc.body?.textContent || "").trim().length;
    const picture = text < 160 && doc.querySelector("img, svg, image");
    const columns = width >= 900 && !picture && prefs().pages !== "one" ? 2 : 1;
    this.el.classList.toggle("spread", columns === 2);
    // How much of each chapter file is words, for the time left: markup weighs differently in every book.
    const size = this.index.spine[this.chapter].size;
    if (text > 200) this.density = Math.min(1, text / size);
    // The text is laid out exactly one page wide, and the frame is one gap wider, so every turn
    // moves one page plus the gap after it, and that gap stays out of sight.
    this.step = width + GAP;
    this.frame.style.width = `${this.step}px`;
    this.frame.style.height = `${height}px`;
    const t = THEMES[this.theme], p = prefs(), font = FONTS[p.font]?.css;
    this.styleEl.textContent = `
      html { height: ${height}px !important; width: ${width}px !important; margin: 0 !important; padding: 0 !important;
        column-count: ${columns} !important; column-width: auto !important; column-gap: ${GAP}px !important; column-fill: auto !important;
        overflow: hidden !important; font-size: ${p.size}% !important; -webkit-text-size-adjust: none; text-size-adjust: none;
        background: transparent !important; color-scheme: ${this.theme === "night" ? "dark" : "light"}; }
      body { margin: 0 !important; padding: 0 !important; background: transparent !important; color: ${t.fg} !important;
        line-height: 1.6; overflow-wrap: break-word; hyphens: auto; -webkit-hyphens: auto; }
      body * { color: inherit !important; background-color: transparent !important; border-color: ${t.soft} !important; }
      a, a * { color: ${t.link} !important; }
      ${font ? `body, body :not(pre):not(code):not(kbd):not(samp) { font-family: ${font} !important; }` : ""}
      img, svg, video, image { max-width: 100% !important; max-height: ${height - 8}px !important; height: auto;
        object-fit: contain; break-inside: avoid; }
      figure, table, pre { break-inside: avoid; }
      ${picture ? `body { text-align: center !important; } img, svg { display: block; margin: 0 auto !important; }` : ""}
      ::selection { background: ${t.link}55; }`;
  }

  relayout() {
    if (!this.doc || !this.index) return;
    const at = this.pages > 1 ? this.page / (this.pages - 1) : 0;
    this.layout();
    this.want = { fraction: at };
    this.place(false);
  }

  measure() {
    const root = this.doc.scrollingElement || this.doc.documentElement;
    this.pages = Math.max(1, Math.ceil((root.scrollWidth - 2) / this.step));
    return root;
  }

  place(animate = false) {
    if (!this.doc) return;
    this.measure();
    const { fraction = 0, fragment = "", end = false } = this.want || {};
    let page = end ? this.pages - 1 : Math.round(fraction * (this.pages - 1));
    const target = fragment && this.doc.getElementById(fragment);
    if (target) {
      const root = this.doc.scrollingElement || this.doc.documentElement;
      page = Math.floor((target.getBoundingClientRect().left + root.scrollLeft) / this.step);
    }
    this.want = null;
    this.mark();
    this.turnTo(page, animate);
  }

  // Which page each contents entry in this chapter file starts on (one file can hold many chapters).
  mark() {
    const root = this.doc.scrollingElement || this.doc.documentElement;
    const href = this.index.spine[this.chapter].href;
    this.marks = this.index.toc.flatMap((t, i) => {
      if (t.href !== href) return [];
      const el = t.fragment && this.doc.getElementById(t.fragment);
      return [{ i, page: el ? Math.floor((el.getBoundingClientRect().left + root.scrollLeft + 1) / this.step) : 0 }];
    });
  }

  turnTo(page, animate = true) {
    const root = this.measure();
    this.page = Math.max(0, Math.min(page, this.pages - 1));
    root.scrollTo({ left: this.page * this.step, behavior: animate && !matchMedia("(prefers-reduced-motion: reduce)").matches ? "smooth" : "instant" });
    this.render();
    this.saveSoon();
  }

  // ---- turning pages ---------------------------------------------------------

  next() {
    if (!this.doc) return;
    if (this.page < this.pages - 1) return this.turnTo(this.page + 1);
    if (this.chapter < this.index.spine.length - 1) return this.go(this.chapter + 1);
    if (!this.book.finished) {
      this.save({ finished: true });
      this.render();
      this.onChange();
    }
  }

  prev() {
    if (!this.doc) return;
    if (this.page > 0) return this.turnTo(this.page - 1);
    if (this.chapter > 0) this.go(this.chapter - 1, { end: true });
  }

  tap(e) {
    const a = e.target.closest?.("a[href]");
    if (a) return this.follow(e, a);
    if (this.doc.getSelection()?.toString()) return; // selecting text, not turning
    const x = e.clientX / ($(".rd-page", this.el).clientWidth || 1);
    if (x < 0.3) this.prev();
    else if (x > 0.7) this.next();
    else this.el.classList.toggle("calm");
    this.closePanels();
  }

  swipe(e) {
    const start = this.touch, t = e.changedTouches[0];
    this.touch = null;
    if (!start || !t) return;
    const dx = t.clientX - start[0], dy = t.clientY - start[1];
    if (Math.abs(dx) > 45 && Math.abs(dx) > Math.abs(dy) * 1.4) dx < 0 ? this.next() : this.prev();
  }

  // Links inside the book go to their chapter; links out of it open in a new tab, never in here.
  follow(e, a) {
    e.preventDefault();
    let url;
    try { url = new URL(a.getAttribute("href"), this.doc.URL); } catch { return; }
    const prefix = `/api/library/${this.book.id}/read/`;
    if (url.origin === location.origin && url.pathname.startsWith(prefix)) {
      const href = url.pathname.slice(prefix.length).split("/").map(decodeURIComponent).join("/");
      const i = this.index.spine.findIndex((s) => s.href === href);
      if (i >= 0) this.go(i, { fragment: decodeURIComponent(url.hash.slice(1)) });
    } else if (/^(https?|mailto):$/.test(url.protocol)) {
      window.open(url.href, "_blank", "noopener,noreferrer");
    }
  }

  key(e) {
    if (!this.book || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.target?.matches?.("input, select, textarea")) return;
    const k = e.key;
    if (k === "Escape") {
      e.preventDefault(); e.stopPropagation();
      if (this.el.querySelector(".rd-toc.open, .rd-settings.open")) this.closePanels();
      else this.close();
      return;
    }
    if (this.pdf) return;
    const rtl = this.index?.rtl;
    if (k === "ArrowRight" || k === "PageDown" || (k === " " && !e.shiftKey)) { e.preventDefault(); e.stopPropagation(); rtl && k === "ArrowRight" ? this.prev() : this.next(); }
    else if (k === "ArrowLeft" || k === "PageUp" || (k === " " && e.shiftKey)) { e.preventDefault(); e.stopPropagation(); rtl && k === "ArrowLeft" ? this.next() : this.prev(); }
  }

  // ---- chrome ----------------------------------------------------------------

  // The contents entry this page is in: the last one starting on or before it.
  current() {
    const here = (this.marks || []).findLast((m) => m.page <= this.page) || this.marks?.[0];
    if (here) return here.i;
    const toc = this.index.toc;
    const at = toc.map((t) => this.index.spine.findIndex((s) => s.href === t.href));
    return toc.findLastIndex((_, i) => at[i] >= 0 && at[i] < this.chapter);
  }

  chapterTitle() {
    const title = this.index.toc[this.current()]?.title || "";
    const { number, name } = chapterParts(title);
    return number ? `Chapter ${number} · ${name}` : name;
  }

  minutesLeft() {
    const chars = (1 - this.progress) * this.total * (this.density || 0.6);
    return chars / CHARS_PER_WORD / WORDS_PER_MINUTE;
  }

  render() {
    if (!this.el || !this.index) return;
    const pct = Math.round(this.progress * 100);
    const title = this.chapterTitle();
    $("[data-bind=chapter]", this.el).textContent = title || this.book.author || "";
    const small = phone(), done = this.book.finished && this.atEnd;
    $("[data-bind=where]", this.el).textContent = done ? "The end"
      : this.pages > 1 ? `Page ${this.page + 1} of ${this.pages}${small ? "" : " in this chapter"}` : title ? "" : `Part ${this.chapter + 1}`;
    $("[data-bind=percent]", this.el).textContent = done ? "Finished"
      : small || this.progress < 0.005 ? `${pct}%` : `${pct}% · ${timeLeft(this.minutesLeft())}`;
    $(".rd-progress i", this.el).style.width = `${this.progress * 100}%`;
    $("[data-r=prev]", this.el).disabled = this.chapter === 0 && this.page === 0;
    $("[data-r=next]", this.el).classList.toggle("finish", this.atEnd);
    $("[data-r=next]", this.el).setAttribute("aria-label", this.atEnd ? "Finish the book" : "Next page");
    const now = this.current();
    $$(".rd-toc button", this.el).forEach((b) => {
      b.classList.toggle("on", b.dataset.r === "toc-go" ? Number(b.dataset.i) === now : Number(b.dataset.i) === this.chapter);
    });
  }

  settingsHTML() {
    const p = prefs();
    const at = Math.max(0, SIZES.indexOf(p.size));
    const pick = (r, list, current) => list.map(([k, label, cls = ""]) =>
      `<button class="${current === k ? "on" : ""} ${cls}" data-r="${r}" data-v="${k}" aria-pressed="${current === k}">${label}</button>`).join("");
    return `
      <div class="rd-head"><h4>Text &amp; theme</h4><button class="rd-done" data-r="done">Done</button></div>
      <div class="rd-row">
        <span class="rd-label">Text size</span>
        <div class="rd-size">
          <button data-r="smaller" aria-label="Smaller text">A</button>
          <b data-bind="size">${p.size}%</b>
          <input type="range" min="0" max="${SIZES.length - 1}" step="1" value="${at}" aria-label="Text size" aria-valuetext="${p.size}%">
          <button data-r="bigger" aria-label="Bigger text">A</button>
        </div>
      </div>
      <div class="rd-row">
        <span class="rd-label">Theme</span>
        <div class="rd-themes" role="radiogroup" aria-label="Theme">
          ${[["auto", "Auto", "Match Narratarr"], ...Object.entries(THEMES).map(([k, t]) => [k, t.label, t.label])].map(([k, label, title]) => `
            <button role="radio" aria-checked="${p.theme === k}" class="sw sw-${k} ${p.theme === k ? "on" : ""}" data-r="theme" data-v="${k}" title="${title}"><i></i><span>${label}</span></button>`).join("")}
        </div>
      </div>
      <div class="rd-row">
        <span class="rd-label">Font</span>
        <div class="segmented">${pick("font", Object.entries(FONTS).map(([k, f]) => [k, f.label, `f-${k}`]), p.font)}</div>
      </div>
      <div class="rd-row rd-pages">
        <span class="rd-label">Pages</span>
        <div class="segmented">${pick("pages", [["two", "Two side by side"], ["one", "One"]], p.pages)}</div>
      </div>`;
  }

  classes() {
    const keep = ["open", "loaded", "calm", "spread", "has-sheet"].filter((c) => this.el?.classList.contains(c));
    return ["reader", `theme-${this.theme}`, prefs().pages === "one" ? "one-page" : "", ...keep].filter(Boolean).join(" ");
  }

  set(patch, { redraw = true } = {}) {
    remember(patch);
    this.el.className = this.classes();
    if (redraw) $(".rd-settings", this.el).innerHTML = this.settingsHTML();
    this.relayout();
  }

  // The text-size slider: applied as it moves, without redrawing the panel it's in.
  slide(e) {
    if (e.target.type !== "range") return;
    const size = SIZES[Number(e.target.value)];
    e.target.setAttribute("aria-valuetext", `${size}%`);
    $("[data-bind=size]", this.el).textContent = `${size}%`;
    this.set({ size }, { redraw: false });
  }

  closePanels() {
    $$(".rd-toc, .rd-settings", this.el).forEach((p) => p.classList.remove("open"));
    this.el.classList.remove("has-sheet");
    $$("[data-r=toc], [data-r=settings]", this.el).forEach((b) => b.setAttribute("aria-expanded", "false"));
  }

  togglePanel(name) {
    const panel = $(`.rd-${name}`, this.el);
    const open = !panel.classList.contains("open");
    this.closePanels();
    panel.classList.toggle("open", open);
    this.el.classList.toggle("has-sheet", open); // on a phone, the page dims behind the sheet
    $(`[data-r=${name}]`, this.el).setAttribute("aria-expanded", String(open));
    if (open && name === "toc") $(".rd-toc button.on", this.el)?.scrollIntoView({ block: "center" });
  }

  handle(e) {
    const btn = e.target.closest("[data-r]");
    if (!btn) {
      if (!e.target.closest(".rd-toc, .rd-settings")) this.closePanels();
      return;
    }
    const r = btn.dataset.r, p = prefs();
    if (r === "close") this.close();
    else if (r === "done") this.closePanels();
    else if (r === "next") this.next();
    else if (r === "prev") this.prev();
    else if (r === "toc" || r === "settings") this.togglePanel(r);
    else if (r === "toc-go") {
      const t = this.index.toc[btn.dataset.i];
      this.go(this.index.spine.findIndex((s) => s.href === t.href), { fragment: t.fragment });
      this.closePanels();
    } else if (r === "spine-go") { this.go(Number(btn.dataset.i)); this.closePanels(); }
    else if (r === "smaller" || r === "bigger") {
      const i = SIZES.indexOf(p.size) < 0 ? 3 : SIZES.indexOf(p.size);
      this.set({ size: SIZES[Math.max(0, Math.min(SIZES.length - 1, i + (r === "bigger" ? 1 : -1)))] });
    } else if (r === "theme") this.set({ theme: btn.dataset.v });
    else if (r === "font") this.set({ font: btn.dataset.v });
    else if (r === "pages") this.set({ pages: btn.dataset.v });
  }
}

export const reader = new Reader();
// Pick "Match Narratarr" up again when the system switches between light and dark.
matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", () => { if (reader.book && prefs().theme === "auto") reader.set({}); });
