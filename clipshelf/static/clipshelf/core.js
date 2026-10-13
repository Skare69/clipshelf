/* clipshelf web app — shared core: DOM helpers, transport, state, chrome, navigation.
   Vanilla JS ES module, no framework, no CDN, no third-party requests. All model/source
   text enters the DOM via textContent; URLs only http(s); private state lives in memory only.
   localStorage holds the two layout prefs, nothing else. */
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
    else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
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
  eye: "M12 4.5C7 4.5 2.7 7.6 1 12c1.7 4.4 6 7.5 11 7.5s9.3-3.1 11-7.5c-1.7-4.4-6-7.5-11-7.5zM12 17a5 5 0 1 1 0-10 5 5 0 0 1 0 10zm0-8a3 3 0 1 0 0 6 3 3 0 0 0 0-6z",
  close: "M19 6.41 17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z",
  link: "M3.9 12c0-1.71 1.39-3.1 3.1-3.1h4V7H7c-2.76 0-5 2.24-5 5s2.24 5 5 5h4v-1.9H7c-1.71 0-3.1-1.39-3.1-3.1zM8 13h8v-2H8v2zm9-6h-4v1.9h4c1.71 0 3.1 1.39 3.1 3.1s-1.39 3.1-3.1 3.1h-4V17h4c2.76 0 5-2.24 5-5s-2.24-5-5-5z",
  prompt: "M3 18h12v-2H3v2zM3 6v2h18V6H3zm0 7h18v-2H3v2z"
};
const icon = (d, size) => {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  s.setAttribute("width", size || 18); s.setAttribute("height", size || 18);
  s.setAttribute("fill", "currentColor"); s.setAttribute("aria-hidden", "true");
  const p = document.createElementNS(s.namespaceURI, "path");
  p.setAttribute("d", d); s.append(p); return s;
};
// The year only shows when it differs from this year: labels stay short.
const fmtDate = s => { if (!s) return ""; const d = new Date(s);
  if (isNaN(d)) return String(s);
  const year = d.getFullYear() === new Date().getFullYear() ? undefined : "numeric";
  return d.toLocaleDateString(undefined, { year, month: "short", day: "numeric" }); };
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
const copyBtn = (getText, label, cls = "btn small") => {
  const b = el("button", { class: cls, type: "button" }, icon(ICONS.copy), label);
  b.addEventListener("click", async () => {
    if (await copyText(String(getText()))) {
      b.classList.add("done");
      b.replaceChildren(icon(ICONS.check), "Copied");
      setTimeout(() => { b.classList.remove("done"); b.replaceChildren(icon(ICONS.copy), label); }, 1400);
    } else banner("err", "The browser did not allow the copy. Select the text and copy it by hand.", { sticky: true });
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
const out = $("#out"), vhead = $("#vhead"), bannerEl = $("#banner");
let bannerTimer = 0;
function banner(kind, text, opts = {}) {
  clearTimeout(bannerTimer);
  bannerEl.className = `banner ${kind}`;
  bannerEl.replaceChildren(el("span", { class: "grow-text", text }),
    ...(opts.actions || []).map(a => el("button", { class: "btn small", type: "button",
      onclick: () => { a.onclick?.(); if (!a.keep) hideBanner(); } }, a.label)),
    el("button", { class: "iconbtn", type: "button", "aria-label": "Dismiss",
      onclick: hideBanner }, icon(ICONS.close, 20)));
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
      placeholder: f.placeholder || "", autocomplete: "off", required: f.required || null });
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

// Sheets close from their close button, Escape (native), or a click on the backdrop.
for (const b of document.querySelectorAll("dialog [data-close]")) {
  const d = b.closest("dialog");
  b.addEventListener("click", () => d.close());
  d.addEventListener("click", e => {
    if (e.target !== d) return;
    const r = d.getBoundingClientRect();
    if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) d.close();
  });
}
const rail = $(".rail");

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
  for (const a of rail.querySelectorAll("a.vw")) {
    const on = a.dataset.nav === v;
    a.classList.toggle("on", on);
    if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  vhead.replaceChildren();
  out.replaceChildren();
  $("#main").focus({ preventScroll: true });
  scrollTo({ top: 0 });
  const enter = ENTER[v];
  if (enter) enter();
}

const ENTER = {};
const onEnter = (view, fn) => { ENTER[view] = fn; };
rail.addEventListener("click", e => {
  const a = e.target.closest("a.vw");
  if (a) { e.preventDefault(); setView(a.dataset.nav); }
});


async function refreshMe() {
  S.me = await api("/api/me");
  $(".rail .adm").hidden = !S.me.user.is_app_admin;
}


export {
  $,
  FALLBACK,
  ICONS,
  S,
  api,
  banner,
  captureActive,
  captureBad,
  catsFresh,
  confirmDialog,
  copyBtn,
  csrf,
  el,
  fmtDate,
  fmtWhen,
  hostOf,
  icon,
  note,
  onAuthLoss,
  onEnter,
  out,
  refreshMe,
  safeUrl,
  sensitive,
  setView,
  shortHost,
  toast,
  uuid,
  vhead,
};
