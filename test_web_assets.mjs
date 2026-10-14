/* test_web_assets.mjs — retained-asset URL rendering check (red-before/green-after).
   Run from the repository root:
       node test_web_assets.mjs
   Drives the real openDetail -> renderAssets code path in
   clipshelf/static/clipshelf/panels.js against a minimal DOM stub, then asserts:
     1. image/video/audio/text retained assets render with an absolute
        same-origin /api/assets/<uuid> URL (fails on the original bug where
        relative /api/assets URLs were dropped by safeUrl), and
     2. dangerous URLs (external hosts, other paths, schemes, queries,
        fragments, traversal) render no media and no link, and
     3. an entry with no url, tags, sources or install renders no "null"
        text (the stub stringifies non-node children like the real DOM).
   Uses node: builtins only; never talks to the network. */
import assert from "node:assert/strict";

const ORIGIN = "https://library.test";
const ABS = (u) => ORIGIN + u;

/* ---------- minimal DOM stub (only what core.js/panels.js touch) ---------- */
function fakeEl(tag) {
  return {
    tagName: String(tag).toUpperCase(),
    attributes: {}, dataset: {}, listeners: {},
    children: [], childNodes: [], parentElement: null,
    className: "", value: "", hidden: false, open: false,
    get textContent() { return this.childNodes.map((c) => typeof c === "string" ? c : c.textContent).join(""); },
    set textContent(v) { this.childNodes = [String(v)]; this.children = []; },
    setAttribute(k, v) { this.attributes[k] = String(v); },
    getAttribute(k) { return this.attributes[k] ?? null; },
    append(...kids) {
      // The real DOM stringifies non-node kids into text nodes (null -> "null").
      for (const k of kids.flat(9)) {
        const node = k != null && typeof k === "object" ? k : String(k);
        if (typeof node === "object") { node.parentElement = this; this.children.push(node); }
        this.childNodes.push(node);
      }
    },
    replaceChildren(...kids) {
      this.children = kids.filter((k) => k != null && typeof k === "object");
      this.children.forEach((k) => (k.parentElement = this));
      this.childNodes = kids.map((k) => k != null && typeof k === "object" ? k : String(k));
    },
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    closest(sel) {
      const key = sel.replace(/^\[/, "").replace(/\]$/, "").slice(5); // [data-x] -> x
      for (let n = this; n; n = n.parentElement) if (n.dataset[key] != null) return n;
      return null;
    },
    querySelectorAll() { return []; },
    focus() { globalThis.document.activeElement = this; },
    showModal() { this.open = true; },
  };
}
const bySel = new Map();
globalThis.document = {
  cookie: "",
  body: fakeEl("body"),
  querySelector: (sel) => { if (!bySel.has(sel)) bySel.set(sel, fakeEl(sel)); return bySel.get(sel); },
  querySelectorAll: () => [],
  createElement: (t) => fakeEl(t),
  createElementNS: (ns, t) => fakeEl(t),
};
globalThis.location = { origin: ORIGIN };
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.addEventListener = () => {};
globalThis.scrollTo = () => {};

/* ---------- load the real module ---------- */
const panelsPath = process.env.PANELS_PATH ||
  new URL("./clipshelf/static/clipshelf/panels.js", import.meta.url).href;
await import(panelsPath);

const outEl = bySel.get("#out");
const detBody = bySel.get("#detBody");
const clickHandler = (outEl.listeners.click || [])[0];
assert.ok(clickHandler, "panels.js did not register the delegated click handler");

/* ---------- helpers ---------- */
function collect(node, list = []) { list.push(node); for (const c of node.children) collect(c, list); return list; }
const mediaSrcs = () => collect(detBody)
  .filter((n) => ["IMG", "VIDEO", "AUDIO"].includes(n.tagName) && n.attributes.src);
const links = () => collect(detBody).filter((n) => n.tagName === "A" && n.attributes.href);

async function renderEntry(assets) {
  const payload = { entry: { title: "T", url: null, tags: [], sources: [] }, jobs: [], assets };
  globalThis.fetch = async () => ({
    ok: true, status: 200, text: async () => JSON.stringify(payload),
  });
  const target = fakeEl("a");
  target.dataset.detail = "probe";
  target.parentElement = outEl;
  clickHandler({ target });
  for (let i = 0; i < 100 && (detBody.children[0]?.className === "hint" || !detBody.children.length); i++)
    await new Promise((r) => setImmediate(r));
}

