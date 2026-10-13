/* clipshelf web app — library, inbox, collections, settings panels. */
"use strict";

import { $, FALLBACK, ICONS, S, api, banner, captureActive, captureBad, catsFresh, confirmDialog, copyBtn, csrf, el, fmtDate, fmtWhen, hostOf, icon, note, onAuthLoss, onEnter, out, refreshMe, safeUrl, setView, shortHost, toast, uuid, vhead } from "./core.js";

/* ---------- shared view pieces ---------- */
const qInput = $("#q"), qClear = $("#qclear");
const colName = () => (S.me?.collections || []).find(c => c.id === S.col)?.name || "this collection";
const colShared = () => (S.me?.collections || []).find(c => c.id === S.col)?.kind === "shared";
const byName = (a, b) => a === "Other" ? 1 : b === "Other" ? -1 : a.localeCompare(b);
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

// Segmented control: one pressed button per group; the group never re-renders.
function seg(label, options, current, onPick) {
  const g = el("div", { class: "seg", role: "group", "aria-label": label });
  for (const [value, text] of options) {
    const b = el("button", { type: "button", "aria-pressed": String(value === current), "data-v": value }, text);
    b.addEventListener("click", () => {
      for (const x of g.children) x.setAttribute("aria-pressed", String(x === b));
      onPick(value);
    });
    g.append(b);
  }
  return g;
}
// Collection switcher in the view header: the same library, another collection.
function colSelect() {
  const sel = el("select", { class: "colsel", "aria-label": "Collection" },
    ...(S.me?.collections || []).map(c => el("option", { value: c.id, text: c.name })));
  sel.value = S.col;
  sel.addEventListener("change", () => {
    S.col = sel.value; catsFresh(); resetHeaderFilters(); setView(S.view); refreshInboxCount();
  });
  return sel;
}
function head(title, ...rest) {
  vhead.replaceChildren(el("div", { class: "vhead" },
    el("div", { class: "vtitle" }, el("h1", { text: title }), ...rest.slice(0, 1)), ...rest.slice(1)));
}
function renderError(e) {
  out.replaceChildren(el("div", { class: "empty", role: "alert" },
    el("b", { text: e.message || "Something went wrong." }),
    el("button", { class: "btn primary", type: "button", onclick: () => setView(S.view) }, "Try again")));
}
function tagList(tags, max) {
  if (!(tags || []).length) return null;
  const shown = max ? tags.slice(0, max) : tags;
  return el("div", { class: "tags" },
    ...shown.map(t => el("button", { class: "tag", type: "button", "data-tag": t,
      "aria-label": `Show items tagged ${t}` }, t)),
    tags.length > shown.length ? el("span", { class: "tag more", text: `+${tags.length - shown.length}` }) : null);
}
function byLine(e) {
  const mail = (e.contributors || []).map(c => c.email).filter(Boolean);
  const me = S.me?.user?.email;
  if (!mail.length || (!colShared() && mail.every(m => m === me))) return null;
  return el("p", { class: "by", text: "Added by " + mail.join(", ") });
}

