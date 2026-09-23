/* clipshelf web app — vanilla JS, no framework, no CDN, no third-party requests.
   All model/source text enters the DOM via textContent; URLs only http(s); private
   state lives in memory only. localStorage holds the two layout prefs, nothing else. */
(() => {
"use strict";

/* ---------- tiny DOM helpers ---------- */
const $ = (s, r) => (r || document).querySelector(s);
const el = (tag, attrs, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k === "value") n.value = v;
    else if (v === true) n.setAttribute(k, "");
    else n.setAttribute(k, v);
  }
  for (const kid of kids.flat(9)) if (kid != null && kid !== false) n.append(kid);
  return n;
};
const ICONS = {
  copy: "M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z",
  check: "M9 16.17 4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z",
  retry: "M17.65 6.35C16.2 4.9 14.21 4 12 4c-4.42 0-7.99 3.58-8 8s3.57 8 8 8c3.73 0 6.84-2.55 7.73-6h-2.08c-.82 2.33-3.04 4-5.65 4-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z",
  ext: "M19 19H5V5h7V3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2v-7h-2v7zM14 3v2h3.59l-9.83 9.83 1.41 1.41L19 6.41V10h2V3h-7z",
  eye: "M12 4.5C7 4.5 2.7 7.6 1 12c1.7 4.4 6 7.5 11 7.5s9.3-3.1 11-7.5c-1.7-4.4-6-7.5-11-7.5zM12 17a5 5 0 1 1 0-10 5 5 0 0 1 0 10zm0-8a3 3 0 1 0 0 6 3 3 0 0 0 0-6z"
};
const icon = (d, size) => {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  s.setAttribute("width", size || 18); s.setAttribute("height", size || 18);
  s.setAttribute("fill", "currentColor"); s.setAttribute("aria-hidden", "true");
  const p = document.createElementNS(s.namespaceURI, "path");
  p.setAttribute("d", d); s.append(p); return s;
};
const HUE = s => { let h = 0; for (const c of String(s)) h = (h * 31 + c.codePointAt(0)) % 360; return h; };
const avatar = seed => el("span", { class: "avatar", "aria-hidden": "true",
  style: `--av:hsl(${HUE(seed)} 42% 52%)` }, (String(seed).trim()[0] || "?").toUpperCase());
const fmtDate = s => { if (!s) return ""; const d = new Date(s);
  return isNaN(d) ? String(s) : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }); };
const fmtWhen = s => { if (!s) return ""; const d = new Date(s);
  return isNaN(d) ? String(s) : d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }); };
