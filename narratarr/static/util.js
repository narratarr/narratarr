// Small helpers shared by the app and the player.

export const $ = (sel, el = document) => el.querySelector(sel);
export const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ESC[c]);

export const I = {
  play: '<svg viewBox="0 0 24 24"><path d="M7 4.5v15a1 1 0 0 0 1.5.86l12.4-7.5a1 1 0 0 0 0-1.72L8.5 3.64A1 1 0 0 0 7 4.5Z" fill="currentColor" stroke="none"/></svg>',
  pause: '<svg viewBox="0 0 24 24"><rect x="6" y="4" width="4" height="16" rx="1.2" fill="currentColor" stroke="none"/><rect x="14" y="4" width="4" height="16" rx="1.2" fill="currentColor" stroke="none"/></svg>',
  back: '<svg viewBox="0 0 24 24"><path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1"/><path d="M3 4v4.5h4.5"/><text x="12" y="15.6" text-anchor="middle" font-size="7.5" font-weight="700" font-family="Inter,sans-serif" fill="currentColor" stroke="none">15</text></svg>',
  fwd: '<svg viewBox="0 0 24 24"><path d="M20.5 12a8.5 8.5 0 1 1-2.6-6.1"/><path d="M21 4v4.5h-4.5"/><text x="12" y="15.6" text-anchor="middle" font-size="7.5" font-weight="700" font-family="Inter,sans-serif" fill="currentColor" stroke="none">30</text></svg>',
  prev: '<svg viewBox="0 0 24 24"><path d="M6 5v14"/><path d="M19 5.5v13a.8.8 0 0 1-1.2.7L8.5 12.7a.8.8 0 0 1 0-1.4l9.3-6.5a.8.8 0 0 1 1.2.7Z" fill="currentColor"/></svg>',
  next: '<svg viewBox="0 0 24 24"><path d="M18 5v14"/><path d="M5 5.5v13a.8.8 0 0 0 1.2.7l9.3-6.5a.8.8 0 0 0 0-1.4L6.2 4.8A.8.8 0 0 0 5 5.5Z" fill="currentColor"/></svg>',
  moon: '<svg viewBox="0 0 24 24"><path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5Z"/></svg>',
  list: '<svg viewBox="0 0 24 24"><path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1" fill="currentColor"/><circle cx="4.5" cy="12" r="1" fill="currentColor"/><circle cx="4.5" cy="18" r="1" fill="currentColor"/></svg>',
  chevron: '<svg viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>',
  expand: '<svg viewBox="0 0 24 24"><path d="m6 15 6-6 6 6"/></svg>',
  done: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="m8 12.5 3 3 5-6"/></svg>',
  plus: '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
  check: '<svg viewBox="0 0 24 24"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>',
  down: '<svg viewBox="0 0 24 24"><path d="M12 4v11"/><path d="m7.5 10.5 4.5 4.5 4.5-4.5"/><path d="M5 20h14"/></svg>',
  retry: '<svg viewBox="0 0 24 24"><path d="M4 12a8 8 0 0 1 14-5.3L20 9"/><path d="M20 4v5h-5"/><path d="M20 12a8 8 0 0 1-14 5.3L4 15"/><path d="M4 20v-5h5"/></svg>',
  stop: '<svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
  trash: '<svg viewBox="0 0 24 24"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/></svg>',
  bookmark: '<svg viewBox="0 0 24 24"><path d="M6 4h12v17l-6-4-6 4z"/></svg>',
  clock: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
  disc: '<svg viewBox="0 0 24 24"><path d="M4 7c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3Z"/><path d="M4 7v10c0 1.7 3.6 3 8 3s8-1.3 8-3V7"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/></svg>',
  wave: '<svg viewBox="0 0 24 24"><path d="M4 10v4M8 7v10M12 4v16M16 8v8M20 11v2"/></svg>',
  file: '<svg viewBox="0 0 24 24"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>',
  globe: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/></svg>',
  search: '<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>',
  key: '<svg viewBox="0 0 24 24"><circle cx="8" cy="15" r="4"/><path d="m10.8 12.2 8.7-8.7M17 6l3 3M14.5 8.5l2 2"/></svg>',
  alert: '<svg viewBox="0 0 24 24"><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/></svg>',
  x: '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg>',
  books: '<svg viewBox="0 0 24 24"><path d="M4 19V5a1 1 0 0 1 1-1h3v16H5a1 1 0 0 1-1-1Z"/><path d="M8 4h4v16H8"/><path d="m14 5.2 3.6-.9 3.2 13.6-3.6.9z"/></svg>',
  headphones: '<svg viewBox="0 0 24 24"><path d="M4 15v-3a8 8 0 0 1 16 0v3"/><rect x="3" y="14" width="5" height="7" rx="2"/><rect x="16" y="14" width="5" height="7" rx="2"/></svg>',
  book: '<svg viewBox="0 0 24 24"><path d="M12 6.8C10.4 5.3 7.9 4.6 3.5 4.6v13.2c4.4 0 6.9.7 8.5 2.2 1.6-1.5 4.1-2.2 8.5-2.2V4.6c-4.4 0-6.9.7-8.5 2.2Z"/><path d="M12 6.8V20"/></svg>',
};