/* ---------- library ---------- */
let tagSel = null, libCount = null, filtBtn = null;
function libHead() {
  tagSel = el("select", { "aria-label": "Filter by tag" });
  tagSel.addEventListener("change", () => { S.tag = tagSel.value; loadEntries(true); });
  libCount = el("span", { class: "num" });
  const tools = el("div", { class: "toolbar libtools", id: "libTools" },
    seg("Kind", [["", "All"], ["link", "Links"], ["prompt", "Prompts"]], S.kind,
      v => { S.kind = v; loadEntries(true); }),
    tagSel,
    seg("Sort", [["az", "By category"], ["new", "Newest"]], S.srt, setSrt),
    seg("Layout", [["grid", "Labels"], ["list", "List"]], S.vt, setVT));
  // Phones fold the toolbar behind one button; wider screens hide the button.
  filtBtn = el("button", { class: "btn small filters", type: "button",
    "aria-expanded": "false", "aria-controls": "libTools" }, "Filters");
  filtBtn.addEventListener("click", () => {
    const open = filtBtn.getAttribute("aria-expanded") !== "true";
    filtBtn.setAttribute("aria-expanded", String(open));
    tools.classList.toggle("open", open);
  });
  head("Library", el("div", { class: "vsub" }, colSelect(), libCount, filtBtn), tools,
    el("div", { class: "catrow", id: "catrow", role: "group", "aria-label": "Categories" }));
  rebuildTagSel();
}
function skeleton() {
  out.replaceChildren(el("ul", { class: "labels", role: "status", "aria-label": "Loading the library" },
    ...Array.from({ length: 6 }, () => el("li", { class: "skel" }))));
}
async function loadEntries(reset) {
  if (!S.me) return;
  const unfiltered = !S.q && !S.cat && !S.tag && !S.kind;
  if (reset) { S.offset = 0; S.entries = []; if (unfiltered) catsFresh(); }
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
    if (seq !== S.seq || S.view !== "library") return;
    S.entries = S.entries.concat(j.entries || []);
    S.count = j.count || 0;
    if (unfiltered) S.total = S.count;
    for (const e of j.entries || []) {
      if (unfiltered) {
        const c = note(e) || "Other";
        S.cats.set(c, (S.cats.get(c) || 0) + 1);
      }
      for (const t of e.tags || []) S.tags.add(t);
    }
    $("#nLib").textContent = S.total || "";
    rebuildTagSel();
    renderLibrary();
  } catch (e) { if (e.status !== 401) renderError(e); }
}
function rebuildTagSel() {
  if (!tagSel) return;
  const cur = S.tag;
  tagSel.replaceChildren(el("option", { value: "", text: "All tags" }),
    ...[...S.tags].sort().map(t => el("option", { value: t, text: t })));
  if (cur && !S.tags.has(cur)) tagSel.append(el("option", { value: cur, text: cur }));
  tagSel.value = cur;
}
function contents(e) {
  const text = e.summary || e.desc;
  return text ? el("p", { class: "contents", text })
    : el("p", { class: "contents none", text: e.interpreted ? "No summary." : "Not read yet." });
}
function kindMark(e) {
  return e.kind === "prompt" ? [icon(ICONS.prompt, 16), el("span", { text: "Prompt" })]
    : [icon(ICONS.link, 16), el("span", { text: "Link" })];
}
const details = id => el("button", { class: "btn small quiet", type: "button", "data-detail": id }, "Details");
function entryLabel(e, grouped) {
  const url = safeUrl(e.url), prompt = e.kind === "prompt";
  const title = prompt ? (e.title || "Untitled prompt") : (e.title || (url ? hostOf(e.url) : "Untitled"));
  const cat = note(e);
  return el("li", { class: "label" },
    el("div", { class: "lhead" },
      !grouped && cat ? el("span", { class: "rb small", text: cat }) : null,
      ...kindMark(e),
      e.found ? el("time", { class: "date", datetime: e.found, text: fmtDate(e.found) }) : null),
    url ? el("a", { class: "ltitle", href: url, target: "_blank", rel: "noopener noreferrer nofollow", text: title })
      : el("span", { class: "ltitle", text: title }),
    url ? el("p", { class: "from", title: url }, el("span", { class: "fname", text: "From" }), shortHost(e.url)) : null,
    prompt ? el("pre", { class: "code", text: e.text || "" }) : contents(e),
    tagList(e.tags, 4),
    byLine(e),
    el("hr", { class: "perf" }),
    el("div", { class: "acts" },
      prompt && e.text ? copyBtn(() => e.text, "Copy prompt") : null,
      e.install ? copyBtn(() => e.install, "Copy install") : null,
      details(e.id)));
}
function entryRow(e, grouped) {
  const url = safeUrl(e.url), prompt = e.kind === "prompt";
  const title = prompt ? (e.title || "Untitled prompt") : (e.title || (url ? hostOf(e.url) : "Untitled"));
  const line = prompt ? e.text : (e.summary || e.desc);
  return el("li", {},
    el("div", { class: "rmain" },
      el("div", { class: "rline" },
        !grouped && note(e) ? el("span", { class: "rb small", text: note(e) }) : null,
        url ? el("a", { class: "ltitle", href: url, target: "_blank", rel: "noopener noreferrer nofollow", text: title })
          : el("span", { class: "ltitle", text: title }),
        url ? el("span", { class: "from", text: shortHost(e.url) }) : null),
      line ? el("p", { class: "contents", text: line }) : null),
    el("div", { class: "rside" },
      e.found ? el("time", { class: "date", datetime: e.found, text: fmtDate(e.found) }) : null,
      prompt && e.text ? copyBtn(() => e.text, "Copy prompt")
        : e.install ? copyBtn(() => e.install, "Copy install") : null,
      details(e.id)));
}
function listWrap(list, grouped) {
  return S.vt === "list"
    ? el("ul", { class: "rows" }, ...list.map(e => entryRow(e, grouped)))
    : el("ul", { class: "labels" }, ...list.map(e => entryLabel(e, grouped)));
}
const slug = c => c.toLowerCase().replace(/[^a-z0-9]+/g, "-");
function renderLibrary() {
  if (libCount) libCount.textContent = S.count === S.total || !S.total
    ? plural(S.count, "item", "items") : `${S.count} of ${plural(S.total, "item", "items")}`;
  const active = (S.kind ? 1 : 0) + (S.tag ? 1 : 0);
  if (filtBtn) filtBtn.textContent = active ? `Filters (${active})` : "Filters";
  const frag = document.createDocumentFragment();
  if (!S.entries.length) frag.append(emptyLibrary());
  else if (S.srt === "az") {
    const groups = new Map();
    for (const e of S.entries) {
      const c = note(e) || "Other";
      if (!groups.has(c)) groups.set(c, []);
      groups.get(c).push(e);
    }
    for (const c of [...groups.keys()].sort(byName)) {
      const list = groups.get(c), id = "g-" + slug(c);
      frag.append(el("section", { class: "group", "aria-labelledby": id },
        el("div", { class: "rhead" }, el("h2", { class: "rb", id, text: c }),
          el("span", { class: "rcount", text: plural(list.length, "item", "items") })),
        listWrap(list, true)));
    }
  } else frag.append(listWrap(S.entries, false));
  if (S.entries.length && S.entries.length < S.count)
    frag.append(el("div", { class: "more-row" },
      el("button", { class: "btn", type: "button",
        onclick: () => { S.offset = S.entries.length; loadEntries(false); } },
        `Show ${Math.min(S.limit, S.count - S.entries.length)} more (${S.count - S.entries.length} left)`)));
  out.replaceChildren(frag);
  renderToc();
}
function emptyLibrary() {
  if (S.q || S.cat || S.tag || S.kind) {
    const bits = [];
    if (S.q) bits.push(`matches “${S.q}”`);
    if (S.cat) bits.push(`is in “${S.cat}”`);
    if (S.tag) bits.push(`has the tag “${S.tag}”`);
    if (S.kind) bits.push(`is a ${S.kind}`);
    return el("div", { class: "empty" },
      el("b", { text: "No saved item " + bits.join(" and ") + "." }),
      el("button", { class: "btn primary", type: "button", onclick: resetFilters }, "Clear the filters"));
  }
  return el("div", { class: "empty" },
    el("b", { text: `Nothing in ${colName()} yet.` }),
    el("p", { text: "Share a link to Clipshelf on your phone, or press Add to paste links. The server fetches each link, reads it, and shelves it here." }),
    el("button", { class: "btn primary", type: "button", onclick: openAdd }, "Add a capture"));
}
function resetHeaderFilters() {
  S.q = ""; S.cat = ""; S.tag = ""; S.kind = "";
  qInput.value = ""; qClear.hidden = true;
}
function resetFilters() { resetHeaderFilters(); setView("library"); }
function pickCat(c) { S.cat = c; loadEntries(true); }
function renderToc() {
  const tsec = $("#tsec"), row = $("#catrow");
  if (S.view !== "library") { tsec.replaceChildren(); return; }
  const cats = [...S.cats.keys()].sort(byName);
  const link = (c, text, n) => el("a", { href: c ? "#g-" + slug(c) : "#library", class: S.cat === c ? "on" : "",
    "aria-current": S.cat === c ? "true" : null, onclick: ev => { ev.preventDefault(); pickCat(c); } },
    el("span", { text }), n != null ? el("span", { class: "count", text: String(n) }) : null);
  tsec.replaceChildren(el("h2", { text: "Categories" }), link("", "All categories", S.total || null),
    ...cats.map(c => link(c, c, S.cats.get(c))));
  if (row) row.replaceChildren(
    el("button", { type: "button", "aria-pressed": String(!S.cat), onclick: () => pickCat("") }, "All"),
    ...cats.map(c => el("button", { type: "button", "aria-pressed": String(S.cat === c), onclick: () => pickCat(c) },
      c, el("span", { class: "count", text: String(S.cats.get(c)) }))));
}

