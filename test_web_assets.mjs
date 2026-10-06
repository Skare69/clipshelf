/* test_web_assets.mjs — retained-asset URL rendering check (red-before/green-after).
   Run from the repository root:
       node test_web_assets.mjs
   Drives the real openDetail -> renderAssets code path in
   clipshelf/static/clipshelf/panels.js against a minimal DOM stub, then asserts:
     1. image/video/audio/text retained assets render with an absolute
        same-origin /api/assets/<uuid> URL (fails on the original bug where
        relative /api/assets URLs were dropped by safeUrl), and
     2. dangerous URLs (external hosts, other paths, schemes, queries,
        fragments, traversal) render no media and no link.
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
      for (const k of kids.flat(9)) {
        if (k == null) continue;
        if (typeof k === "object") { k.parentElement = this; this.children.push(k); }
        this.childNodes.push(k);
      }
    },
    replaceChildren(...kids) {
      this.children = kids.filter((k) => k != null);
      this.children.forEach((k) => (k.parentElement = this));
      this.childNodes = [...this.children];
    },
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    closest(sel) {
      const key = sel.replace(/^\[/, "").replace(/\]$/, "").slice(5); // [data-x] -> x
      for (let n = this; n; n = n.parentElement) if (n.dataset[key] != null) return n;
      return null;
    },
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