/* ---------- 1. safe retained assets render as absolute same-origin URLs ---------- */
const IMG = "/api/assets/11111111-2222-4333-8444-555555555555";
const VID = "/api/assets/99999999-8888-4777-8666-555555555555";
const AUD = "/api/assets/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
const TXT = "/api/assets/12345678-90ab-4cde-8f01-234567890abc";
await renderEntry([
  { kind: "image", content_type: "image/png", url: IMG, name: "shot.png" },
  { kind: "video", content_type: "video/mp4", url: VID, name: "clip.mp4" },
  { kind: "audio", content_type: "audio/ogg", url: AUD, name: "take.ogg" },
  { kind: "page", content_type: "text/plain", url: TXT, name: "notes.txt" },
]);
assert.ok(!detBody.textContent.includes("null"),
  `an entry with no url/tags/sources/install must render no "null" text, got: ${JSON.stringify(detBody.textContent)}`);
{
  const srcs = mediaSrcs().map((n) => n.attributes.src).sort();
  assert.deepEqual(srcs,
    [ABS(AUD), ABS(IMG), ABS(VID)].sort(),
    `image/video/audio must render with absolute same-origin URLs, got: ${srcs}`);
  const img = mediaSrcs().find((n) => n.tagName === "IMG");
  assert.equal(img.parentElement.tagName, "A", "retained image must be wrapped in a link");
  assert.equal(img.parentElement.attributes.href, ABS(IMG), "image link must use the absolute asset URL");
  const textLink = links().find((n) => n.attributes.href === ABS(TXT));
  assert.ok(textLink, "text asset must render a link with the absolute asset URL");
  assert.ok((textLink.textContent || "").includes("notes.txt"), "text link must show the asset name");
  for (const n of [...mediaSrcs(), ...links()]) {
    const u = n.attributes.src || n.attributes.href;
    assert.match(u, new RegExp(`^${ORIGIN}/api/assets/[0-9a-f-]+$`),
      `every rendered media/link URL must be same-origin /api/assets/<uuid>, got: ${u}`);
  }
}

/* ---------- 2. unsafe URLs render no media and no link ---------- */
const unsafe = [
  "https://evil.example/a.png",                       // external absolute
  "//evil.example/a.png",                             // protocol-relative
  "javascript:alert(1)",                              // scheme
  "data:image/png;base64,AAAA",                       // scheme
  "/static/clipshelf/favicon.svg",                    // other root-relative path
  "/api/assets/11111111-2222-4333-8444-55555555555/5",// not a canonical uuid
  "/api/assets/not-a-uuid",                           // not a uuid
  "/API/ASSETS/11111111-2222-4333-8444-555555555555", // case-sensitive server route
  `${IMG}?v=2`,                                       // query
  `${IMG}#frag`,                                      // fragment
  `${IMG}/../../secret.png`,                          // traversal
  null,                                               // not a string
];
await renderEntry(unsafe.map((u, i) => ({ kind: "image", content_type: "image/png", url: u, name: `a${i}` }))
  .concat([{ kind: "video", content_type: "video/mp4", url: "https://evil.example/v.mp4" }]));
{
  const srcs = mediaSrcs(), hrefs = links();
  assert.equal(srcs.length, 0,
    `unsafe asset URLs must create no media, got: ${srcs.map((n) => n.attributes.src)}`);
  assert.equal(hrefs.length, 0,
    `unsafe asset URLs must create no link, got: ${hrefs.map((n) => n.attributes.href)}`);
}

console.log(`ok: retained assets render only as absolute same-origin ${ORIGIN}/api/assets/<uuid> URLs`);