/* card actions (delegated) */
function filterTag(t) {
  if (detailDlg.open) detailDlg.close();
  S.tag = t;
  if (S.view !== "library") setView("library"); else loadEntries(true);
}
out.addEventListener("click", e => {
  const tag = e.target.closest("[data-tag]");
  if (tag) { filterTag(tag.dataset.tag); return; }
  const det = e.target.closest("[data-detail]");
  if (det) openDetail(det.dataset.detail);
});

/* ---------- inbox: every capture is a tracked shipment ---------- */
const STEPS = ["Received", "Fetched", "Read", "Shelved"];
const lastReach = new Map();  // job id -> furthest step drawn last time, for the fill animation
function progress(j) {
  const fetched = j.acquisition === "complete" || j.acquisition === "partial";
  const read = j.interpretation === "complete";
  const readBad = j.interpretation === "blocked" || j.interpretation === "error";
  const fetchBad = j.acquisition === "blocked" || j.acquisition === "error";
  let done = 0;
  if (fetched) done = 1;
  if (fetched && read) done = 2;
  if (j.state === "done" && read) done = 3;
  let stop = -1, now = -1;
  if (j.state === "blocked" || (j.state === "done" && readBad)) stop = fetched ? 2 : 1;
  else if (fetchBad && !j.active) stop = 1;
  else if (j.active && done < 3) now = done + 1;
  return { done, now, stop };
}
function statusOf(j) {
  const p = progress(j), why = j.error || "";
  if (p.stop >= 0) {
    if (j.guardrail) return ["stop", "Held by screening", why];
    if (j.state === "done") return ["stop", "Read, but the findings were withheld", why];
    return ["stop", p.stop === 1 ? "Stopped at Fetch" : "Stopped at Read", why];
  }
  if (j.state === "done") return j.acquisition === "partial"
    ? ["warn", "Shelved, with notes", ""] : ["ok", "Shelved", ""];
  if (j.state === "retry" || (j.active && why))
    return ["warn", `Retrying soon after ${plural(j.attempts || 1, "failed attempt", "failed attempts")}`, why];
  if (j.state === "running") return ["", p.done >= 1 ? "Reading with the model" : "Fetching the page", ""];
  return ["", p.done >= 1 ? "Waiting to read" : "Waiting to fetch", ""];
}
function track(j) {
  const p = progress(j), reach = Math.max(p.done, p.now, p.stop);
  const prev = lastReach.get(j.id);
  lastReach.set(j.id, reach);
  return el("ol", { class: "track", "aria-label": "Progress" },
    ...STEPS.map((name, i) => {
      const cls = ["step"];
      if (i <= p.done) cls.push("done");
      if (i === p.now) cls.push("now");
      if (i === p.stop) cls.push("stop");
      if (i < reach) cls.push("lit");
      if (i < reach && prev != null && i >= prev) cls.push("fresh");
      const state = i === p.stop ? "stopped here" : i === p.now ? "in progress" : i <= p.done ? "done" : "not reached";
      return el("li", { class: cls.join(" ") }, el("span", { text: name }), el("span", { class: "vh", text: ", " + state }));
    }));
}
function jobItem(j, onDone) {
  const [kind, text, why] = statusOf(j);
  const url = safeUrl(j.final_url || j.url);
  const item = el("li", { class: "job" },
    url ? el("a", { class: "jurl", href: url, target: "_blank", rel: "noopener noreferrer nofollow", text: hostOf(url) })
      : el("span", { class: "jurl", text: hostOf(j.final_url || j.url) }),
    track(j),
    el("p", { class: "status " + kind }, text, why ? el("span", { class: "why", text: ": " + why }) : null),
    (j.warnings || []).length ? el("ul", { class: "notes", "aria-label": "Notes" },
      ...j.warnings.map(w => el("li", { text: w }))) : null);
  // can_retry is the action the server accepts right now; finished jobs stay reprocessable.
  if (j.can_retry) {
    const label = j.state === "done" ? "Process again" : "Try again";
    const b = el("button", { class: "btn small", type: "button", "data-fk": "retry:" + j.id }, icon(ICONS.retry), label);
    b.addEventListener("click", async () => {
      b.disabled = true;
      try {
        await api("/api/jobs/" + j.id + "/retry", { method: "POST", body: {} });
        toast("Queued again. The worker picks it up soon.");
        onDone?.();
      } catch (e) { if (e.status !== 401) banner("err", e.message, { sticky: true }); b.disabled = false; }
    });
    item.append(el("div", { class: "acts" }, b));
  }
  return item;
}
async function loadCaptures() {
  if (!S.me) return;
  try {
    const j = await api("/api/captures?collection_id=" + S.col);
    if (S.view !== "inbox") return;
    S.captures = j.captures || [];
    S.capsLoaded = true;
    setInboxCount(S.captures);
    renderInbox();
  } catch (e) { if (e.status !== 401) renderError(e); }
}
function pollCaptures() {
  clearInterval(S.capTimer);
  if (S.view !== "inbox") return;
  let last = "";
  S.capTimer = setInterval(async () => {
    if (S.capsLoaded && !S.captures.some(captureActive)) { clearInterval(S.capTimer); return; }
    try {
      const j = await api("/api/captures?collection_id=" + S.col);
      if (S.view !== "inbox") return;
      const caps = j.captures || [], sig = JSON.stringify(caps);
      if (sig === last) return;  // nothing moved: keep the DOM, focus, and selection
      last = sig;
      S.captures = caps;
      setInboxCount(caps);
      renderInbox();
    } catch {}
  }, 3000);
}
function setInboxCount(caps) {
  const act = caps.filter(captureActive).length, bad = caps.filter(captureBad).length;
  const n = $("#nInbox");
  n.textContent = bad || act || "";
  n.className = "count" + (bad ? " alert" : "");
  n.setAttribute("title", bad ? plural(bad, "capture needs you", "captures need you")
    : act ? plural(act, "capture in progress", "captures in progress") : "");
}
async function refreshInboxCount() {
  try { setInboxCount((await api("/api/captures?collection_id=" + S.col)).captures || []); } catch {}
}
let inSum = null, inSeg = null;
const IN_NAMES = { all: "All", active: "In progress", attention: "Needs you" };
function inboxHead() {
  inSum = el("span", {});
  inSeg = seg("Show", Object.entries(IN_NAMES), S.capFilter, v => { S.capFilter = v; renderInbox(); });
  head("Inbox", el("div", { class: "vsub" }, colSelect(), inSum), el("div", { class: "toolbar" }, inSeg));
}
function keepFocus(fn) {
  const k = document.activeElement?.dataset?.fk;
  fn();
  if (k) for (const n of out.querySelectorAll("[data-fk]")) if (n.dataset.fk === k) { n.focus(); break; }
}
function renderInbox() {
  const all = S.captures, act = all.filter(captureActive).length, bad = all.filter(captureBad).length;
  inSum.replaceChildren(plural(all.length, "capture", "captures"),
    act ? ` · ${act} in progress` : "", bad ? el("span", { class: "need", text: ` · ${bad} need you` }) : "");
  const n = { all: all.length, active: act, attention: bad };
  for (const b of inSeg.children) b.textContent = `${IN_NAMES[b.dataset.v]} ${n[b.dataset.v]}`;
  let caps = all;
  if (S.capFilter === "active") caps = caps.filter(captureActive);
  if (S.capFilter === "attention") caps = caps.filter(captureBad);
  keepFocus(() => {
    if (!caps.length) {
      out.replaceChildren(el("div", { class: "empty" },
        el("b", { text: S.capFilter === "all" ? `No captures in ${colName()} yet.` :
          S.capFilter === "active" ? "Nothing is in progress." : "Nothing needs you." }),
        S.capFilter === "all" ? el("p", { text: "Press Add to paste links, or import a TikTok export or a library.json." }) : null));
      return;
    }
    out.replaceChildren(el("ul", { class: "ships" }, ...caps.map(c => {
      const jobs = c.jobs || [], when = c.receipt?.received_at || c.received_at;
      return el("li", { class: "ship" },
        el("div", { class: "shead" }, el("span", { class: "fname", text: "Received" }),
          when ? el("time", { datetime: when, text: fmtWhen(when) }) : null,
          el("span", { text: plural(jobs.length, "link", "links") }),
          c.receipt?.notice ? el("span", { class: "badge", text: c.receipt.notice }) : null),
        c.text ? el("p", { class: "stext", text: c.text }) : null,
        jobs.length ? el("ul", { class: "jobs" }, ...jobs.map(j => jobItem(j, loadCaptures)))
          : el("p", { class: "hint", text: "This text holds no link, so there is nothing to fetch." }));
    })));
  });
  renderToc();
}