const safeUrl = u => { try { const p = new URL(u); return p.protocol === "http:" || p.protocol === "https:" ? p.href : null; } catch { return null; } };
const hostOf = u => { try { const p = new URL(u); return p.host + (p.pathname !== "/" ? p.pathname : ""); } catch { return String(u || ""); } };
const shortHost = u => { try { return new URL(u).host.replace(/^www\./, ""); } catch { return String(u || ""); } };
const uuid = () => {
  if (crypto.randomUUID) return crypto.randomUUID();   // secure contexts only
  // Plain HTTP on the LAN: randomUUID is missing, getRandomValues is not.
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40;                         // version 4
  b[8] = (b[8] & 0x3f) | 0x80;                         // variant 10xx
  const h = [...b].map(x => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
};
async function copyText(t) {
  try { await navigator.clipboard.writeText(t); return true; }
  catch {
    const ta = el("textarea", { class: "vh", "aria-hidden": "true" }); ta.value = t;
    document.body.append(ta); ta.select();
    let ok = false; try { ok = document.execCommand("copy"); } catch {}
    ta.remove(); return ok;
  }
}
const copyBtn = (getText, label) => {
  const b = el("button", { class: "btn small", type: "button", title: `Copy ${label}` }, icon(ICONS.copy), label);
  b.addEventListener("click", async () => {
    if (await copyText(String(getText()))) {
      b.replaceChildren(icon(ICONS.check), "copied");
      setTimeout(() => b.replaceChildren(icon(ICONS.copy), label), 1200);
    }
  });
  return b;
};

/* ---------- API ---------- */
class ApiError extends Error {
  constructor(status, detail) {
    const d = detail && typeof detail === "object" ? detail : {};
    const msg = (typeof detail === "string" && detail) || d.detail || d.error ||
      Object.values(d).flat?.().join(" ") || "";
    super(String(msg).slice(0, 300) || FALLBACK[status] || `Request failed (${status}).`);
    this.status = status; this.detail = detail;
  }
}
const FALLBACK = {
  0: "Server unreachable. Check the connection and try again.",
  400: "The server rejected this input.",
  403: "You don't have access to do that.",
  404: "Not found — or you don't have access to it.",
  409: "Conflict: this item changed or the id was reused.",
  413: "Too large for the server's configured limit."
};
const csrf = () => (document.cookie.match(/(?:^|;\s*)csrftoken=([^;]*)/) || [])[1] ||
  ($('input[name="csrfmiddlewaretoken"]') || {}).value || "";
let redirecting = false;
async function api(path, { method = "GET", body } = {}) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-CSRFToken"] = csrf();
  let r;
  try {
    r = await fetch(path, { method, headers, credentials: "same-origin",
      body: body === undefined ? undefined : JSON.stringify(body) });
  } catch { throw new ApiError(0); }
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (r.status === 401) { onAuthLoss(); throw new ApiError(401, data); }
  if (!r.ok) throw new ApiError(r.status, data);
  return data;
}
/* sensitive admin mutation: server asks for reauth only when the session is stale */
async function sensitive(path, body) {
  try { return await api(path, { method: "POST", body }); }
  catch (e) {
    if (e.status === 403 && e.detail && typeof e.detail === "object" &&
        e.detail.detail === "reauthentication_required") {
      const vals = await confirmDialog({
        head: "Reauthenticate",
        text: "Enter your current password to confirm this sensitive action.",
        ok: "Continue",
        fields: [{ name: "reauth_password", label: "Current password", type: "password", required: true }]
      });
      if (!vals) { const err = new ApiError(403, "Cancelled."); err.cancelled = true; throw err; }
      return await api(path, { method: "POST", body: { ...body, ...vals } });
    }
    throw e;
  }
}

/* ---------- state ---------- */
const S = {
  me: null, view: "library", col: null,
  q: "", cat: "", tag: "", kind: "",
  srt: localStorage.getItem("clipshelf.sort") === "new" ? "new" : "az",
  vt: localStorage.getItem("clipshelf.view") === "list" ? "list" : "grid",
  entries: [], count: 0, total: 0, offset: 0, limit: 100, capsLoaded: false,
  cats: new Map(), tags: new Set(),
  captures: [], capFilter: "all", capTimer: null, libTimer: null,
  inboxActive: 0, seq: 0
};
const catsFresh = () => { S.cats = new Map(); S.tags = new Set(); };
const note = e => Array.isArray(e.cat) ? (e.cat[0] || "") : (e.cat || "");
/* Activity/error/retry decisions are server-projected booleans
   (Job.active/needs_attention/can_retry in every job payload); only the
   badge colors and labels below are presentation. */
const captureActive = c => (c.jobs || []).some(j => j.active);
const captureBad = c => (c.jobs || []).some(j => j.needs_attention);

/* ---------- chrome: banner, dialogs, nav ---------- */
const out = $("#out"), bannerEl = $("#banner");
let bannerTimer = 0;
function banner(kind, text, opts = {}) {
  clearTimeout(bannerTimer);
  bannerEl.className = `banner ${kind}`;
  bannerEl.replaceChildren(el("span", { class: "grow-text", text }),
    ...(opts.actions || []).map(a => el("button", { class: "btn small", type: "button",
      onclick: () => { a.onclick?.(); if (!a.keep) hideBanner(); } }, a.label)),
    el("button", { class: "iconbtn", type: "button", "aria-label": "Dismiss",
      onclick: hideBanner }, "✕"));
  bannerEl.hidden = false;
  if (!opts.sticky && !opts.actions?.length)
    bannerTimer = setTimeout(hideBanner, opts.ms || 4000);
}
const hideBanner = () => { bannerEl.hidden = true; bannerEl.replaceChildren(); };
const toast = (text, actions) => banner("ok", text, { actions, ms: 6000 });

