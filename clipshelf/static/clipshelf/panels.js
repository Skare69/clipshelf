/* clipshelf web app — library, inbox, collections, settings panels. */
"use strict";

import { $, FALLBACK, ICONS, S, api, avatar, banner, captureActive, captureBad, catsFresh, confirmDialog, copyBtn, csrf, el, fmtDate, fmtWhen, hostOf, icon, note, onEnter, out, refreshMe, safeUrl, setView, shortHost, toast, uuid } from "./core.js";

/* ---------- library ---------- */
const qInput = $("#q"), qClear = $("#qclear"), tagSel = $("#tag"), kindSel = $("#kind");
function skeleton() {
  out.replaceChildren(el("div", { class: "cards", role: "status",
    "aria-label": "Loading library" },
    ...Array.from({ length: 8 }, () => el("div", { class: "skel" }))));
}
async function loadEntries(reset) {
  if (!S.me) return;
  if (reset) { S.offset = 0; S.entries = []; catsFresh(); }
  const seq = ++S.seq;
  if (!S.entries.length) skeleton();
  try {
    const p = new URLSearchParams({ collection_id: S.col, sort: S.srt,
      offset: S.offset, limit: S.limit });
    if (S.q) p.set("q", S.q);
    if (S.cat) p.set("cat", S.cat);
    if (S.tag) p.set("tag", S.tag);
    if (S.kind) p.set("kind", S.kind);
    const j = await api("/api/entries?" + p);
    if (seq !== S.seq) return;
    S.entries = S.entries.concat(j.entries || []);
    S.count = j.count || 0;
    if (!S.q && !S.cat && !S.tag && !S.kind) S.total = S.count;
    for (const e of j.entries || []) {
      const c = note(e) || "Other";
      S.cats.set(c, (S.cats.get(c) || 0) + 1);
      for (const t of e.tags || []) S.tags.add(t);
    }
    $("#nLib").textContent = S.total || "";
    rebuildTagSel();
    renderLibrary();
  } catch (e) { if (e.status !== 401) renderError(e); }
}
function rebuildTagSel() {
  const cur = S.tag;
  tagSel.replaceChildren(el("option", { value: "", text: "all tags" }),
    ...[...S.tags].sort().map(t => el("option", { value: t, text: t })));
  if (cur && !S.tags.has(cur)) tagSel.append(el("option", { value: cur, text: cur }));
  tagSel.value = cur;
}
function renderError(e) {
  out.replaceChildren(el("div", { class: "empty", role: "alert" },
    el("b", { text: e.message || "Something went wrong." }), " ",
    el("button", { class: "btn small", type: "button", onclick: () => setView(S.view) }, "Retry")));
}
function entryMeta(e) {
  const bits = [];
  const c = note(e);
  if (c) bits.push(el("span", { class: "badge", text: c }));
  const contrib = (e.contributors || []).map(x => x.email).filter(Boolean);
  if (contrib.length) bits.push(el("span", { text: "by " + contrib.join(", ") }));
  const srcs = e.sources || [];
  if (srcs.length) bits.push(el("button", { class: "btn small", type: "button",
    "data-detail": e.id, text: `${srcs.length} source${srcs.length > 1 ? "s" : ""}` }));
  if (e.found) bits.push(el("span", { class: "date", text: fmtDate(e.found) }));
  return el("div", { class: "meta" }, ...bits);
}
function linkCard(e) {
  const url = safeUrl(e.url);
  const title = e.title || (url ? hostOf(e.url) : "Untitled");
  const kids = [
    el("div", { class: "head" },
      avatar(title),
      el("div", { style: "min-width:0" },
        url ? el("a", { class: "title", href: url, target: "_blank",
          rel: "noopener noreferrer nofollow", text: title })
            : el("span", { class: "title", text: title }),
        e.desc ? el("div", { class: "desc clamp3", text: e.desc }) : null)),
    (e.tags || []).length ? el("div", {}, ...e.tags.slice(0, 8)
      .map(t => el("span", { class: "tag", text: t }))) : null,
    el("div", { class: "acts" },
      e.install ? copyBtn(() => e.install, "copy install") : null,
      el("button", { class: "btn small", type: "button", "data-detail": e.id }, "sources & status")),
    entryMeta(e)
  ];
  const card = el("li", { class: "card" }, ...kids);
  if (e.can_remove) card.append(el("button", { class: "x", type: "button",
    "aria-label": `Remove ${title}`, "data-rm": e.id }, "✕"));
  return card;
}
function promptCard(e) {
  const card = el("li", { class: "card prompt" },
    el("div", { class: "head" },
      avatar(e.title || note(e) || "p"),
      el("div", { style: "min-width:0" },
        el("span", { class: "title", text: e.title || "Untitled prompt" })),
      copyBtn(() => e.text, "copy prompt")),
    el("pre", { text: e.text || "" }),
    el("div", { class: "acts" },
      e.install ? copyBtn(() => e.install, "copy install") : null,
      el("button", { class: "btn small", type: "button", "data-detail": e.id }, "sources & status")),
    entryMeta(e));
  if (e.can_remove) card.append(el("button", { class: "x", type: "button",
    "aria-label": `Remove ${e.title || "prompt"}`, "data-rm": e.id }, "✕"));
  return card;
}
function renderLibrary() {
  const frag = document.createDocumentFragment();
  frag.append(el("h1", {}, "Library ",
    el("span", { class: "sub", text: `${S.count} of ${S.total || S.count} in ${colName()}` })));
  if (!S.entries.length) {
    frag.append(filteredEmpty());
  } else {
    if (S.srt === "az" && !S.kind) {
      const groups = new Map();
      for (const e of S.entries) {
        const c = note(e) || "Other";
        if (!groups.has(c)) groups.set(c, []);
        groups.get(c).push(e);
      }
      const cats = [...groups.keys()].sort((a, b) =>
        a === "Other" ? 1 : b === "Other" ? -1 : a.localeCompare(b));
      for (const c of cats) {
        const list = groups.get(c);
        frag.append(el("h2", { id: "g-" + slug(c) }, c + " ",
          el("span", { class: "sub", text: `(${list.length})` })));
        frag.append(listWrap(list));
      }
    } else frag.append(listWrap(S.entries));
    if (S.entries.length < S.count)
      frag.append(el("div", { class: "row2" },
        el("button", { class: "btn tonal", type: "button",
          onclick: () => { S.offset = S.entries.length; loadEntries(false); } },
          `Show more (${S.count - S.entries.length} remaining)`)));
  }
  out.replaceChildren(frag);
  renderToc();
}
const slug = c => c.toLowerCase().replace(/[^a-z0-9]+/g, "-");
function listWrap(list) {
  return S.vt === "grid"
    ? el("ul", { class: "cards" }, ...list.map(e => e.kind === "prompt" ? promptCard(e) : linkCard(e)))
    : el("ul", { class: "rows" }, ...list.map(e => e.kind === "prompt"
        ? el("li", {}, promptCard(e)) : linkRow(e)));
}
function linkRow(e) {
  const url = safeUrl(e.url);
  const title = e.title || (url ? hostOf(e.url) : "Untitled");
  return el("li", { class: "row" },
    el("div", { style: "min-width:0" },
      el("div", { class: "rhead" },
        url ? el("a", { class: "title", href: url, target: "_blank",
            rel: "noopener noreferrer nofollow", text: title })
            : el("span", { class: "title", text: title }),
        ...(e.tags || []).slice(0, 6).map(t => el("span", { class: "tag", text: t }))),
      e.desc ? el("div", { class: "desc clamp1", text: e.desc }) : null,
      entryMeta(e)),
    el("div", { class: "rside" },
      e.install ? copyBtn(() => e.install, "copy install") : null,
      el("button", { class: "btn small", type: "button", "data-detail": e.id }, "sources & status"),
      e.can_remove ? el("button", { class: "x", type: "button",
        "aria-label": `Remove ${title}`, "data-rm": e.id }, "✕") : null));
}
function filteredEmpty() {
  if (S.q || S.cat || S.tag || S.kind) {
    const bits = [];
    if (S.q) bits.push(`matching “${S.q}”`);
    if (S.cat) bits.push(`in category “${S.cat}”`);
    if (S.tag) bits.push(`tagged “${S.tag}”`);
    if (S.kind) bits.push(`of kind ${S.kind}`);
    return el("div", { class: "empty" }, el("b", { text: "No entries " + bits.join(" ") + "." }),
      " ", el("button", { class: "btn small", type: "button", onclick: resetFilters }, "Clear filters"));
  }
  return el("div", { class: "empty" },
    el("b", { text: `Nothing in ${colName()} yet.` }), " Paste links with the + button — ",
    "interpreted repositories, pages and prompts land here as the server processes them.");
}
function resetFilters() {
  S.q = ""; S.cat = ""; S.tag = ""; S.kind = "";
  qInput.value = ""; qClear.hidden = true; kindSel.value = ""; rebuildTagSel();
  loadEntries(true);
}
function renderToc() {
  const tsec = $("#tsec");
  if (S.view !== "library") { tsec.replaceChildren(); return; }
  const cats = [...S.cats.keys()].sort((a, b) =>
    a === "Other" ? 1 : b === "Other" ? -1 : a.localeCompare(b));
  tsec.replaceChildren(
    el("a", { href: "#library", class: S.cat ? "" : "on",
      onclick: e => { e.preventDefault(); S.cat = ""; loadEntries(true); } },
      "All categories"),
    ...cats.map(c => el("a", { href: "#g-" + slug(c), class: S.cat === c ? "on" : "",
      onclick: e => { e.preventDefault(); S.cat = c; loadEntries(true); } },
      c, el("span", { class: "count", text: String(S.cats.get(c)) }))));
}
const colName = () => (S.me?.collections || []).find(c => c.id === S.col)?.name || "this collection";