/* ---------- entry details: label, sources, files, tracking ---------- */
const detailDlg = $("#detailDlg"), detBody = $("#detBody");
let detOpener = null;  // focus goes back here when the details close
detailDlg.addEventListener("close", () => { if (detOpener?.isConnected) detOpener.focus(); detOpener = null; });
detBody.addEventListener("click", e => {
  const tag = e.target.closest("[data-tag]");
  if (tag) filterTag(tag.dataset.tag);
});
const dsec = (title, ...kids) => el("section", { class: "dsec" }, el("h3", { text: title }), ...kids);
async function openDetail(id) {
  $("#detHead").textContent = "Details";
  detBody.replaceChildren(el("p", { class: "hint", text: "Loading…" }));
  if (!detailDlg.open) {
    detOpener = document.activeElement;
    // Wide screens read details in a side panel beside the list; phones get the full sheet.
    if (globalThis.matchMedia?.("(min-width: 961px)").matches) detailDlg.show(); else detailDlg.showModal();
  }
  let j;
  try { j = await api("/api/entries/" + id); }
  catch (e) {
    if (e.status === 401) return;
    detBody.replaceChildren(el("p", { class: "err", text: e.message })); return;
  }
  const e = j.entry || {}, prompt = e.kind === "prompt";
  $("#detHead").textContent = e.title || (prompt ? "Untitled prompt" : e.url ? shortHost(e.url) : "Entry");
  const url = safeUrl(e.url), cat = note(e);
  const contrib = (e.contributors || []).map(c => c.email).filter(Boolean);
  let remove = null;
  if (e.can_remove) {
    remove = el("button", { class: "btn small", type: "button" }, "Remove from collection");
    remove.addEventListener("click", async () => {
      const vals = await confirmDialog({ head: "Remove this entry?",
        text: "Your contributions to this entry leave this collection. Contributions by other members and retained files follow the collection rules.",
        ok: "Remove" });
      if (!vals) return;
      try {
        await api("/api/entries/" + id, { method: "DELETE" });
        detailDlg.close(); toast("Entry removed."); if (S.view === "library") loadEntries(true);
      } catch (err) { if (err.status !== 401) banner("err", err.message, { sticky: true }); }
    });
  }
  detBody.replaceChildren(...[
    el("div", { class: "dmeta" },
      cat ? el("span", { class: "rb small", text: cat }) : null,
      el("span", { text: prompt ? "Prompt" : "Link" }),
      el("span", { class: "badge" + (e.interpreted ? " ok" : ""), text: e.interpreted ? "Read by the model" : "Not read yet" }),
      e.found ? el("time", { class: "date", datetime: e.found, text: "Saved " + fmtWhen(e.found) }) : null),
    url ? el("a", { class: "dfrom", href: url, target: "_blank", rel: "noopener noreferrer nofollow" },
      icon(ICONS.ext, 16), shortHost(e.url)) : null,
    e.summary || e.desc ? el("p", { class: "dsum", text: e.summary || e.desc }) : null,
    e.summary && e.desc ? el("p", { class: "ddesc", text: e.desc }) : null,
    prompt && e.text ? dsec("Prompt", el("pre", { class: "code tall", text: e.text }),
      el("div", { class: "row" }, copyBtn(() => e.text, "Copy prompt"))) : null,
    e.install ? dsec("Install (copy only: Clipshelf never runs it)", el("pre", { class: "code", text: e.install }),
      el("div", { class: "row" }, copyBtn(() => e.install, "Copy install"))) : null,
    !prompt && e.text ? dsec("Extracted text", el("details", {}, el("summary", { text: "Show the text the server saved" }),
      el("pre", { class: "code tall", text: e.text }))) : null,
    (e.tags || []).length ? dsec("Tags", tagList(e.tags)) : null,
    (e.sources || []).length ? dsec("Sources", el("ul", {}, ...e.sources.map(s => {
      const u = safeUrl(typeof s === "string" ? s : s.url);
      const t = (typeof s === "object" && s.title) || u || String(s);
      return el("li", {}, u ? el("a", { href: u, target: "_blank", rel: "noopener noreferrer nofollow", text: t })
        : el("span", { text: t }));
    }))) : null,
    dsec("Retained files", renderAssets(j.assets || [])),
    dsec("Processing", ...(j.jobs || []).length
      ? [el("ul", { class: "jobs" }, ...j.jobs.map(job => jobItem(job, () => openDetail(id)))),
        el("p", { class: "hint", text: j.jobs.map(job => `Job ${job.state}, fetch ${job.acquisition}, read ${job.interpretation}, ${plural(job.attempts || 0, "attempt", "attempts")}`).join(". ") + "." })]
      : [el("p", { class: "hint", text: "No processing is recorded for this entry." })]),
    contrib.length || remove ? el("div", { class: "dfoot" },
      contrib.length ? el("span", { text: "Added by " + contrib.join(", ") }) : el("span", {}), remove) : null
  ].filter(Boolean));
}
// Only root-relative /api/assets/<uuid> URLs resolve; everything else is rejected.
function assetUrl(raw) {
  if (typeof raw !== "string") return null;
  if (!/^\/api\/assets\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(raw)) return null;
  try {
    const u = new URL(raw, location.origin);
    return u.origin === location.origin && !u.search && !u.hash ? u.href : null;
  } catch { return null; }
}
function renderAssets(assets) {
  if (!assets.length) return el("p", { class: "hint",
    text: "The server kept no files for this entry." });
  const grid = el("div", { class: "thumbs" });
  const other = [];
  for (const a of assets) {
    const u = assetUrl(a.url);
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
  const wrap = el("div", { class: "dsec" });
  if (grid.childNodes.length) wrap.append(grid);
  for (const a of other) {
    const u = assetUrl(a.url); if (!u) continue;
    const row = el("div", { class: "file" },
      el("span", { class: "badge", text: a.kind || (a.content_type || "").split("/")[0] || "file" }),
      el("a", { href: u, target: "_blank", rel: "noopener" }, a.name || u));
    if (/^text\//.test(a.content_type || "") || a.kind === "page" || a.kind === "captions") {
      const view = el("button", { class: "btn small", type: "button" }, icon(ICONS.eye), "View text");
      view.addEventListener("click", async () => {
        view.disabled = true;
        try {
          const r = await fetch(u, { credentials: "same-origin" });
          const txt = await r.text();
          row.after(el("pre", { text: txt.slice(0, 40000), class: "code tall" }));
          return;
        } catch { banner("err", "The saved text did not load. Try again.", { sticky: true }); }
        view.disabled = false;
      });
      row.append(view);
    }
    wrap.append(row);
  }
  return wrap;
}

/* ---------- add dialog: capture + import ---------- */
const addDlg = $("#addDlg"), capText = $("#capText"), capErr = $("#capErr"),
  capCol = $("#capCol"), capBtn = $("#capBtn");
function openAdd() {
  capErr.hidden = true;
  capCol.replaceChildren(...(S.me?.collections || []).map(c =>
    el("option", { value: c.id, text: c.name + (c.kind === "personal" ? " (private)" : "") })));
  capCol.value = S.col;
  addDlg.showModal();
  capText.focus();
}
$("#addBtn").addEventListener("click", openAdd);
capBtn.addEventListener("click", async () => {
  const text = capText.value.trim();
  capErr.hidden = true;
  if (!text) { capErr.textContent = "Paste at least one link."; capErr.hidden = false; capText.focus(); return; }
  capBtn.disabled = true;
  try {
    const j = await api("/api/captures", { method: "POST", body: {
      client_request_id: uuid(), text, collection_id: capCol.value,
      instance_id: S.me.instance_id, user_id: S.me.user.id } });
    addDlg.close(); capText.value = "";
    banner("ok", "Capture saved. Follow it in the Inbox." + (j.receipt?.notice ? " " + j.receipt.notice : ""), { ms: 6000 });
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
    impFill.style.transform = `scaleX(${pct / 100})`;
    impProg.setAttribute("aria-valuenow", pct);
    impStat.textContent = `Uploading ${pct}% (${(ev.loaded / 1048576).toFixed(1)} of ${(ev.total / 1048576).toFixed(1)} MiB)`;
  });
  xhr.addEventListener("load", () => {
    impBtn.disabled = false;
    let data = null; try { data = JSON.parse(xhr.responseText); } catch {}
    if (xhr.status === 200 || xhr.status === 201) {
      if (data?.import?.status === "error") {
        // Same-digest replays answer 200 with the stored failure; the file is
        // never reprocessed, so the uploader must see the error, not a banner.
        impProg.hidden = true;
        impErr.textContent = `The server could not process this import: ${String(data.import.error || "unknown error").slice(0, 300)}. Re-export the file and upload it again, or check the server log.`;
        impErr.hidden = false;
        return;
      }
      impProg.hidden = true; impFill.style.transform = "scaleX(0)";
      impStat.textContent = "";
      impFile.value = ""; $("#impName").textContent = ""; impBtn.disabled = true;
      addDlg.close();
      banner("ok", "Import accepted. The entries appear in the Library while the worker processes them.", {
        actions: [{ label: "Open Library", onclick: () => setView("library") }], ms: 8000 });
      clearTimeout(S.libTimer);
      S.libTimer = setTimeout(() => { if (S.view === "library") loadEntries(true); }, 4000);
      return;
    }
    if (xhr.status === 401) { onAuthLoss(); return; }
    impProg.hidden = true;
    const msg = data && (typeof data === "object" ? (data.detail || data.error) : data) ||
      FALLBACK[xhr.status] || `Import failed (${xhr.status}).`;
    impErr.textContent = xhr.status === 413
      ? "This export is larger than the server's import ceiling. Split it, or ask your administrator to raise the limit."
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
  head("Collections", el("div", { class: "vsub", text: plural((me.collections || []).length, "collection", "collections") }));
  const name = el("input", { type: "text", id: "newCol", maxlength: 120, required: true, class: "grow",
    autocomplete: "off" });
  const form = el("form", { class: "panel" }, el("h2", { text: "New shared collection" }),
    el("div", { class: "field" }, el("label", { for: "newCol", text: "Name" }),
      el("div", { class: "row" }, name, el("button", { class: "btn primary", type: "submit", text: "Create" }))),
    el("p", { class: "hint", text: "You become the owner and choose the members. Members add and remove their own contributions." }));
  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    const n = name.value.trim();
    if (!n) return;
    try {
      await api("/api/collections", { method: "POST", body: { name: n } });
      await refreshMe(); setView("collections"); toast(`Created “${n}”.`);
    } catch (e) { if (e.status !== 401) banner("err", e.message, { sticky: true }); }
  });
  const frag = document.createDocumentFragment();
  for (const c of me.collections || []) {
    const card = el("section", { class: "panel" },
      el("div", { class: "colrow" },
        el("h2", { text: c.name }),
        el("span", { class: "badge", text: c.kind === "personal" ? "Private to you" : "Shared" }),
        c.id === S.col ? el("span", { class: "badge ink", text: "Open now" }) : null,
        el("button", { class: "btn open", type: "button", onclick: () => {
          S.col = c.id; catsFresh(); resetHeaderFilters(); setView("library"); refreshInboxCount();
        } }, "Open")));
    if (c.kind === "shared" && c.can_manage) card.append(membersPanel(c));
    frag.append(card);
  }
  frag.append(form);
  out.replaceChildren(frag);
  renderToc();
}
function membersPanel(c) {
  const p = el("details", { class: "members" }, el("summary", { text: "Members" }));
  const body = el("div", {});
  p.append(body);
  const load = async () => {
    body.replaceChildren(el("p", { class: "hint", text: "Loading the members…" }));
    let j;
    try { j = await api("/api/collections/" + c.id); }
    catch (e) { if (e.status !== 401) body.replaceChildren(el("p", { class: "err", text: e.message })); return; }
    const rows = (j.members || []).map(m => {
      const rm = el("td", {});
      if (!m.is_owner)
        rm.append(el("div", { class: "acts" }, el("button", { class: "btn small", type: "button", text: "Remove",
          onclick: async () => {
            const vals = await confirmDialog({ head: "Remove this member?",
              text: `${m.email} loses access to “${c.name}”. Their contributions stay.`,
              ok: "Remove" });
            if (!vals) return;
            try { await api(`/api/collections/${c.id}/members/${m.id}`, { method: "DELETE" }); load(); }
            catch (e) { if (e.status !== 401 && !e.cancelled) banner("err", e.message, { sticky: true }); }
          } })));
      return el("tr", {},
        el("td", { text: m.email }),
        el("td", {}, el("span", { class: "badge" + (m.is_owner ? " ink" : ""), text: m.is_owner ? "Owner" : "Member" })),
        rm);
    });
    const email = el("input", { type: "email", id: "addMem-" + c.id, required: true, class: "grow", autocomplete: "off" });
    const addForm = el("form", { class: "field" },
      el("label", { for: "addMem-" + c.id, text: "Add a member by email" }),
      el("div", { class: "row" }, email, el("button", { class: "btn", type: "submit", text: "Add member" })));
    addForm.addEventListener("submit", async ev => {
      ev.preventDefault();
      try { await api(`/api/collections/${c.id}/members`, { method: "POST", body: { email: email.value.trim() } }); load(); }
      catch (e) { if (e.status !== 401 && !e.cancelled) banner("err", e.message, { sticky: true }); }
    });
    body.replaceChildren(
      el("div", { class: "tablewrap" }, el("table", {},
        el("thead", {}, el("tr", {},
          el("th", { text: "Email" }), el("th", { text: "Role" }), el("th", { text: "" }))),
        el("tbody", {}, ...rows))),
      addForm,
      el("p", { class: "hint", text: "Only existing, active accounts can join. The owner cannot be removed." }));
  };
  p.addEventListener("toggle", () => { if (p.open && !body.childNodes.length) load(); });
  return p;
}

/* ---------- settings ---------- */
function renderSettings() {
  const me = S.me; if (!me) return;
  head("Settings");
  const sel = el("select", { id: "defCol" },
    ...(me.collections || []).map(c => el("option", { value: c.id,
      text: c.name + (c.kind === "personal" ? " (private)" : "") })));
  sel.value = me.default_collection_id || S.col || "";
  const form = el("form", { class: "panel" }, el("h2", { text: "New captures go to" }),
    el("div", { class: "field" }, el("label", { for: "defCol", text: "Default collection" }), sel,
      el("p", { class: "help", text: "Used for new captures from the web and the Android app. A share that waits in the phone outbox keeps the collection it had when you shared it." })),
    el("button", { class: "btn primary", type: "submit", text: "Save" }));
  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    try {
      S.me = await api("/api/settings", { method: "POST",
        body: { default_collection_id: sel.value } });
      toast("Default collection saved.");
    } catch (e) { if (e.status !== 401) banner("err", e.message, { sticky: true }); }
  });
  const logoutForm = el("form", { method: "post", action: "/accounts/logout/" },
    el("input", { type: "hidden", name: "csrfmiddlewaretoken", value: csrf() }),
    el("button", { class: "btn", type: "submit", text: "Sign out" }));
  out.replaceChildren(
    el("section", { class: "panel" }, el("h2", { text: "Account" }),
      el("p", { class: "row" }, "Signed in as ", el("b", { text: me.user.email }),
        me.user.is_app_admin ? el("span", { class: "badge ink", text: "Admin" }) : null),
      el("div", { class: "acts", style: "margin-top:16px" },
        el("a", { class: "btn", href: "/accounts/email/" }, "Email addresses"),
        el("a", { class: "btn", href: "/accounts/password/change/" }, "Change password"),
        logoutForm)),
    form);
  renderToc();
}