/* ---------- 3. failed import replay shows the stored error, not the success banner ---------- */
const { S } = await import("./clipshelf/static/clipshelf/core.js");
S.me = { user: { id: "u1" }, instance_id: "inst", collections: [], default_collection_id: "c1" };
const impFile = bySel.get("#impFile");
const uploadFile = new Blob(["{}"], { type: "application/json" });
uploadFile.name = "tiktok.json";
impFile.files = [uploadFile];
bySel.get("#impFill").style = {}; // success path resets the bar width
const addDlg = bySel.get("#addDlg");
let dialogClosed = false;
addDlg.close = () => { dialogClosed = true; };
class FakeUploadXhr {
  static sent = [];
  constructor() {
    this.listeners = {};
    this.upload = { addEventListener() {} };
    this.status = 0; this.responseText = "";
    FakeUploadXhr.sent.push(this);
  }
  open() {} setRequestHeader() {} send() {}
  addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
}
globalThis.XMLHttpRequest = FakeUploadXhr;
const impClick = (bySel.get("#impBtn").listeners.click || [])[0];
assert.ok(impClick, "panels.js did not register the import click handler");
const bannerEl = bySel.get("#banner");
const impErr = bySel.get("#impErr");
const respond = (status, payload) => {
  const xhr = FakeUploadXhr.sent.at(-1);
  xhr.status = status;
  xhr.responseText = JSON.stringify(payload);
  for (const fn of xhr.listeners.load) fn({});
};

// 200 replay of a failed import: error element, no dialog close, no ok banner.
impClick();
respond(200, { import: { id: "r1", status: "error", error: "too many items (5001 > 5000)" } });
assert.equal(impErr.hidden, false, "failed replay must surface the error element");
assert.match(impErr.textContent, /too many items \(5001 > 5000\)/,
  "error element must carry the stored server error");
assert.match(impErr.textContent, /Re-export|server log/,
  "error element must carry the recovery hint");
assert.equal(dialogClosed, false, "failed replay must not close the dialog");
assert.ok(!bannerEl.className.includes("ok"),
  `failed replay must not raise the success banner, got class "${bannerEl.className}"`);

// Control: a pending import still takes the success path.
impClick();
respond(201, { import: { id: "r2", status: "pending" } });
assert.equal(dialogClosed, true, "accepted import still closes the dialog");
assert.ok(bannerEl.className.includes("ok"), "accepted import still shows the ok banner");
assert.equal(impErr.hidden, true, "accepted import shows no error");
clearTimeout(S.libTimer); // drop the deferred library refresh so node exits

console.log("ok: failed import replay shows the stored error and never the success banner");

/* ---------- 4. searching from a non-library view keeps focus in the search box ---------- */
{
  S.view = "inbox"; // the user types a search while Inbox is open
  const qInput = bySel.get("#q");
  globalThis.document.activeElement = qInput; // focus lives in the search box
  qInput.value = "react";
  globalThis.fetch = async () => ({ ok: true, status: 200,
    text: async () => JSON.stringify({ entries: [], count: 0 }) });
  for (const fn of qInput.listeners.input || []) fn({});
  await new Promise((r) => setTimeout(r, 350)); // past the 300 ms debounce
  assert.equal(S.view, "library", "the search must open the library");
  assert.equal(globalThis.document.activeElement, qInput,
    `the debounce must not move focus out of the search box, got ${globalThis.document.activeElement?.id || "none"}`);
}
console.log("ok: a search that opens the library keeps focus in the search box");

/* ---------- 5. empty invitations panel renders no "null" child (red-before/green-after) ---------- */
/* Drives the real adminInvitations code path in clipshelf/static/clipshelf/admin.js.
   The stub's append mirrors the real DOM: non-Node arguments become text, so a
   `append(null)` fallback would surface as the literal text "null" between the form
   and the hint. Fails at the head that passed `: null` into p.append(...). */
const { adminInvitations } = await import("./clipshelf/static/clipshelf/admin.js");
const collectText = (n) => collect(n).map((c) => c.textContent).join("");
const empty = adminInvitations([], null);
const emptyText = collectText(empty);
assert.ok(!emptyText.includes("null"),
  `empty invitations must render no "null" child, got: ${JSON.stringify(emptyText)}`);
assert.ok(collect(empty).some((c) => c.tagName === "FORM"), "invite form still renders");
assert.match(emptyText, /single-use, expiring links/, "hint still renders");

// Control: one pending invitation still renders the table row, and no "null".
const soon = new Date(Date.now() + 864e5).toISOString();
const one = adminInvitations(
  [{ email: "a@b.c", used_at: null, created_at: soon, expires_at: soon }], null);
const oneText = collectText(one);
assert.ok(!oneText.includes("null"), `rendered row must not contain "null": ${JSON.stringify(oneText)}`);
assert.ok(oneText.includes("a@b.c") && oneText.includes("Pending"), "row renders with status");

console.log("ok: empty invitations panel shows no stray null child");