const confirmDlg = $("#confirmDlg");
let cfResolve = null;
function confirmDialog({ head, text, ok = "Confirm", danger = true, fields = [] }) {
  $("#cfHead").textContent = head;
  $("#cfText").textContent = text || "";
  const extra = $("#cfExtra"); extra.replaceChildren();
  for (const f of fields) {
    const id = "cf_" + f.name;
    const input = el("input", { id, type: f.type || "text", name: f.name,
      placeholder: f.placeholder || "", autocomplete: "off" });
    if (f.value != null) input.value = f.value;
    extra.append(el("div", { class: "field" },
      el("label", { for: id, text: f.label + (f.required ? " *" : "") }), input));
  }
  $("#cfErr").hidden = true;
  const okBtn = $("#cfOk");
  okBtn.textContent = ok;
  okBtn.className = "btn " + (danger ? "danger" : "primary");
  confirmDlg.returnValue = "";
  cfResolve = null;
  confirmDlg.showModal();
  (fields[0] ? $("#cf_" + fields[0].name) : okBtn).focus();
  return new Promise(res => { cfResolve = res; });
}
confirmDlg.addEventListener("close", () => {
  const vals = {};
  if (confirmDlg.returnValue === "ok") {
    for (const f of confirmDlg.querySelectorAll("#cfExtra input")) vals[f.name] = f.value;
    cfResolve?.(vals);
  } else cfResolve?.(null);
  cfResolve = null;
});
$("#cfCancel").addEventListener("click", () => confirmDlg.close(""));
$("#cfOk").addEventListener("click", () => {
  let bad = false;
  for (const f of confirmDlg.querySelectorAll("#cfExtra input")) {
    if (f.required && !f.value.trim()) {
      $("#cfErr").textContent = "This field is required."; $("#cfErr").hidden = false; bad = true;
    }
  }
  if (!bad) confirmDlg.close("ok");
});

const toc = $("#toc"), scrim = $("#scrim"), navBtn = $("#navBtn");
const closeDrawer = () => { toc.classList.remove("open"); scrim.hidden = true; navBtn.setAttribute("aria-expanded", "false"); };
navBtn.addEventListener("click", () => {
  const open = !toc.classList.contains("open");
  toc.classList.toggle("open", open); scrim.hidden = !open;
  navBtn.setAttribute("aria-expanded", String(open));
});
scrim.addEventListener("click", closeDrawer);
for (const d of document.querySelectorAll("dialog [data-close]"))
  d.addEventListener("click", () => d.closest("dialog").close());
const topBtn = $("#topBtn");
addEventListener("scroll", () => { topBtn.hidden = scrollY < 600; }, { passive: true });
topBtn.addEventListener("click", () => scrollTo({ top: 0, behavior: "smooth" }));

/* ---------- auth loss: drop private state, let the server route to login ---------- */
function onAuthLoss() {
  if (redirecting) return;
  redirecting = true;
  clearInterval(S.capTimer); clearTimeout(S.libTimer); clearTimeout(bannerTimer);
  S.me = null; S.entries = []; S.captures = []; catsFresh();
  location.assign("/accounts/login/?next=/");
}

/* ---------- navigation ---------- */
function setView(v) {
  S.view = v;
  clearInterval(S.capTimer);
  for (const a of toc.querySelectorAll("a.vw")) a.classList.toggle("on", a.dataset.nav === v);
  closeDrawer();
  out.replaceChildren();
  $("#main").focus({ preventScroll: true });
  scrollTo({ top: 0 });
  if (v === "library") loadEntries(true);
  else if (v === "inbox") { S.capsLoaded = false; loadCaptures(); pollCaptures(); }
  else if (v === "collections") renderCollections();
  else if (v === "settings") renderSettings();
  else if (v === "admin") renderAdmin();
}
toc.addEventListener("click", e => {
  const a = e.target.closest("a.vw");
  if (a) { e.preventDefault(); setView(a.dataset.nav); }
});

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
async function refreshMe() {
  S.me = await api("/api/me");
  $("#adminNav").hidden = !S.me.user.is_app_admin;
}

/* ---------- settings ---------- */
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

