// The Audiobookshelf setup guide: two questions, then the exact steps for this setup,
// with this machine's own paths filled in, ending in a check that Audiobookshelf sees the books.

import { $, $$, esc, I, api } from "./util.js";

const INSTALLER = "curl -fsSL https://raw.githubusercontent.com/narratarr/narratarr/main/install.sh | bash";
const IN_ABS = "/narratarr"; // where Narratarr's folder appears inside Audiobookshelf's container
const HOST_FILL = "/path/it/printed";

const ICON = {
  same: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></svg>',
  other: '<svg viewBox="0 0 24 24"><rect x="2" y="5" width="9" height="7" rx="1.5"/><rect x="13" y="12" width="9" height="7" rx="1.5"/><path d="M6.5 12v3.5h6.5M17.5 12V8.5H11"/></svg>',
  docker: '<svg viewBox="0 0 24 24"><path d="M3 12h15.5a3 3 0 0 0 2.5-1.5M3 12c0 4 3 7 8 7 5.5 0 8.5-3.5 9.5-8.5"/><path d="M5 9h3v3H5zM8.5 9h3v3h-3zM12 9h3v3h-3zM8.5 5.5h3v3h-3z"/></svg>',
  direct: '<svg viewBox="0 0 24 24"><path d="M12 3v12"/><path d="m7.5 10.5 4.5 4.5 4.5-4.5"/><rect x="3" y="17" width="18" height="4" rx="1.5"/></svg>',
  folder: '<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/></svg>',
  copy: '<svg viewBox="0 0 24 24"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/></svg>',
};

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Plain http on a LAN or public address has no clipboard API; the old way still works there.
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;top:0;opacity:0";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch { /* nothing more to try */ }
    ta.remove();
    return ok;
  }
}