/* card actions (delegated) */
out.addEventListener("click", async e => {
  const rm = e.target.closest("[data-rm]");
  if (rm) {
    const vals = await confirmDialog({ head: "Remove entry?",
      text: "Your contributions to this entry are removed from this collection. " +
        "Other members' contributions and retained sources follow collection rules.",
      ok: "Remove" });
    if (!vals) return;
    try { await api("/api/entries/" + rm.dataset.rm, { method: "DELETE" });
      toast("Entry removed."); loadEntries(true);
    } catch (err) { if (err.status !== 401) banner("err", err.message, { sticky: true }); }
    return;
  }
  const det = e.target.closest("[data-detail]");
  if (det) openDetail(det.dataset.detail);
});

/* ---------- entry detail: sources, assets, jobs ---------- */
const detailDlg = $("#detailDlg"), detBody = $("#detBody");
async function openDetail(id) {
  detBody.replaceChildren(el("p", { class: "hint", text: "Loading…" }));
  if (!detailDlg.open) detailDlg.showModal();
  let j;
  try { j = await api("/api/entries/" + id); }
  catch (e) {
    if (e.status === 401) return;
    detBody.replaceChildren(el("p", { class: "err", text: e.message })); return;
  }
  const e = j.entry || {};
  $("#detHead").textContent = e.title || (e.url ? shortHost(e.url) : "Entry");
  const url = safeUrl(e.url);
  detBody.replaceChildren(
    el("div", { class: "row2" },
      url ? el("a", { href: url, target: "_blank", rel: "noopener noreferrer nofollow" },
        icon(ICONS.ext), " ", shortHost(e.url)) : null,
      note(e) ? el("span", { class: "badge", text: note(e) }) : null,
      el("span", { class: "badge " + (e.interpreted ? "ok" : ""), text: e.interpreted ? "interpreted" : "not interpreted" })),
    (e.tags || []).length ? el("div", { class: "row2" },
      ...e.tags.map(t => el("span", { class: "tag", text: t }))) : null,
    e.desc ? el("p", { text: e.desc }) : null,
    (e.contributors || []).length ? el("p", { class: "hint",
      text: "Contributors: " + e.contributors.map(c => c.email).join(", ") }) : null,
    e.found ? el("p", { class: "hint", text: "Found " + fmtWhen(e.found) + "." }) : null,
    e.install ? el("div", { class: "panel" }, el("h3", { text: "Install instructions (copy only — never executed)" }),
      el("pre", { text: e.install }), el("div", { class: "row2" }, copyBtn(() => e.install, "copy install"))) : null,
    e.text ? el("div", {}, el("h3", { text: "Extracted text" }), el("pre", { text: e.text })) : null,
    (e.sources || []).length ? el("div", {}, el("h3", { text: "Sources" }),
      el("ul", { class: "list" }, ...e.sources.map(s => {
        const u = safeUrl(typeof s === "string" ? s : s.url);
        const t = (typeof s === "object" && s.title) || u || String(s);
        return el("li", {}, u ? el("a", { href: u, target: "_blank", rel: "noopener noreferrer nofollow", text: t })
          : el("span", { text: t }));
      }))) : null,
    el("h3", { text: "Retained sources" }),
    renderAssets(j.assets || []),
    el("h3", { text: "Processing jobs" }),
    ...(j.jobs || []).length ? j.jobs.map(job => jobRow(job, () => openDetail(id)))
      : [el("p", { class: "hint", text: "No jobs recorded for this entry." })]
  );
}
function renderAssets(assets) {
  if (!assets.length) return el("p", { class: "hint",
    text: "No source files retained for this entry." });
  const grid = el("div", { class: "thumbs" });
  const other = [];
  for (const a of assets) {
    const u = safeUrl(a.url);
    if (!u) continue;
    if (a.kind === "image" && /^image\//.test(a.content_type || ""))
      grid.append(el("a", { href: u, target: "_blank", rel: "noopener" },
        el("img", { src: u, alt: a.name || "retained image", loading: "lazy" })));
    else if (a.kind === "video" && /^video\//.test(a.content_type || ""))
      grid.append(el("video", { controls: true, preload: "metadata", src: u }));
    else if (a.kind === "audio" && /^audio\//.test(a.content_type || ""))
      grid.append(el("audio", { controls: true, preload: "metadata", src: u }));
    else other.push(a);
  }
  const wrap = el("div", {});
  if (grid.childNodes.length) wrap.append(grid);
  for (const a of other) {
    const u = safeUrl(a.url); if (!u) continue;
    const row = el("div", { class: "row2" },
      el("span", { class: "badge", text: a.kind || (a.content_type || "").split("/")[0] || "file" }),
      el("a", { href: u, target: "_blank", rel: "noopener" }, a.name || u));
    if (/^text\//.test(a.content_type || "") || a.kind === "page" || a.kind === "captions") {
      const view = el("button", { class: "btn small", type: "button" }, icon(ICONS.eye), "view text");
      view.addEventListener("click", async () => {
        view.disabled = true;
        try {
          const r = await fetch(u, { credentials: "same-origin" });
          const txt = await r.text();
          row.after(el("pre", { text: txt.slice(0, 40000), class: "term" }));
        } catch { banner("err", "Could not load the saved text.", { sticky: true }); }
        view.disabled = false;
      });
      row.append(view);
    }
    wrap.append(row);
  }
  return wrap;
}

/* ---------- inbox ---------- */
async function loadCaptures() {
  if (!S.me) return;
  try {
    const j = await api("/api/captures?collection_id=" + S.col);
    S.captures = j.captures || [];
    S.capsLoaded = true;
    refreshInboxCount(); renderInbox();
  } catch (e) { if (e.status !== 401) renderError(e); }
}
function pollCaptures() {
  clearInterval(S.capTimer);
  if (S.view !== "inbox") return;
  S.capTimer = setInterval(async () => {
    if (S.capsLoaded && !S.captures.some(captureActive)) { clearInterval(S.capTimer); renderInbox(); return; }
    try {
      const j = await api("/api/captures?collection_id=" + S.col);
      S.captures = j.captures || [];
      S.capsLoaded = true;
      refreshInboxCount(); renderInbox();
    } catch {}
  }, 3000);
}
async function refreshInboxCount() {
  try {
    const j = await api("/api/captures?collection_id=" + S.col);
    S.inboxActive = (j.captures || []).filter(captureActive).length;
    $("#nInbox").textContent = S.inboxActive || "";
  } catch {}
}
const jobChips = j => [
  el("span", { class: "badge " + (j.state === "done" ? "ok" : j.active ? "run" : j.needs_attention ? "err" : ""), text: "job: " + j.state }),
  j.acquisition ? el("span", { class: "badge " + ({ complete: "ok", pending: "", partial: "warn", blocked: "err", error: "err" }[j.acquisition] ?? ""), text: "acquisition: " + j.acquisition }) : null,
  j.interpretation ? el("span", { class: "badge " + ({ complete: "ok", pending: "", blocked: "err", error: "err" }[j.interpretation] ?? ""), text: "interpretation: " + j.interpretation }) : null,
  j.guardrail ? el("span", { class: "badge err", title: j.error, text: "guardrail" }) : null,
  (j.attempts > 1) ? el("span", { class: "badge", text: `${j.attempts} attempts` }) : null
];
function jobRow(j, onDone) {
  const sw = j.screening_warnings || [];
  const row = el("div", { class: "job" },
    el("div", { class: "jhead" },
      el("span", { class: "jurl", text: hostOf(j.final_url || j.url) }),
      ...jobChips(j),
      sw.length ? el("span", { class: "badge", title: sw.join("\n"), text: "screened" }) : null),
    (j.warnings || []).length ? el("div", { class: "jwarn",
      text: j.warnings.map(w => "⚠ " + w).join("\n"), style: "white-space:pre-wrap" }) : null,
    j.error ? el("div", { class: "jerr" + (j.guardrail ? " guardrail" : ""), text: j.error }) : null);
  // can_retry is the action the server will accept right now; completed jobs
  // stay reprocessable, so the offer appears there too.
  if (j.can_retry) {
    const b = el("button", { class: "btn small", type: "button" }, icon(ICONS.retry), "retry");
    b.addEventListener("click", async () => {
      b.disabled = true;
      try {
        await api("/api/jobs/" + j.id + "/retry", { method: "POST", body: {} });
        toast("Job requeued — the worker picks it up shortly.");
        onDone?.();
      } catch (e) { if (e.status !== 401) banner("err", e.message, { sticky: true }); b.disabled = false; }
    });
    row.append(el("div", { class: "acts" }, b));
  }
  return row;
}
function renderInbox() {
  const frag = document.createDocumentFragment();
  frag.append(el("h1", {}, "Inbox ",
    el("span", { class: "sub", text: colName() })));
  let caps = S.captures;
  if (S.capFilter === "active") caps = caps.filter(captureActive);
  if (S.capFilter === "attention") caps = caps.filter(captureBad);
  const all = S.captures, act = all.filter(captureActive).length, bad = all.filter(captureBad).length;
  frag.append(el("div", { class: "row2" },
    ...[["all", `All (${all.length})`], ["active", `Active (${act})`], ["attention", `Needs attention (${bad})`]]
      .map(([f, label]) => el("button", { class: "stb" + (S.capFilter === f ? " on" : ""),
        type: "button", onclick: () => { S.capFilter = f; renderInbox(); } }, label))));
  if (!caps.length) {
    frag.append(el("div", { class: "empty" },
      el("b", { text: S.capFilter === "all" ? "No captures in this collection yet." :
        S.capFilter === "active" ? "Nothing is processing right now." : "Nothing needs attention." }),
      S.capFilter === "all" ? " Paste links with the + button, or import a TikTok export or an existing library.json." : ""));
  }
  for (const c of caps) {
    const jobs = c.jobs || [];
    frag.append(el("article", { class: "cap" },
      el("div", { class: "row2" },
        el("strong", { text: "Capture" }),
        el("span", { class: "when", text: fmtWhen(c.receipt?.received_at || c.received_at) }),
        c.receipt?.notice ? el("span", { class: "pend", title:
          "The requested destination was unavailable; the server fell back.", text: "notice: " + c.receipt.notice }) : null),
      el("pre", { class: "clamp3", text: c.text || "" }),
      ...jobs.map(j => jobRow(j, () => loadCaptures())),
      !jobs.length ? el("p", { class: "hint", text: "No urls detected in this text." }) : null));
  }
  out.replaceChildren(frag);
  renderToc();
}

/* ---------- add dialog: capture + import ---------- */
const addDlg = $("#addDlg"), capText = $("#capText"), capErr = $("#capErr"),
  capCol = $("#capCol"), capBtn = $("#capBtn");
$("#addFab").addEventListener("click", () => {
  capErr.hidden = true;
  capCol.replaceChildren(...(S.me?.collections || []).map(c =>
    el("option", { value: c.id, text: c.name + (c.kind === "personal" ? " (personal)" : "") })));
  capCol.value = S.col;
  addDlg.showModal();
});
capBtn.addEventListener("click", async () => {
  const text = capText.value.trim();
  capErr.hidden = true;
  if (!text) { capErr.textContent = "Paste at least one link."; capErr.hidden = false; return; }
  capBtn.disabled = true;
  try {
    const j = await api("/api/captures", { method: "POST", body: {
      client_request_id: uuid(), text, collection_id: capCol.value,
      instance_id: S.me.instance_id, user_id: S.me.user.id } });
    addDlg.close(); capText.value = "";
    banner("ok", "Capture accepted." + (j.receipt?.notice ? " " + j.receipt.notice : ""), { ms: 6000 });
    if (capCol.value !== S.col) { S.col = capCol.value; catsFresh(); }
    setView("inbox");
    refreshInboxCount();
  } catch (e) {
    if (e.status === 401) return;
    capErr.textContent = e.message; capErr.hidden = false;
  }
  capBtn.disabled = false;
});

const impFile = $("#impFile"), impBtn = $("#impBtn"), impErr = $("#impErr"),
  impProg = $("#impProg"), impFill = $("#impFill"), impStat = $("#impStat");
impFile.addEventListener("change", () => {
  impErr.hidden = true;
  $("#impName").textContent = impFile.files[0]?.name || "";
  impBtn.disabled = !impFile.files.length;
});
impBtn.addEventListener("click", () => {
  const file = impFile.files[0];
  if (!file || !S.me) return;
  impErr.hidden = true;
  impBtn.disabled = true;
  impProg.hidden = false;
  const fd = new FormData();
  fd.append("client_request_id", uuid());
  fd.append("collection_id", capCol.value || S.col);
  fd.append("instance_id", S.me.instance_id);
  fd.append("user_id", S.me.user.id);
  fd.append("file", file, file.name);
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/import");
  xhr.setRequestHeader("X-CSRFToken", csrf());
  xhr.setRequestHeader("Accept", "application/json");
  xhr.upload.addEventListener("progress", ev => {
    if (!ev.lengthComputable) return;
    const pct = Math.round(100 * ev.loaded / ev.total);
    impFill.style.width = pct + "%";
    impProg.setAttribute("aria-valuenow", pct);
    impStat.textContent = `uploading ${pct}% (${(ev.loaded / 1048576).toFixed(1)} of ${(ev.total / 1048576).toFixed(1)} MiB)`;
  });
  xhr.addEventListener("load", () => {
    impBtn.disabled = false;
    let data = null; try { data = JSON.parse(xhr.responseText); } catch {}
    if (xhr.status === 200 || xhr.status === 201) {
      impProg.hidden = true; impFill.style.width = "0";
      impStat.textContent = "";
      impFile.value = ""; $("#impName").textContent = ""; impBtn.disabled = true;
      addDlg.close();
      banner("ok", "Import accepted — entries from the TikTok export or library.json appear in the library as the worker processes them.", {
        actions: [{ label: "Open library", onclick: () => setView("library") }], ms: 8000 });
      clearTimeout(S.libTimer);
      S.libTimer = setTimeout(() => { if (S.view === "library") loadEntries(true); }, 4000);
      return;
    }
    impProg.hidden = true;
    const msg = data && (typeof data === "object" ? (data.detail || data.error) : data) ||
      FALLBACK[xhr.status] || `Import failed (${xhr.status}).`;
    impErr.textContent = xhr.status === 413
      ? "This export is larger than the server's import ceiling. Split it or ask your administrator to raise the limit."
      : String(msg).slice(0, 300);
    impErr.hidden = false;
  });
  xhr.addEventListener("error", () => {
    impBtn.disabled = false; impProg.hidden = true;
    impErr.textContent = FALLBACK[0]; impErr.hidden = false;
  });
  xhr.send(fd);
});

/* ---------- collections ---------- */
async function renderCollections() {
  const me = S.me; if (!me) return;
  const frag = document.createDocumentFragment();
  frag.append(el("h1", { text: "Collections" }));
  const form = el("form", { class: "panel" }, el("h3", { text: "Create a shared collection" }),
    el("div", { class: "row2" },
      el("input", { type: "text", placeholder: "Name", "aria-label": "New collection name",
        maxlength: 120, required: true, style: "flex:1;min-width:180px" }),
      el("button", { class: "btn primary", type: "submit", text: "Create" })),
    el("p", { class: "hint", text: "You become the owner and can manage members. Members can add and remove their own contributions." }));
  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    const name = form.querySelector("input").value.trim();
    if (!name) return;
    try {
      await api("/api/collections", { method: "POST", body: { name } });
      await refreshMe(); setView("collections"); toast(`Created “${name}”.`);
    } catch (e) { if (e.status !== 401) banner("err", e.message, { sticky: true }); }
  });
  frag.append(form);
  for (const c of me.collections || []) {
    const card = el("div", { class: "panel" },
      el("div", { class: "row2" },
        el("h3", { text: c.name, style: "margin:0" }),
        el("span", { class: "badge" + (c.kind === "personal" ? "" : " run"),
          text: c.kind === "personal" ? "Personal — private to you" : "Shared" }),
        el("span", { style: "flex:1" }),
        el("button", { class: "btn small", type: "button", onclick: () => {
          S.col = c.id; catsFresh(); resetHeaderFilters(); setView("library");
        } }, "Open")));
    if (c.kind === "shared" && c.can_manage) card.append(membersPanel(c));
    frag.append(card);
  }
  out.replaceChildren(frag);
  renderToc();
}
function resetHeaderFilters() { S.q = ""; S.cat = ""; S.tag = ""; S.kind = "";
  qInput.value = ""; qClear.hidden = true; kindSel.value = ""; rebuildTagSel(); }
function membersPanel(c) {
  const p = el("details", { class: "panel" }, el("summary", { text: "Members" }));
  const body = el("div", {});
  p.append(body);
  const load = async () => {
    body.replaceChildren(el("p", { class: "hint", text: "Loading members…" }));
    let j;
    try { j = await api("/api/collections/" + c.id); }
    catch (e) { if (e.status !== 401) body.replaceChildren(el("p", { class: "err", text: e.message })); return; }
    const rows = (j.members || []).map(m => {
      const rm = el("td", {});
      if (!m.is_owner)
        rm.append(el("button", { class: "btn small", type: "button", text: "Remove",
          onclick: async () => {
            const vals = await confirmDialog({ head: "Remove member?",
              text: `${m.email} loses access to “${c.name}”. Their contributions remain.`,
              ok: "Remove" });
            if (!vals) return;
            try { await api(`/api/collections/${c.id}/members/${m.id}`, { method: "DELETE" }); load(); }
            catch (e) { if (e.status !== 401 && !e.cancelled) banner("err", e.message, { sticky: true }); }
          } }));
      return el("tr", {},
        el("td", { text: m.email }),
        el("td", {}, m.is_owner
          ? el("span", { class: "badge run", text: "owner" })
          : el("span", { class: "badge", text: "member" })),
        rm);
    });
    const addForm = el("form", { class: "row2" },
      el("input", { type: "email", placeholder: "member@example.com", required: true,
        "aria-label": "Email of an existing user to add", style: "flex:1;min-width:200px" }),
      el("button", { class: "btn tonal", type: "submit", text: "Add member" }));
    addForm.addEventListener("submit", async ev => {
      ev.preventDefault();
      const email = addForm.querySelector("input").value.trim();
      try { await api(`/api/collections/${c.id}/members`, { method: "POST", body: { email } }); load(); }
      catch (e) { if (e.status !== 401 && !e.cancelled) banner("err", e.message, { sticky: true }); }
    });
    body.replaceChildren(
      el("table", {},
        el("thead", {}, el("tr", {},
          el("th", { text: "Email" }), el("th", { text: "Role" }), el("th", { text: "" }))),
        el("tbody", {}, ...rows)),
      addForm,
      el("p", { class: "hint", text: "Only existing active accounts can be added. Remove is not offered for the owner." }));
  };
  p.addEventListener("toggle", () => { if (p.open && !body.childNodes.length) load(); });
  return p;
}

function renderSettings() {
  const me = S.me; if (!me) return;
  const sel = el("select", { "aria-label": "Default capture destination" },
    ...(me.collections || []).map(c => el("option", { value: c.id,
      text: c.name + (c.kind === "personal" ? " (personal)" : "") })));
  sel.value = me.default_collection_id || S.col || "";
  const form = el("form", { class: "panel" }, el("h3", { text: "Default capture destination" }),
    el("div", { class: "field" }, sel,
      el("p", { class: "help", text: "Used for new captures from the web and the Android app. Each queued share keeps the destination it was captured with." })),
    el("button", { class: "btn primary", type: "submit", text: "Save" }));
  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    try {
      S.me = await api("/api/settings", { method: "POST",
        body: { default_collection_id: sel.value } });
      toast("Default destination saved.");
    } catch (e) { if (e.status !== 401) banner("err", e.message, { sticky: true }); }
  });
  const logoutForm = el("form", { method: "post", action: "/accounts/logout/" },
    el("input", { type: "hidden", name: "csrfmiddlewaretoken", value: csrf() }),
    el("button", { class: "btn danger", type: "submit", text: "Sign out" }));
  out.replaceChildren(
    el("h1", { text: "Settings" }),
    el("div", { class: "panel" }, el("h3", { text: "Account" }),
      el("p", {}, "Signed in as ", el("b", { text: me.user.email }),
        me.user.is_app_admin ? el("span", { class: "badge run", text: "app admin", style: "margin-left:8px" }) : null),
      el("div", { class: "acts" },
        el("a", { class: "btn small", href: "/accounts/email/" }, "Manage email addresses"),
        el("a", { class: "btn small", href: "/accounts/password/change/" }, "Change password"),
        logoutForm)),
    form);
  renderToc();
}