/* ---------- admin ---------- */
async function renderAdmin() {
  if (!S.me?.user.is_app_admin) return;
  const frag = document.createDocumentFragment();
  frag.append(el("h1", { text: "Server administration" }));
  const wrap = el("div", {});
  frag.append(wrap);
  out.replaceChildren(frag);
  renderToc();
  const [users, invs, cols, llm, scr] = await Promise.allSettled([
    api("/api/admin/users"), api("/api/admin/invitations"),
    api("/api/admin/collections"), api("/api/admin/llm"), api("/api/admin/screening")
  ]);
  const errNode = e => el("p", { class: "err", text: e.message });
  wrap.replaceChildren();
  wrap.append(adminUsers(users.status === "fulfilled" ? users.value.users : null,
    users.status === "rejected" ? users.reason : null));
  wrap.append(adminInvitations(invs.status === "fulfilled" ? invs.value.invitations : null,
    invs.status === "rejected" ? invs.reason : null));
  wrap.append(adminCollections(cols.status === "fulfilled" ? cols.value.collections : null,
    cols.status === "rejected" ? cols.reason : null));
  wrap.append(adminLlm(llm.status === "fulfilled" ? llm.value.llm : null,
    llm.status === "rejected" ? llm.reason : null,
    scr.status === "fulfilled" ? scr.value.screening : null,
    scr.status === "rejected" ? scr.reason : null));
}
function adminUsers(users, err) {
  const p = el("section", { class: "panel" }, el("h3", { text: "Accounts" }));
  if (err) return p.append(errNode(err)), p;
  const rows = (users || []).map(u => {
    const acts = el("div", { class: "acts" });
    const toggle = el("button", { class: "btn small", type: "button",
      text: u.is_active ? "Disable" : "Re-enable" });
    toggle.addEventListener("click", async () => {
      const vals = await confirmDialog({
        head: (u.is_active ? "Disable" : "Re-enable") + " " + u.email + "?",
        text: u.is_active
          ? "Access and sessions are revoked; identity, content, memberships and ownership are preserved. Re-enabling allows fresh login."
          : "The account can sign in again under current permissions. Previously revoked sessions stay revoked.",
        ok: u.is_active ? "Disable" : "Re-enable", danger: u.is_active });
      if (!vals) return;
      try { await sensitive(`/api/admin/users/${u.id}/status`, { active: !u.is_active }); renderAdmin(); }
      catch (e) { if (e.status !== 401 && !e.cancelled) banner("err", e.message, { sticky: true }); }
    });
    const rec = el("button", { class: "btn small", type: "button", text: "Recover" });
    rec.addEventListener("click", async () => {
      const vals = await confirmDialog({
        head: "Exceptional recovery for " + u.email + "?",
        text: "Use only when password AND mailbox access are both lost. This creates a reset link that grants full control of the account — handle it like a key. Sessions are invalidated; MFA is never removed.",
        ok: "Create reset link",
        fields: [{ name: "reauth_password", label: "Your current password", type: "password", required: true }] });
      if (!vals) return;
      try {
        const j = await sensitive(`/api/admin/users/${u.id}/recover`, vals);
        await confirmDialog({ head: "Reset link created",
          text: "Copy it now and deliver it through a trusted channel. It is not emailed.",
          ok: "Done", danger: false,
          fields: [{ name: "url", label: "Reset link (copy it now)", value: j.reset_url }] });
      } catch (e) { if (e.status !== 401 && !e.cancelled) banner("err", e.message, { sticky: true }); }
    });
    acts.append(toggle, rec);
    return el("tr", {},
      el("td", {}, el("b", { text: u.email }),
        u.is_app_admin ? el("span", { class: "badge run", text: "admin", style: "margin-left:6px" }) : null),
      el("td", { text: u.is_verified ? "yes" : "no" }),
      el("td", { text: fmtDate(u.date_joined) }),
      el("td", {}, el("span", { class: "badge " + (u.is_active ? "ok" : "err"),
        text: u.is_active ? "active" : "disabled" })),
      el("td", {}, acts));
  });
  p.append(el("table", {},
    el("thead", {}, el("tr", {},
      ...["Email", "Verified", "Joined", "Status", "Actions"].map(h => el("th", { text: h })))),
    el("tbody", {}, ...rows)));
  p.append(el("p", { class: "hint", text: "Recovery uses the framework's reset tokens under the same account id and never removes MFA. Disabling never deletes content." }));
  return p;
}
function adminInvitations(invs, err) {
  const p = el("section", { class: "panel" }, el("h3", { text: "Invitations" }));
  if (err) return p.append(errNode(err)), p;
  const form = el("form", { class: "row2" },
    el("input", { type: "email", required: true, placeholder: "invitee@example.com",
      "aria-label": "Email to invite", style: "flex:1;min-width:200px" }),
    el("button", { class: "btn primary", type: "submit", text: "Create invitation" }));
  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    const email = form.querySelector("input").value.trim();
    try {
      const j = await sensitive("/api/admin/invitations", { email });
      const url = j.invitation?.url;
      await confirmDialog({ head: "Invitation created for " + email,
        text: "Copy the link now and deliver it yourself — this is the only time it is shown (tokens are stored hashed). If it is lost, create a new invitation; used and expired ones never block a fresh link.",
        ok: "Done", danger: false,
        fields: url ? [{ name: "url", label: "Invitation link (copy it now)", value: url }] : [] });
      renderAdmin();
    } catch (e) { if (e.status !== 401 && !e.cancelled) banner("err", e.message, { sticky: true }); }
  });
  const rows = (invs || []).map(v => {
    const status = v.used_at ? "used" : (new Date(v.expires_at) < new Date() ? "expired" : "pending");
    return el("tr", {},
      el("td", { text: v.email }),
      el("td", { text: fmtDate(v.created_at) }),
      el("td", { text: fmtDate(v.expires_at) }),
      el("td", {}, el("span", { class: "badge " + ({ used: "", expired: "err", pending: "ok" }[status]), text: status })));
  });
  p.append(form,
    el("table", {},
      el("thead", {}, el("tr", {}, ...["Email", "Created", "Expires", "Status"].map(h => el("th", { text: h })))),
      el("tbody", {}, ...rows)),
    el("p", { class: "hint", text: "Invitations are single-use, expiring links; the recipient picks their own password and verifies the email. SMTP is not involved. The full link is shown only once, at creation." }));
  return p;
}
function adminCollections(cols, err) {
  const p = el("section", { class: "panel" }, el("h3", { text: "Shared collections" }));
  if (err) return p.append(errNode(err)), p;
  if (!(cols || []).length) return p.append(el("p", { class: "hint", text: "No shared collections exist. Personal collections are never listed here." })), p;
  p.append(el("table", {},
    el("thead", {}, el("tr", {}, ...["Collection", "Owner", "Members", "Succession"].map(h => el("th", { text: h })))),
    el("tbody", {}, ...(cols || []).map(c => {
      const activeMembers = (c.members || []).filter(m => m.is_active && m.id !== c.owner.id);
      const sel = el("select", { "aria-label": "New owner for " + c.name },
        ...activeMembers.map(m => el("option", { value: m.id, text: m.email })));
      const btn = el("button", { class: "btn small", type: "button", text: "Transfer" });
      if (c.owner.is_active || !activeMembers.length)
        btn.disabled = true,
        btn.title = c.owner.is_active ? "Disable the owner account first." : "No active member to transfer to.";
      btn.addEventListener("click", async () => {
        const vals = await confirmDialog({
          head: `Transfer “${c.name}”?`,
          text: `Ownership moves to the selected active member. The previous owner stays a member. Personal collections are never transferable.`,
          ok: "Transfer ownership",
          fields: [{ name: "reauth_password", label: "Your current password", type: "password", required: true }] });
        if (!vals) return;
        try {
          await sensitive(`/api/admin/collections/${c.id}/transfer`, { owner_id: sel.value, ...vals });
          toast("Ownership transferred."); renderAdmin();
        } catch (e) { if (e.status !== 401 && !e.cancelled) banner("err", e.message, { sticky: true }); }
      });
      return el("tr", {},
        el("td", { text: c.name }),
        el("td", {}, c.owner.email, " ",
          el("span", { class: "badge " + (c.owner.is_active ? "ok" : "err"),
            text: c.owner.is_active ? "active" : "disabled" })),
        el("td", { text: String((c.members || []).length + 1) }),
        el("td", {}, el("div", { class: "acts" }, sel, btn)));
    }))),
    el("p", { class: "hint", text: "Succession requires a disabled owner and an existing active member. The admin never gains content access through a transfer." }));
  return p;
}
/* Base URLs are stable; model names are not, so those are fetched, never hardcoded.
   Vision is required — the capability check sends an image. */
