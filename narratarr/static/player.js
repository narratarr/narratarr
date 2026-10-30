// The built-in audiobook player: a mini bar, a full "Now Playing" sheet,
// chapters, speed, sleep timer, resume, and lock-screen / media-key controls.
//
// A book can be one big .m4b or dozens of .mp3 parts. Either way the player
// presents a single timeline: each file has an offset, and "time" is always
// seconds from the start of the book.

import { $, $$, esc, I, api, coverHTML, fmtTime, fmtLeft, toast } from "./util.js";

const SPEEDS = [0.75, 0.9, 1, 1.1, 1.2, 1.3, 1.5, 1.75, 2, 2.5, 3];
const SLEEP = [[0, "Off"], [5, "5 minutes"], [15, "15 minutes"], [30, "30 minutes"], [45, "45 minutes"], [60, "1 hour"], [-1, "End of chapter"]];
const SAVE_EVERY = 10_000;
const STORE = "narratarr.player";

const load = () => { try { return JSON.parse(localStorage.getItem(STORE) ?? localStorage.getItem("audiobookeer.player")) || {}; } catch { return {}; } };
const store = (patch) => { try { localStorage.setItem(STORE, JSON.stringify({ ...load(), ...patch })); } catch { /* private mode */ } };

class Player {
  constructor() {
    this.audio = new Audio();
    this.audio.preload = "auto";
    this.book = null;
    this.media = null;
    this.fileIdx = -1;
    this.speed = load().speed || 1;
    this.sleep = { mode: 0, until: 0 };
    this.lastSave = 0;
    this.dragging = false;
    this.onChange = () => {};
    this.wire();
  }

  // ---- timeline -----------------------------------------------------------

  get file() { return this.media?.files[this.fileIdx]; }
  get duration() {
    const d = this.media?.duration || 0;
    return d || (this.media?.files.length === 1 ? this.audio.duration || 0 : 0);
  }
  get time() { return this.file ? this.file.offset + (this.audio.currentTime || 0) : 0; }
  get playing() { return !this.audio.paused && !this.audio.ended; }

  chapterIndex(t = this.time) {
    const ch = this.media?.chapters || [];
    let i = -1;
    for (let k = 0; k < ch.length; k++) if (ch[k].start <= t + 0.5) i = k;
    return i;
  }
  chapterBounds(i = this.chapterIndex()) {
    const ch = this.media?.chapters || [];
    if (i < 0) return [0, this.duration];
    return [ch[i].start, ch[i + 1]?.start ?? this.duration];
  }

  fileAt(t) {
    const files = this.media.files;
    for (let i = files.length - 1; i >= 0; i--) if (t >= files[i].offset) return i;
    return 0;
  }

  async loadFile(i) {
    this.fileIdx = i;
    this.audio.src = `/api/library/${this.book.id}/stream/${i}`;
    await new Promise((resolve, reject) => {
      const ok = () => { cleanup(); resolve(); };
      const bad = () => { cleanup(); reject(new Error("This audio file couldn’t be played by your browser.")); };
      const cleanup = () => { this.audio.removeEventListener("loadedmetadata", ok); this.audio.removeEventListener("error", bad); };
      this.audio.addEventListener("loadedmetadata", ok);
      this.audio.addEventListener("error", bad);
    });
    this.audio.playbackRate = this.speed;
  }

  async seek(t, play = this.playing) {
    if (!this.media) return;
    const dur = this.duration;
    t = Math.max(0, dur ? Math.min(t, dur - 0.25) : t);
    const i = this.fileAt(t);
    if (i !== this.fileIdx) await this.loadFile(i);
    this.audio.currentTime = Math.max(0, t - this.file.offset);
    if (play) await this.play();
    this.render();
  }

  // ---- controls -----------------------------------------------------------

  async open(book, { autoplay = true } = {}) {
    if (this.book?.id === book.id) {
      if (autoplay && !this.playing) this.play();
      this.render();
      return;
    }
    this.save();
    this.audio.pause();
    let media;
    try {
      media = await api(`/api/library/${book.id}/media`);
    } catch (e) {
      toast(e.message, { kind: "err" });
      return;
    }
    this.book = book;
    this.media = media;
    this.fileIdx = -1;
    store({ bookId: book.id });
    this.mount();
    const resume = book.finished ? 0 : Math.max(0, (book.listen_position || 0) - 2);
    try {
      await this.seek(resume, autoplay);
    } catch (e) {
      toast(e.message, { kind: "err" });
    }
    this.mediaSession();
    this.render();
    this.onChange();
  }