export async function api(path, opts = {}) {
  const init = { ...opts, headers: { "Content-Type": "application/json", ...(opts.headers || {}) } };
  if (init.body && typeof init.body !== "string") init.body = JSON.stringify(init.body);
  const res = await fetch(path, init);
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  // Signed out (the session expired, or a password changed elsewhere): the app shows the sign-in screen.
  if (res.status === 401 && !path.startsWith("/api/auth/")) window.dispatchEvent(new Event("narratarr:signed-out"));
  if (!res.ok) throw new Error((data && (typeof data.detail === "string" ? data.detail : "Request failed")) || res.statusText);
  return data;
}

export function hue(str) {
  let h = 0;
  for (const c of String(str)) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return h % 360;
}

export function coverHTML(b, cls = "") {
  const h = hue(b.title + b.author);
  // Downloaded books use the cover saved next to the audio; everything else uses the source's image.
  const src = b.id && b.path ? `/api/library/${b.id}/cover` : b.cover_url || "";
  // Book covers are taller than audiobook squares: show the whole cover, standing on a blur of itself.
  const book = b.kind === "ebook" || b.source === "openlibrary";
  const tag = (extra) => `<img ${extra} src="${esc(src)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" onload="this.classList.add('loaded')" onerror="this.remove()">`;
  const img = /^(https?:\/\/|\/api\/)/.test(src) ? (book ? tag('class="blur" aria-hidden="true"') : "") + tag("") : "";
  return `<div class="cover ${book ? "book" : ""} ${cls}" style="--h:${h}"><div class="faux"><small>${esc(firstAuthor(b.author))}</small><b>${esc(b.title)}</b></div>${img}</div>`;
}

export const firstAuthor = (a) => String(a || "").split(/,| and /)[0].trim();

export function fmtBytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB"];
  const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), u.length - 1);
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
}

export function ago(ts) {
  if (!ts) return "";
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function toast(msg, { kind = "", cover = "" } = {}) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  const icon = kind === "err" ? I.alert : I.check;
  el.innerHTML = (/^https?:\/\//.test(cover)
    ? `<img src="${esc(cover)}" alt="" referrerpolicy="no-referrer" onerror="this.remove()">`
    : `<span class="dot">${icon}</span>`) + `<span>${esc(msg)}</span>`;
  $("#toasts").append(el);
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 300); }, kind === "err" ? 5200 : 3400);
}

export function fmtTime(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

export function fmtLeft(sec) {
  sec = Math.max(0, sec || 0);
  if (sec < 60) return `${Math.ceil(sec)}s left`;
  const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  return h ? `${h}h ${m}m left` : `${m}m left`;
}