const LLM_PROVIDERS = [
  { id: "", label: "Custom / OpenAI-compatible", url: "" },
  { id: "openai", label: "OpenAI", url: "https://api.openai.com/v1" },
  { id: "anthropic", label: "Anthropic", url: "https://api.anthropic.com/v1" },
  { id: "gemini", label: "Google Gemini", url: "https://generativelanguage.googleapis.com/v1beta/openai" },
  { id: "openrouter", label: "OpenRouter", url: "https://openrouter.ai/api/v1" },
  { id: "groq", label: "Groq", url: "https://api.groq.com/openai/v1" },
  { id: "mistral", label: "Mistral", url: "https://api.mistral.ai/v1" },
  { id: "xai", label: "xAI", url: "https://api.x.ai/v1" },
  { id: "zai", label: "Z.AI (GLM)", url: "https://api.z.ai/api/paas/v4" },
  { id: "zai-coding", label: "Z.AI Coding Plan (GLM)", url: "https://api.z.ai/api/coding/paas/v4" },
  { id: "ollama", label: "Ollama (local)", url: "http://localhost:11434/v1", local: true },
  { id: "llamacpp", label: "llama.cpp (local)", url: "http://localhost:8080/v1", local: true },
  { id: "lmstudio", label: "LM Studio (local)", url: "http://localhost:1234/v1", local: true },
  { id: "vllm", label: "vLLM (local)", url: "http://localhost:8000/v1", local: true },
];
function adminLlm(llm, err, scr, scrErr) {
  const p = el("section", { class: "panel" }, el("h3", { text: "Shared interpretation endpoint" }));
  if (err) return p.append(errNode(err)), p;
  const base = el("input", { type: "url", id: "llmBase", value: llm.base_url || "", placeholder: "https://llm.example.internal/v1" });
  let modelCtl = el("input", { type: "text", id: "llmModel", class: "grow",
    value: llm.model || "", placeholder: "model name" });
  const modelValue = () => modelCtl.value.trim();
  const textModel = () => {
    const input = el("input", { type: "text", id: "llmModel", class: "grow",
      value: modelValue(), placeholder: "model name" });
    modelCtl.replaceWith(input); modelCtl = input;
  };
  const showModels = list => {
    const current = modelValue();
    const custom = el("option", { text: "Type a name instead…" });
    const sel = el("select", { id: "llmModel", class: "grow" },
      el("option", { value: "", text: "Choose a model…", disabled: true }),
      ...[...new Set([current, ...list].filter(Boolean))].map(m => el("option", { value: m, text: m })),
      custom);
    sel.value = current;
    let picked = current;   // the restore point when the admin escapes to free text
    sel.addEventListener("change", () => {
      if (sel.selectedOptions[0] !== custom) { picked = sel.value; return; }
      sel.value = picked; textModel(); modelCtl.focus();
    });
    modelCtl.replaceWith(sel); modelCtl = sel;
  };
  const load = el("button", { class: "btn small", type: "button", text: "Load models" });
  const conc = el("input", { type: "number", id: "llmConc", min: 1, max: 8, value: llm.concurrency ?? 2 });
  const key = el("input", { type: "password", id: "llmKey", placeholder: llm.has_api_key ? "Saved — leave blank to keep" : "No key set (optional for local endpoints)", autocomplete: "new-password" });
  const clear = el("input", { type: "checkbox", id: "llmClearKey" });
  const provider = el("select", { id: "llmProvider" }, ...LLM_PROVIDERS.map(o => el("option", { value: o.id, text: o.label })));
  const known = LLM_PROVIDERS.find(o => o.url && (llm.base_url || "").replace(/\/+$/, "") === o.url);
  provider.value = known ? known.id : "";
  const localHint = el("p", { class: "help", hidden: !known?.local,
    text: "Clipshelf runs in a container, so localhost is the container itself — use the host's address." });
  provider.addEventListener("change", () => {
    const o = LLM_PROVIDERS.find(x => x.id === provider.value) || {};
    if (o.url) base.value = o.url;
    localHint.hidden = !o.local;
    key.value = "";
    if (modelCtl.tagName === "SELECT") textModel();
  });
  base.addEventListener("input", () => { if (modelCtl.tagName === "SELECT") textModel(); });
  const note = el("p", { class: "hint", hidden: true });
  const showNote = (ok, text) => {
    note.className = ok ? "hint" : "err";
    note.textContent = text;
    note.hidden = false;
  };
  const check = el("button", { class: "btn tonal", type: "button", text: "Check connection" });
  const setCheck = s => {
    check.disabled = s === "busy";
    check.className = "btn tonal" + (s === "ok" ? " ok" : "");
    if (s === "busy") check.replaceChildren(el("span", { class: "spin" }), " Checking…");
    else if (s === "ok") { check.replaceChildren("✓"); check.setAttribute("aria-label", "Connection verified"); }
    else { check.replaceChildren("Check connection"); check.removeAttribute("aria-label"); }
  };
  setCheck(llm.verified_at ? "ok" : "idle");
  const form = el("form", {},
    el("div", { class: "field" }, el("label", { for: "llmProvider", text: "Provider" }), provider,
      el("p", { class: "help", text: "Presets fill the base URL. Any OpenAI-compatible endpoint works; vision is required." })),
    el("div", { class: "field" }, el("label", { for: "llmBase", text: "Base URL" }), base, localHint),
    el("div", { class: "field" }, el("label", { for: "llmModel", text: "Model" }),
      el("div", { class: "row2" }, modelCtl, load),
      el("p", { class: "help", text: "Load models asks the endpoint which models it has and turns this into a list to pick from." })),
    el("div", { class: "field" }, el("label", { for: "llmConc", text: "Concurrent requests" }), conc),
    el("div", { class: "field" }, el("label", { for: "llmKey", text: "API key" }), key,
      el("p", { class: "help", text: "Write-only: never returned by the server. Leave blank to keep a key for this endpoint; changing the URL clears it." })),
    el("div", { class: "field" },
      el("label", { for: "llmClearKey" }, clear, " Clear the saved key")),
    el("div", { class: "acts" },
      el("button", { class: "btn primary", type: "submit", text: "Save settings" }),
      check),
    note);
  form.addEventListener("input", () => setCheck("idle"));
  form.addEventListener("change", () => setCheck("idle"));
  load.addEventListener("click", async () => {
    load.disabled = true; load.textContent = "Loading…";
    try {
      const b = { base_url: base.value.trim() };
      if (clear.checked) b.api_key = "";
      else if (key.value) b.api_key = key.value;
      const list = (await sensitive("/api/admin/llm/models", b)).models || [];
      if (base.value.trim() !== b.base_url) return;
      if (list.length) showModels(list);
      else if (modelCtl.tagName === "SELECT") textModel();
      showNote(list.length, list.length
        ? `${list.length} models offered — pick one in the Model box.`
        : "The endpoint listed no models; type the name instead.");
    } catch (e) { if (e.status !== 401 && !e.cancelled) showNote(false, e.message); }
    finally { load.disabled = false; load.textContent = "Load models"; }
  });
  const body = () => ({ base_url: base.value.trim(), model: modelValue(),
    concurrency: Math.min(8, Math.max(1, parseInt(conc.value, 10) || 1)) });
  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    const b = body();
    if (clear.checked) b.api_key = "";
    else if (key.value) b.api_key = key.value;
    try { await sensitive("/api/admin/llm", b); key.value = ""; clear.checked = false;
      toast("Interpretation settings saved."); renderAdmin(); }
    catch (e) { if (e.status !== 401 && !e.cancelled) banner("err", e.message, { sticky: true }); }
  });
  form.querySelector(".tonal").addEventListener("click", async () => {
    const b = { base_url: base.value.trim(), model: modelValue() };
    if (clear.checked) b.api_key = "";
    else if (key.value) b.api_key = key.value;
    const before = [base.value.trim(), modelValue(), key.value, conc.value, clear.checked].join("\n");
    note.hidden = true;
    setCheck("busy");
    try {
      const j = await sensitive("/api/admin/llm/check", b);
      if ([base.value.trim(), modelValue(), key.value, conc.value, clear.checked].join("\n") !== before)
        setCheck("idle");
      else if (j.check?.ok) setCheck("ok");
      else { setCheck("idle"); showNote(false, j.check?.message || "check failed"); }
    } catch (e) {
      setCheck("idle");
      if (e.status !== 401 && !e.cancelled) showNote(false, e.message);
    }
  });
  p.append(form,
    el("p", { class: "hint", text: "One admin-managed endpoint serves every capture, including Personal collections. The key stays server-side." }));
  if (scrErr) return p.append(errNode(scrErr)), p;
  const sKey = el("input", { type: "password", id: "scrKey", autocomplete: "new-password",
    placeholder: scr?.has_api_key ? "saved key present"
      : scr?.env_fallback ? "using TYPESAFE_API_KEY from the environment" : "API key" });
  const sClear = el("input", { type: "checkbox", id: "scrClearKey" });
  const sBody = () => sClear.checked ? { api_key: "" } : sKey.value ? { api_key: sKey.value } : {};
  const sNote = el("p", { class: "hint", hidden: true });
  const sShowNote = (ok, text) => { sNote.className = ok ? "hint" : "err"; sNote.textContent = text; sNote.hidden = false; };
  const sForm = el("form", {},
    el("div", { class: "field" }, el("label", { for: "scrKey", text: "API key" }), sKey,
      el("p", { class: "help", text: "Optional guard rail against prompt injection in captured pages. Without a key, interpretation runs unguarded." })),
    el("div", { class: "field" },
      el("label", { for: "scrClearKey" }, sClear, " Clear the saved key")),
    el("div", { class: "acts" },
      el("button", { class: "btn primary", type: "submit", text: "Save" }),
      el("button", { class: "btn tonal", type: "button", text: "Test" })),
    sNote);
  sForm.addEventListener("submit", async ev => {
    ev.preventDefault();
    try { await sensitive("/api/admin/screening", sBody()); sKey.value = ""; sClear.checked = false;
      toast("Screening settings saved."); renderAdmin(); }
    catch (e) { if (e.status !== 401 && !e.cancelled) sShowNote(false, e.message); }
  });
  sForm.querySelector(".tonal").addEventListener("click", async () => {
    sNote.hidden = true;
    try {
      const j = await sensitive("/api/admin/screening/check", sBody());
      const ok = j.check?.ok ?? j.ok;
      sShowNote(ok, (ok ? "Screening check passed: " : "Screening check failed: ") + (j.check?.message || j.message || ""));
    } catch (e) { if (e.status !== 401 && !e.cancelled) sShowNote(false, e.message); }
  });
  p.append(el("fieldset", {}, el("legend", { text: "Screening (TypeSafe)" }), sForm));
  return p;
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

/* ---------- boot ---------- */
(async () => {
  setVT(S.vt);
  for (const b of srtBtns) { const on = b.dataset.srt === S.srt;
    b.classList.toggle("on", on); b.setAttribute("aria-pressed", String(on)); }
  try {
    await refreshMe();
    const me = S.me;
    S.col = me.default_collection_id || (me.collections.find(c => c.kind === "personal") || me.collections[0] || {}).id;
    if (!S.col) {
      out.replaceChildren(el("div", { class: "empty" },
        el("b", { text: "No collection available." }), " Contact your administrator."));
      return;
    }
    setView("library");
    refreshInboxCount();
  } catch (e) {
    if (e.status === 401) return; // onAuthLoss is redirecting
    out.replaceChildren(el("div", { class: "empty", role: "alert" },
      el("b", { text: "The server could not load your account." }), " ",
      el("span", { text: e.message }), " ",
      el("button", { class: "btn small", type: "button",
        onclick: () => location.reload() }, "Retry")));
  }
})();
})();