/* ---------- header bindings ---------- */
let qTimer = 0;
qInput.addEventListener("input", () => {
  qClear.hidden = !qInput.value;
  clearTimeout(qTimer);
  qTimer = setTimeout(() => { S.q = qInput.value.trim(); loadEntries(true); }, 350);
});
qClear.addEventListener("click", () => {
  qInput.value = ""; qClear.hidden = true; S.q = ""; qInput.focus(); loadEntries(true);
});
addEventListener("keydown", e => {
  if (e.key === "/" && !/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) && !e.target.isContentEditable &&
      !document.querySelector("dialog[open]")) {
    e.preventDefault(); qInput.focus();
  }
  if (e.key === "Escape" && document.activeElement === qInput && qInput.value) qClear.click();
});
tagSel.addEventListener("change", () => { S.tag = tagSel.value; loadEntries(true); });
kindSel.addEventListener("change", () => { S.kind = kindSel.value; loadEntries(true); });
const vtBtns = [...document.querySelectorAll(".vtb")];
function setVT(v) {
  S.vt = v; localStorage.setItem("clipshelf.view", v);
  for (const b of vtBtns) { const on = b.dataset.vt === v;
    b.classList.toggle("on", on); b.setAttribute("aria-pressed", String(on)); }
  if (S.view === "library" && S.entries.length) renderLibrary();
}
for (const b of vtBtns) b.addEventListener("click", () => setVT(b.dataset.vt));
const srtBtns = [...document.querySelectorAll(".stb[data-srt]")];
function setSrt(s) {
  S.srt = s; localStorage.setItem("clipshelf.sort", s);
  for (const b of srtBtns) { const on = b.dataset.srt === s;
    b.classList.toggle("on", on); b.setAttribute("aria-pressed", String(on)); }
  loadEntries(true);
}
for (const b of srtBtns) b.addEventListener("click", () => setSrt(b.dataset.srt));


/* ---------- view registrations + layout buttons ---------- */
onEnter("library", () => loadEntries(true));
onEnter("inbox", () => { S.capsLoaded = false; loadCaptures(); pollCaptures(); });
onEnter("collections", renderCollections);
onEnter("settings", renderSettings);
function initLayoutButtons() {
  setVT(S.vt);
  for (const b of srtBtns) { const on = b.dataset.srt === S.srt;
    b.classList.toggle("on", on); b.setAttribute("aria-pressed", String(on)); }
}
export { initLayoutButtons, refreshInboxCount, renderToc };