// A command or snippet with a Copy button. `shown` may carry markup (dimmed context lines,
// fill-in spans); what gets copied is `copy`, or the text of the lines marked to copy.
function block(shown, { label = "", copy = null } = {}) {
  return `<div class="cmd">${label ? `<span class="cmd-label">${esc(label)}</span>` : ""}
    <pre><code>${shown}</code></pre>
    <button class="cmd-copy" type="button" ${copy !== null ? `data-copy="${esc(copy)}"` : ""}>${ICON.copy}<span>Copy</span></button></div>`;
}
const fill = (key, text) => `<span class="fill" data-fill="${key}">${esc(text)}</span>`;
// One compose volume entry; quoted when the path has spaces or other characters YAML would trip on.
const volume = (host) => {
  const entry = `${host}:${IN_ABS}`;
  return /[\s#"'{}[\],&*!|>%@`]/.test(entry) ? `"${entry.replaceAll("\\", "\\\\").replaceAll('"', '\\"')}"` : entry;
};

function step(title, body, { done = false } = {}) {
  return { title, body, done };
}

// ---- the steps for each setup ------------------------------------------------------------------

function stepsFor(g, where, abs) {
  const lib = g.library_dir;
  const narratarrDocker = g.install === "docker";
  if (where === "other") return otherMachine(g);

  const steps = [];
  if (narratarrDocker) {
    steps.push(step("Find Narratarr’s audiobook folder on this server",
      `<p>Narratarr runs in Docker, where its books live at <code>/audiobooks</code>. Ask Docker which folder on the server that really is:</p>
       ${block(esc(`docker inspect narratarr --format '{{range .Mounts}}{{if eq .Destination "/audiobooks"}}{{.Source}}{{end}}{{end}}'`))}
       <label class="paste"><span>Paste what it printed</span>
         <input class="input mono" data-host placeholder="/home/you/audiobooks" autocomplete="off" spellcheck="false"></label>
       <p class="aside">The steps below fill it in for you.</p>`));
  }
  const hostLib = narratarrDocker ? fill("host", HOST_FILL) : esc(lib);

  if (abs === "docker") {
    const line = `      - ${volume(lib)}`;
    steps.push(step("Hand that folder to Audiobookshelf’s container",
      `<p>A container only sees the folders you give it. Open Audiobookshelf’s <code>docker-compose.yml</code> and add one line under <code>volumes:</code></p>
       ${block(`<span class="ctx">services:\n  audiobookshelf:\n    volumes:\n      - ./audiobooks:/audiobooks</span>\n<span class="add">      - ${narratarrDocker ? `${hostLib}:${IN_ABS}` : esc(volume(lib))}</span>`,
         { label: "docker-compose.yml", copy: narratarrDocker ? null : line })}
       <p class="aside">Can’t find the file? This prints the folder it’s in:</p>
       ${block(esc(`docker inspect audiobookshelf --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'`))}
       <p class="aside">Started with <code>docker run</code> instead? Add <code>-v "${narratarrDocker ? hostLib : esc(lib)}:${IN_ABS}"</code> when you start it again.</p>`));
    steps.push(step("Restart Audiobookshelf",
      `<p>In the folder with that file, recreate the container. Your Audiobookshelf settings and listening progress stay as they are.</p>
       ${block("docker compose up -d")}
       <p class="aside">Check that it can see your books. This should list your authors:</p>
       ${block(`docker exec audiobookshelf ls ${IN_ABS}`)}`));
  }

  const absPath = abs === "docker" ? esc(IN_ABS) : hostLib;
  const libs = g.libraries || [];
  const current = libs.find((l) => l.id === g.library_id);
  const want = abs === "docker" ? IN_ABS : narratarrDocker ? "" : lib.replace(/\/+$/, "");
  if (want && current?.folders?.some((f) => f.replace(/[\\/]+$/, "") === want)) {
    steps.push(step("Add the folder to a library in Audiobookshelf",
      `<p><b>${esc(current.name)}</b> already watches <code>${esc(want)}</code>. Nothing to do here.</p>`, { done: true }));
    steps.push(connectStep(g, true));
    steps.push(checkStep(g));
    return steps;
  }
  steps.push(step("Add the folder to a library in Audiobookshelf",
    `<ol class="clicks">
       <li>In Audiobookshelf, open <b>Settings → Libraries</b>.</li>
       <li>${libs.length ? `Edit ${current ? `<b>${esc(current.name)}</b>` : "your book library"}` : "Press <b>Add your first library</b>, with Media Type <b>Books</b>"}.</li>
       <li>Under <b>Folders</b>, type this path and press Enter: ${block(absPath, abs === "docker" || !narratarrDocker ? { copy: abs === "docker" ? IN_ABS : lib } : {})}</li>
       <li>Press <b>${libs.length ? "Save" : "Create"}</b>. Audiobookshelf scans the folder straight away.</li>
     </ol>
     ${current?.folders?.length ? `<p class="aside">${esc(current.name)} watches ${current.folders.map((f) => `<code>${esc(f)}</code>`).join(", ")} right now. Adding a folder keeps those.</p>` : ""}
     <p class="aside">Tip: set <b>Metadata Provider</b> to <b>Audible.com</b>. It knows narrators and series far better than Google Books.</p>
     ${abs === "direct" && g.os === "linux" ? `<p class="aside warn">On Linux, Audiobookshelf runs as its own user and needs permission to read this folder${lib.startsWith("/root") && !narratarrDocker ? `, and it can’t look inside <code>/root</code> at all. Move your library somewhere shared first, like <code>/srv/audiobooks</code>, under <b>Settings → Library</b>` : ". If the library stays empty, that’s why"}.</p>` : ""}`));

  steps.push(connectStep(g, true));
  steps.push(checkStep(g));
  return steps;
}

function otherMachine(g) {
  return [
    step("Install Narratarr on the Audiobookshelf machine",
      `<p>Audiobookshelf only scans its own disks, so the simplest fix is to have books land there. Run this on that machine (macOS or Linux). It can install with Docker too:</p>
       ${block(esc(INSTALLER))}
       <p class="aside">Then open Narratarr there, open this guide again and choose <b>On this machine too</b>.</p>`),
    step("Bring the books you already have",
      `<p>Copy <code>${esc(g.library_dir)}</code> from this machine to the new one. In the new Narratarr, use <b>Import</b>, pick that folder and choose <b>Leave where they are</b>, so nothing is downloaded twice.</p>`),
  ];
}

function connectStep(g, sameMachine) {
  if (!g.connected) {
    return step("Connect Narratarr to Audiobookshelf",
      `<p>So Audiobookshelf rescans the moment a book finishes. Narratarr finds it and signs in once.</p>
       <button class="btn primary" type="button" data-act="connect">${I.search} Connect now</button>`);
  }
  const overInternet = sameMachine && g.where === "internet";
  return step("Connect Narratarr to Audiobookshelf",
    `<p>Connected to <code>${esc(g.url)}</code>.</p>
     ${overInternet ? `<p class="aside warn">Both run on this machine, but Narratarr reaches Audiobookshelf over the internet${g.url.startsWith("http://") ? ", without https, so its key travels in the open" : ""}. Reconnect through this machine instead: in <b>Settings → Audiobookshelf</b>, press <b>Disconnect</b>, then <b>Find</b>.</p>` : ""}`,
    { done: !overInternet });
}

function checkStep(g) {
  return step("Check that Audiobookshelf sees your books",
    `<p>Narratarr asks Audiobookshelf to rescan, then looks for every book you’ve downloaded.</p>
     <button class="btn" type="button" data-act="check" ${g.connected && g.library_id ? "" : "disabled"}>${I.books} Check now</button>
     ${g.connected && !g.library_id ? `<p class="aside">Pick a library in <b>Settings → Audiobookshelf</b> first.</p>` : ""}
     ${g.connected ? "" : `<p class="aside">Connect first, in the step above.</p>`}
     <div class="check-result" data-check></div>`);
}

function checkHTML(r, abs) {
  const n = r.books, k = r.seen;
  const folders = r.folders.map((f) => `<code>${esc(f)}</code>`).join(", ") || "no folders";
  if (!n) {
    return `<div class="verdict">${I.clock}<div><b>Nothing downloaded yet</b><p>Once Narratarr finishes a book, check again.</p></div></div>`;
  }
  if (k === n) {
    return `<div class="verdict ok">${I.check}<div><b>Audiobookshelf sees ${n === 1 ? "your book" : `all ${n} of your books`}</b>
      <p>In <b>${esc(r.library)}</b>${r.seen_at ? `, at <code>${esc(r.seen_at)}</code>` : ""}. New downloads will show up there by themselves.</p></div></div>`;
  }
  const missing = r.missing.length ? `<ul>${r.missing.map((t) => `<li>${esc(t)}</li>`).join("")}${n - k > r.missing.length ? `<li>and ${n - k - r.missing.length} more</li>` : ""}</ul>` : "";
  if (k) {
    return `<div class="verdict part">${I.alert}<div><b>Audiobookshelf sees ${k} of your ${n} books</b>
      <p>Still missing:</p>${missing}
      <p>Books you imported and left where they were only show up if Audiobookshelf watches their folder too. A big scan can also take a while: check again in a minute.</p></div></div>`;
  }
  return `<div class="verdict bad">${I.x}<div><b>Audiobookshelf can’t see your books yet</b>
    <p><b>${esc(r.library)}</b> watches ${folders}, and none of Narratarr’s books are in there.
    ${abs === "docker" ? `Make sure <code>${IN_ABS}</code> is one of those folders, and that <code>docker exec audiobookshelf ls ${IN_ABS}</code> lists your authors.` : "Make sure Narratarr’s library folder is one of those."}</p></div></div>`;
}

// ---- the dialog ----------------------------------------------------------------------------------

export async function openAbsGuide({ onConnect } = {}) {
  const scrim = $("#modal");
  scrim.hidden = false;
  scrim.innerHTML = `
    <div class="modal wide guide" role="dialog" aria-modal="true" aria-labelledby="guide-title">
      <button class="guide-close" type="button" data-act="close" aria-label="Close">${I.x}</button>
      <span class="eyebrow">Setup guide</span>
      <h3 id="guide-title">Show your books in Audiobookshelf</h3>
      <p class="guide-lede">Audiobookshelf can only show books from folders it can see. Answer two questions and you’ll get the exact steps for your setup.</p>
      <div class="guide-body"><div class="guide-loading"><span class="spinner"></span></div></div>
    </div>`;
  const close = () => { scrim.hidden = true; scrim.innerHTML = ""; document.removeEventListener("keydown", onKey, true); };
  const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
  document.addEventListener("keydown", onKey, true);
  scrim.onclick = (e) => { if (e.target === scrim || e.target.closest('[data-act="close"]')) close(); };

  const body = $(".guide-body", scrim);
  let g;
  try {
    g = await api("/api/audiobookshelf/guide");
  } catch (err) {
    body.innerHTML = `<div class="verdict bad">${I.x}<div><b>Couldn’t load the guide</b><p>${esc(err.message)}</p></div></div>`;
    return;
  }

  // Best guesses from what Narratarr can see; the person can always pick the other answer.
  const sameGuess = g.where === "here" || g.where === "docker" || g.nearby ? "same"
    : g.where === "network" || g.where === "internet" ? "other" : "";
  const folders = (g.libraries || []).flatMap((l) => l.folders);
  const absGuess = g.where === "docker" ? "docker"
    : folders.some((f) => /^\/[^/]+\/?$/.test(f)) ? "docker" // a folder at the very top, like /audiobooks, is a container mount
    : folders.some((f) => /^[A-Za-z]:\\/.test(f) || /^\/(home|srv|mnt|media|Users|Volumes|opt|data)\//.test(f)) ? "direct" : "";
  const pick = { where: sameGuess, abs: absGuess };
  let host = "";

  const choice = (q, v, icon, title, sub, guess) => `
    <button class="choice ${pick[q] === v ? "on" : ""}" type="button" data-q="${q}" data-v="${v}" aria-pressed="${pick[q] === v}">
      <span class="choice-icon">${icon}</span>
      <span class="choice-text"><b>${title}</b><small>${sub}</small></span>
      ${guess === v ? `<em class="guess">Detected</em>` : ""}
    </button>`;

  function bridge() {
    if (pick.where === "other") {
      return `<div class="bridge apart">
        <div class="end"><span class="end-icon"><img src="/static/icon.svg" alt=""></span><small>Narratarr, here</small><code>${esc(g.library_dir)}</code></div>
        <div class="link broken"><span class="line"></span><span class="mid">${I.x}</span></div>
        <div class="end"><span class="end-icon abs">${I.headphones}</span><small>Audiobookshelf, elsewhere</small><code>its own disks</code></div>
      </div>`;
    }
    const docker = g.install === "docker";
    const left = docker ? (host || "your audiobook folder") : g.library_dir;
    const right = pick.abs === "docker" ? IN_ABS : left;
    return `<div class="bridge">
      <div class="end"><span class="end-icon"><img src="/static/icon.svg" alt=""></span><small>Narratarr saves to</small><code data-bridge>${esc(left)}</code></div>
      <div class="link"><span class="line"></span><span class="mid">${ICON.folder}</span></div>
      <div class="end"><span class="end-icon abs">${I.headphones}</span><small>Audiobookshelf reads</small><code data-bridge-abs>${esc(right)}</code></div>
    </div>
    <p class="bridge-note">${pick.abs === "docker"
      ? `The same folder, under two names: Docker shows it to Audiobookshelf as <code>${IN_ABS}</code>.`
      : "The same folder, at the same path. Audiobookshelf only needs to be told about it."}</p>`;
  }

  function render() {
    const ready = pick.where === "other" || (pick.where === "same" && pick.abs);
    const steps = ready ? stepsFor(g, pick.where, pick.abs) : [];
    body.innerHTML = `
      <div class="guide-q">
        <span class="q-label">Where does Audiobookshelf run?</span>
        <div class="choices">
          ${choice("where", "same", ICON.same, "On this machine too", "The same computer or server as Narratarr", sameGuess)}
          ${choice("where", "other", ICON.other, "On a different machine", "Another computer, a NAS or a server", sameGuess)}
        </div>
      </div>
      ${pick.where === "same" ? `
      <div class="guide-q">
        <span class="q-label">How is Audiobookshelf installed?</span>
        <div class="choices">
          ${choice("abs", "docker", ICON.docker, "In Docker", "A container, from a compose file or docker run", absGuess)}
          ${choice("abs", "direct", ICON.direct, "Installed directly", "An app, a package or the Windows installer", absGuess)}
        </div>
      </div>` : ""}
      ${ready ? `${bridge()}
      <ol class="g-steps">${steps.map((s, i) => `
        <li class="g-step ${s.done ? "done" : ""}" style="--i:${i}">
          <span class="num">${s.done ? I.check : i + 1}</span>
          <div class="step-body"><h4>${s.title}</h4>${s.body}</div>
        </li>`).join("")}
      </ol>
      ${pick.where === "other" ? alternative() : ""}` : `<p class="guide-wait">Pick an answer to see your steps.</p>`}`;
    applyHost();
  }

  function alternative() {
    return `<details class="guide-alt">
      <summary>Or keep Narratarr here and save straight onto the Audiobookshelf machine</summary>
      <ol class="clicks">
        <li>On the Audiobookshelf machine, share its audiobook folder over the network (SMB or NFS).</li>
        <li>Connect to the share on this machine. On a Mac: Finder → <b>Go → Connect to Server</b>.</li>
        <li>In Narratarr, set <b>Settings → Library → Library folder</b> to where the share appears, like <code>/Volumes/audiobooks</code>.</li>
      </ol>
      <p class="aside warn">This machine has to be on, with the share connected, whenever a book finishes. Torrents are copied rather than hard-linked across the network, so they take twice the space while seeding.</p>
      ${g.connected ? `<button class="btn" type="button" data-act="check" ${g.library_id ? "" : "disabled"}>${I.books} Check now</button><div class="check-result" data-check></div>` : ""}
    </details>`;
  }

  // Narratarr in Docker can't see the server's path for its folder; once pasted, it fills the steps.
  function applyHost() {
    const shown = host || HOST_FILL;
    $$('[data-fill="host"]', body).forEach((el) => { el.textContent = shown; el.classList.toggle("filled", Boolean(host)); });
    const b = $("[data-bridge]", body);
    if (b && g.install === "docker") {
      b.textContent = host || "your audiobook folder";
      const a = $("[data-bridge-abs]", body);
      if (a && pick.abs !== "docker") a.textContent = host || "your audiobook folder";
    }
    const input = $("[data-host]", body);
    if (input && input.value !== host) input.value = host;
  }

  body.oninput = (e) => {
    if (!e.target.matches("[data-host]")) return;
    host = e.target.value.trim();
    applyHost();
  };

  body.onclick = async (e) => {
    const c = e.target.closest(".choice");
    if (c) {
      pick[c.dataset.q] = c.dataset.v;
      render();
      return;
    }
    const cp = e.target.closest(".cmd-copy");
    if (cp) {
      const code = $("code", cp.parentElement);
      const added = $$(".add", code).map((n) => n.textContent).join("\n");
      const text = cp.dataset.copy ?? (added || code.textContent);
      const ok = await copyText(text);
      cp.classList.toggle("copied", ok);
      $("span", cp).textContent = ok ? "Copied" : "Select and copy";
      setTimeout(() => { cp.classList.remove("copied"); $("span", cp).textContent = "Copy"; }, 1800);
      return;
    }
    if (e.target.closest('[data-act="connect"]')) {
      close();
      onConnect?.();
      return;
    }
    const chk = e.target.closest('[data-act="check"]');
    if (chk) {
      const out = $("[data-check]", chk.parentElement);
      chk.disabled = true;
      out.innerHTML = `<div class="verdict wait"><span class="spinner"></span><div><b>Asking Audiobookshelf to rescan…</b><p>This takes a few seconds.</p></div></div>`;
      try {
        const r = await api("/api/audiobookshelf/check", { method: "POST", body: { scan: true } });
        out.innerHTML = checkHTML(r, pick.abs);
        if (r.books && r.seen === r.books) {
          // It works, so every step before it did too.
          $$(".g-step", body).forEach((li) => { li.classList.add("done"); $(".num", li).innerHTML = I.check; });
        }
      } catch (err) {
        out.innerHTML = `<div class="verdict bad">${I.x}<div><b>Couldn’t check</b><p>${esc(err.message)}</p></div></div>`;
      }
      chk.disabled = false;
    }
  };

  render();
}