  async play() {
    try {
      await this.audio.play();
    } catch (e) {
      if (e.name !== "AbortError") toast("Playback was blocked. Press play to start.", { kind: "err" });
    }
  }

  toggle() { this.playing ? this.audio.pause() : this.play(); }
  skip(sec) { this.seek(this.time + sec); }

  prevChapter() {
    const i = this.chapterIndex();
    const [start] = this.chapterBounds(i);
    // Like a CD player: first press goes to the start of this chapter.
    if (this.time - start > 4 || i <= 0) this.seek(start);
    else this.seek(this.media.chapters[i - 1].start);
  }
  nextChapter() {
    const next = this.media?.chapters[this.chapterIndex() + 1];
    if (next) this.seek(next.start);
  }

  setSpeed(s) {
    this.speed = s;
    this.audio.playbackRate = s;
    store({ speed: s });
    this.render();
  }

  setSleep(mode) {
    this.sleep = { mode, until: mode > 0 ? Date.now() + mode * 60_000 : 0 };
    this.audio.volume = 1;
    if (mode) toast(mode > 0 ? `Sleep timer set for ${mode} minutes` : "Stopping at the end of this chapter", { kind: "ok" });
    this.render();
  }

  close() {
    this.save();
    this.audio.pause();
    this.audio.removeAttribute("src");
    this.audio.load();
    this.book = null;
    this.media = null;
    this.fileIdx = -1;
    store({ bookId: null });
    document.body.classList.remove("has-player");
    $("#player")?.remove();
    this.closeSheet();
    if ("mediaSession" in navigator) navigator.mediaSession.metadata = null;
    this.onChange();
  }

