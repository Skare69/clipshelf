/* test_cat_rail.mjs — the category rail counts the whole library (red-before/green-after).
   Run from the repository root:
       node test_cat_rail.mjs
   Drives the real loadEntries -> renderLibrary -> renderToc code path in
   clipshelf/static/clipshelf/panels.js against a minimal DOM stub, then asserts:
     1. the rail lists "Zebra" with its full count even though every Zebra
        entry sorts after the first 100-title page (fails before the fix,
        where counts came from the loaded page alone), and
     2. "All categories" shows the whole-library total.
   Uses node: builtins only; never talks to the network. */
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

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
    closest() { return null; },
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
  createDocumentFragment: () => fakeEl("#fragment"),
};
globalThis.location = { origin: "https://library.test" };
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.addEventListener = () => {};
globalThis.scrollTo = () => {};

/* ---------- load the real modules ---------- */
const panelsPath = process.env.PANELS_PATH ||
  new URL("./clipshelf/static/clipshelf/panels.js", import.meta.url).href;
const panelsUrl = panelsPath.startsWith("file:")
  ? panelsPath : pathToFileURL(panelsPath).href;
const { loadEntries } = await import(panelsUrl);
// Same core.js instance the panels module loaded: state edits must be visible.
const { S, catsFresh } = await import(new URL("./core.js", panelsUrl).href);

/* ---------- first load: 150 items, "Zebra" only after the first page ---------- */
S.me = { user: { email: "me@library.test" }, collections: [{ id: "c1", name: "Personal" }] };
S.col = "c1";
S.view = "library";
catsFresh();
const page = {
  entries: Array.from({ length: 100 }, (_, i) => ({
    id: String(i), kind: "link", url: `https://example.com/item-${String(i).padStart(3, "0")}`,
    title: "", desc: "", summary: "", text: "", cat: [], tags: [], sources: [],
    found: "", interpreted: false, contributors: [], can_remove: true,
  })),
  count: 150,
  cats: { Other: 100, Zebra: 50 },
};
globalThis.fetch = async () => ({ ok: true, status: 200, text: async () => JSON.stringify(page) });
await loadEntries(true);

/* ---------- the rail must come from the server's whole-library map ---------- */
const tsec = bySel.get("#tsec");
const catrow = bySel.get("#catrow");
const links = function collect(node, list = []) {
  list.push(node);
  for (const c of node.children) collect(c, list);
  return list;
}(tsec).filter((n) => n.tagName === "A" && n.children[0]?.textContent);
const find = (label) => links.find((a) => a.children[0].textContent === label);
assert.ok(find("Zebra"), `rail shows Zebra; got: ${links.map((a) => a.children[0].textContent).join(", ")}`);
assert.equal(find("Zebra").children[1].textContent, "50", "Zebra shows its full count");
assert.equal(find("Other").children[1].textContent, "100");
assert.equal(find("All categories").children[1].textContent, "150", "All categories shows the whole-library total");
const rowButtons = function collect(node, list = []) {
  list.push(node);
  for (const c of node.children) collect(c, list);
  return list;
}(catrow).filter((n) => n.tagName === "BUTTON" && n.childNodes.includes("Zebra"));
assert.ok(rowButtons.length && rowButtons[0].children.at(-1).textContent === "50",
  "phone category row shows Zebra with its full count");
console.log("ok: the rail counts the whole library, not just the loaded page");