/* ---------- header: search always looks in the library ---------- */
let qTimer = 0;
qInput.addEventListener("input", () => {
  qClear.hidden = !qInput.value;
  clearTimeout(qTimer);
  qTimer = setTimeout(() => {
    S.q = qInput.value.trim();
    if (S.view !== "library") setView("library"); else loadEntries(true);
  }, 300);
});
qClear.addEventListener("click", () => {
  qInput.value = ""; qClear.hidden = true; S.q = ""; qInput.focus();
  if (S.view === "library") loadEntries(true);
});
addEventListener("keydown", e => {
  if (e.key === "/" && !/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) && !e.target.isContentEditable &&
      !document.querySelector("dialog:modal")) {
    e.preventDefault(); qInput.focus();
  }
  if (e.key === "Escape" && document.activeElement === qInput && qInput.value) qClear.click();
  else if (e.key === "Escape" && detailDlg.open && !detailDlg.matches(":modal")) detailDlg.close();
});
function setVT(v) {
  S.vt = v; localStorage.setItem("clipshelf.view", v);
  if (S.view === "library" && S.entries.length) renderLibrary();
}
function setSrt(s) {
  S.srt = s; localStorage.setItem("clipshelf.sort", s);
  loadEntries(true);
}

/* ---------- view registrations ---------- */
onEnter("library", () => { libHead(); loadEntries(true); });
onEnter("inbox", () => { inboxHead(); S.capsLoaded = false; loadCaptures(); pollCaptures(); });
onEnter("collections", renderCollections);
onEnter("settings", renderSettings);
export { progress, refreshInboxCount, renderToc, statusOf };