  save({ finished } = {}) {
    if (!this.book || !this.media) return;
    const body = { position: this.time, duration: this.duration };
    if (finished !== undefined) body.finished = finished;
    else if (this.book.finished && this.time > 30) body.finished = false; // re-listening
    this.lastSave = Date.now();
    this.book.listen_position = body.position;
    fetch(`/api/library/${this.book.id}/progress`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      keepalive: true,
    }).catch(() => {});
  }

  async restore(library) {
    const id = load().bookId;
    const book = id && library.find((b) => b.id === id && b.status === "completed");
    if (book) await this.open(book, { autoplay: false });
  }

  // ---- events -------------------------------------------------------------

  wire() {
    const a = this.audio;
    a.addEventListener("timeupdate", () => this.tick());
    a.addEventListener("play", () => { this.render(); this.onChange(); });
    a.addEventListener("pause", () => { this.save(); this.render(); this.onChange(); });
    a.addEventListener("waiting", () => $("#player")?.classList.add("buffering"));
    a.addEventListener("playing", () => $("#player")?.classList.remove("buffering"));
    a.addEventListener("ended", async () => {
      if (this.fileIdx < this.media.files.length - 1) {
        await this.loadFile(this.fileIdx + 1);
        this.play();
      } else {
        this.save({ finished: true });
        this.book.finished = true;
        toast(`You finished ${this.book.title}`, { kind: "ok", cover: this.book.cover_url });
        this.render();
        this.onChange();
      }
    });

    document.addEventListener("keydown", (e) => {
      if (!this.book || e.target.matches("input, textarea, select, [contenteditable]") || e.metaKey || e.ctrlKey) return;
      if (document.body.classList.contains("reading")) return; // the reader has the keys while a book is open
      if (e.code === "Space") { e.preventDefault(); this.toggle(); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); this.skip(-15); }
      else if (e.key === "ArrowRight") { e.preventDefault(); this.skip(30); }
      else if (e.key === "Escape" && $("#np.open")) { e.stopPropagation(); this.closeSheet(); }
    }, true);

    window.addEventListener("pagehide", () => this.save());
    document.addEventListener("visibilitychange", () => { if (document.hidden) this.save(); });
  }

  tick() {
    if (!this.book) return;
    if (this.playing && Date.now() - this.lastSave > SAVE_EVERY) this.save();

    // Sleep timer, with a gentle fade over the last 15 seconds.
    if (this.sleep.mode > 0) {
      const left = this.sleep.until - Date.now();
      if (left <= 0) { this.audio.pause(); this.audio.volume = 1; this.sleep = { mode: 0, until: 0 }; this.render(); }
      else this.audio.volume = Math.min(1, left / 15_000);
    } else if (this.sleep.mode === -1) {
      const [, end] = this.chapterBounds();
      if (this.time >= end - 0.6) { this.audio.pause(); this.sleep = { mode: 0, until: 0 }; this.render(); }
    }

    this.renderTime();
    if ("mediaSession" in navigator && this.duration && navigator.mediaSession.setPositionState) {
      try {
        navigator.mediaSession.setPositionState({ duration: this.duration, position: Math.min(this.time, this.duration), playbackRate: this.speed });
      } catch { /* some browsers reject during load */ }
    }
  }

  mediaSession() {
    if (!("mediaSession" in navigator) || !this.book) return;
    navigator.mediaSession.metadata = new MediaMetadata({
      title: this.book.title,
      artist: this.book.author,
      album: "Narratarr",
      artwork: [{ src: `/api/library/${this.book.id}/cover`, sizes: "512x512" }],
    });
    const h = {
      play: () => this.play(),
      pause: () => this.audio.pause(),
      seekbackward: (d) => this.skip(-(d.seekOffset || 15)),
      seekforward: (d) => this.skip(d.seekOffset || 30),
      seekto: (d) => this.seek(d.seekTime),
      previoustrack: () => this.prevChapter(),
      nexttrack: () => this.nextChapter(),
    };
    for (const [k, fn] of Object.entries(h)) {
      try { navigator.mediaSession.setActionHandler(k, fn); } catch { /* unsupported action */ }
    }
  }

  // ---- UI -----------------------------------------------------------------

  mount() {
    document.body.classList.add("has-player");
    let bar = $("#player");
    if (!bar) {
      bar = document.createElement("div");
      bar.id = "player";
      bar.className = "player";
      document.body.append(bar);
      bar.onclick = (e) => this.handle(e);
      bar.oninput = (e) => this.scrub(e);
      bar.onchange = (e) => this.scrubEnd(e);
    }
    const b = this.book;
    bar.innerHTML = `
      <button class="p-book" data-p="sheet" aria-label="Open Now Playing">
        ${coverHTML(b, "p-cover")}
        <span class="p-text"><b>${esc(b.title)}</b><small data-bind="chapter">${esc(b.author)}</small></span>
      </button>
      <div class="p-center">
        <div class="p-controls">
          <button class="p-btn" data-p="back" aria-label="Back 15 seconds" title="Back 15s (←)">${I.back}</button>
          <button class="p-btn p-play" data-p="toggle" aria-label="Play" title="Play / pause (Space)">${I.play}</button>
          <button class="p-btn" data-p="fwd" aria-label="Forward 30 seconds" title="Forward 30s (→)">${I.fwd}</button>
        </div>
        <div class="p-scrub">
          <span data-bind="elapsed">0:00</span>
          <input type="range" class="range" data-scrub="book" min="0" max="1000" value="0" step="1" aria-label="Book position" />
          <span data-bind="remaining">0:00</span>
        </div>
      </div>
      <div class="p-right">
        <button class="p-chip" data-p="speed" title="Playback speed"><span data-bind="speed">1×</span></button>
        <button class="p-btn sm" data-p="sleep" aria-label="Sleep timer" title="Sleep timer">${I.moon}<i class="dot" data-bind="sleepdot"></i></button>
        <button class="p-btn sm" data-p="sheet" aria-label="Chapters" title="Chapters">${I.list}</button>
        <button class="p-btn sm" data-p="close" aria-label="Close player" title="Close">${I.x}</button>
      </div>
      <div class="p-mini-progress"><i data-bind="miniprog"></i></div>`;
  }

  handle(e) {
    const el = e.target.closest("[data-p]");
    if (!el) return;
    const act = el.dataset.p;
    if (act === "toggle") this.toggle();
    else if (act === "back") this.skip(-15);
    else if (act === "fwd") this.skip(30);
    else if (act === "prev") this.prevChapter();
    else if (act === "next") this.nextChapter();
    else if (act === "close") this.close();
    else if (act === "sheet") this.openSheet();
    else if (act === "collapse") this.closeSheet();
    else if (act === "speed") this.menu(el, SPEEDS.map((s) => [s, `${s}×`, s === this.speed]), (s) => this.setSpeed(s));
    else if (act === "sleep") this.menu(el, SLEEP.map(([m, l]) => [m, l, m === this.sleep.mode]), (m) => this.setSleep(m));
    else if (act === "chapter") this.seek(this.media.chapters[Number(el.dataset.i)].start, true);
    else if (act === "finished") {
      const done = !this.book.finished;
      this.book.finished = done;
      this.save({ finished: done });
      toast(done ? "Marked as finished" : "Marked as unfinished", { kind: "ok" });
      this.render();
      this.onChange();
    }
  }

  scrub(e) {
    const r = e.target.closest("[data-scrub]");
    if (!r) return;
    this.dragging = true;
    const t = this.scrubTime(r);
    const label = r.dataset.scrub === "chapter" ? fmtTime(t - this.chapterBounds()[0]) : fmtTime(t);
    $$(`[data-bind="${r.dataset.scrub === "chapter" ? "ch-elapsed" : "elapsed"}"]`).forEach((n) => (n.textContent = label));
    r.style.setProperty("--fill", `${(r.value / r.max) * 100}%`);
  }

  async scrubEnd(e) {
    const r = e.target.closest("[data-scrub]");
    if (!r) return;
    const t = this.scrubTime(r);
    this.dragging = false;
    try { await this.seek(t); } catch (err) { toast(err.message, { kind: "err" }); }
  }

  scrubTime(r) {
    const frac = Number(r.value) / Number(r.max);
    if (r.dataset.scrub === "chapter") {
      const [s, end] = this.chapterBounds();
      return s + frac * (end - s);
    }
    return frac * this.duration;
  }

  menu(anchor, items, pick) {
    $(".p-menu")?.remove();
    const m = document.createElement("div");
    m.className = "p-menu";
    m.innerHTML = items.map(([v, label, on]) => `<button data-v="${v}" class="${on ? "on" : ""}">${esc(label)}${on ? I.check : ""}</button>`).join("");
    document.body.append(m);
    const r = anchor.getBoundingClientRect();
    const w = m.offsetWidth, h = m.offsetHeight;
    m.style.left = `${Math.min(Math.max(8, r.left + r.width / 2 - w / 2), innerWidth - w - 8)}px`;
    m.style.top = `${r.top - h - 10 > 8 ? r.top - h - 10 : r.bottom + 10}px`;
    const close = (ev) => {
      if (ev && m.contains(ev.target)) return;
      m.remove();
      document.removeEventListener("pointerdown", close, true);
    };
    setTimeout(() => document.addEventListener("pointerdown", close, true));
    m.onclick = (ev) => {
      const b = ev.target.closest("[data-v]");
      if (!b) return;
      pick(Number(b.dataset.v));
      m.remove();
      document.removeEventListener("pointerdown", close, true);
    };
  }

  // Full-screen "Now Playing" sheet.
  openSheet() {
    if (!this.book) return;
    let np = $("#np");
    if (!np) {
      np = document.createElement("section");
      np.id = "np";
      np.className = "np";
      np.setAttribute("role", "dialog");
      np.setAttribute("aria-label", "Now playing");
      document.body.append(np);
      np.onclick = (e) => this.handle(e);
      np.oninput = (e) => this.scrub(e);
      np.onchange = (e) => this.scrubEnd(e);
    }
    const b = this.book;
    const ch = this.media.chapters;
    np.innerHTML = `
      <div class="np-bg"><div></div></div>
      <header class="np-top">
        <button class="p-btn" data-p="collapse" aria-label="Minimize player">${I.chevron}</button>
        <span>Now playing</span>
        <button class="p-chip" data-p="finished" data-bind="finished"></button>
      </header>
      <div class="np-main">
        <div class="np-art">
          ${coverHTML(b, "np-cover")}
        </div>
        <div class="np-info">
          <div class="np-chapter" data-bind="chapter-name"></div>
          <h2>${esc(b.title)}</h2>
          <div class="np-by">${esc(b.author)}${b.narrators?.length ? ` · read by ${esc(b.narrators.slice(0, 2).join(", "))}` : ""}</div>
          <div class="np-scrub">
            <input type="range" class="range big" data-scrub="${ch.length ? "chapter" : "book"}" min="0" max="1000" value="0" step="1" aria-label="Position" />
            <div class="np-times"><span data-bind="${ch.length ? "ch-elapsed" : "elapsed"}">0:00</span><span data-bind="${ch.length ? "ch-remaining" : "remaining"}">0:00</span></div>
          </div>
          <div class="np-controls">
            <button class="p-btn" data-p="prev" aria-label="Previous chapter" ${ch.length ? "" : "hidden"}>${I.prev}</button>
            <button class="p-btn lg" data-p="back" aria-label="Back 15 seconds">${I.back}</button>
            <button class="p-btn p-play xl" data-p="toggle" aria-label="Play">${I.play}</button>
            <button class="p-btn lg" data-p="fwd" aria-label="Forward 30 seconds">${I.fwd}</button>
            <button class="p-btn" data-p="next" aria-label="Next chapter" ${ch.length ? "" : "hidden"}>${I.next}</button>
          </div>
          <div class="np-extras">
            <button class="p-chip" data-p="speed"><span data-bind="speed">1×</span> speed</button>
            <button class="p-chip" data-p="sleep">${I.moon}<span data-bind="sleep">Sleep</span></button>
            <span class="np-left" data-bind="bookleft"></span>
          </div>
        </div>
        ${ch.length ? `
        <aside class="np-chapters">
          <h4>Chapters <small>${ch.length}</small></h4>
          <ol>${ch.map((c, i) => `
            <li><button data-p="chapter" data-i="${i}">
              <span class="n">${i + 1}</span><span class="t">${esc(c.title)}</span>
              <span class="d">${fmtTime((ch[i + 1]?.start ?? this.duration) - c.start)}</span>
            </button></li>`).join("")}
          </ol>
        </aside>` : ""}
      </div>`;
    const bg = $(".np-bg div", np);
    bg.style.backgroundImage = `url(${JSON.stringify(`/api/library/${b.id}/cover`)})`;
    requestAnimationFrame(() => np.classList.add("open"));
    document.body.style.overflow = "hidden";
    this.lastChapter = null;
    this.render();
    $(".np-chapters li.on")?.scrollIntoView({ block: "center" });
  }

  closeSheet() {
    const np = $("#np");
    if (!np) return;
    np.classList.remove("open");
    document.body.style.overflow = "";
    setTimeout(() => { if (!np.classList.contains("open")) np.remove(); }, 450);
  }

  render() {
    if (!this.book) return;
    const playing = this.playing;
    $$(".p-play").forEach((b) => {
      b.innerHTML = playing ? I.pause : I.play;
      b.setAttribute("aria-label", playing ? "Pause" : "Play");
    });
    $$('[data-bind="speed"]').forEach((n) => (n.textContent = `${this.speed}×`));
    $$('[data-bind="sleepdot"]').forEach((n) => (n.hidden = !this.sleep.mode));
    $$('[data-bind="finished"]').forEach((n) => (n.innerHTML = this.book.finished ? `${I.done} Finished` : "Mark finished"));
    $("#player")?.classList.toggle("is-playing", playing);
    $("#np")?.classList.toggle("is-playing", playing);
    this.renderTime();
  }

  renderTime() {
    if (!this.book) return;
    const t = this.time, dur = this.duration;
    const ci = this.chapterIndex(t);
    const [cs, ce] = this.chapterBounds(ci);
    const chapter = this.media.chapters[ci];

    if (!this.dragging) {
      const set = (sel, frac) => $$(sel).forEach((r) => {
        r.value = Math.round(frac * Number(r.max));
        r.style.setProperty("--fill", `${frac * 100}%`);
      });
      set('[data-scrub="book"]', dur ? t / dur : 0);
      set('[data-scrub="chapter"]', ce > cs ? (t - cs) / (ce - cs) : 0);
      $$('[data-bind="elapsed"]').forEach((n) => (n.textContent = fmtTime(t)));
      $$('[data-bind="ch-elapsed"]').forEach((n) => (n.textContent = fmtTime(t - cs)));
    }
    const rate = this.speed || 1;
    $$('[data-bind="remaining"]').forEach((n) => (n.textContent = `-${fmtTime((dur - t) / rate)}`));
    $$('[data-bind="ch-remaining"]').forEach((n) => (n.textContent = `-${fmtTime((ce - t) / rate)}`));
    $$('[data-bind="bookleft"]').forEach((n) => (n.textContent = dur ? `${fmtLeft((dur - t) / rate)} in book` : ""));
    const sleepLabel = this.sleep.mode === -1 ? "End of chapter" : this.sleep.mode > 0 ? `${Math.ceil((this.sleep.until - Date.now()) / 60000)}m left` : "Sleep";
    $$('[data-bind="sleep"]').forEach((n) => (n.textContent = sleepLabel));
    $$('[data-bind="miniprog"]').forEach((n) => (n.style.width = `${dur ? (t / dur) * 100 : 0}%`));
    $$('[data-bind="chapter"]').forEach((n) => (n.textContent = chapter ? chapter.title : this.book.author));
    $$('[data-bind="chapter-name"]').forEach((n) => (n.textContent = chapter ? `Chapter ${ci + 1} of ${this.media.chapters.length} · ${chapter.title}` : ""));

    if (this.lastChapter !== ci) {
      this.lastChapter = ci;
      $$(".np-chapters li").forEach((li, i) => li.classList.toggle("on", i === ci));
    }
  }
}

export const player = new Player();
