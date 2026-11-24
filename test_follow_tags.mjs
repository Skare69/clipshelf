/* test_follow_tags.mjs — "Suggest tags" on the Followed sources form.
   Run from the repository root:
       node test_follow_tags.mjs
   Imports the real panels.js against a stub DOM and renders Settings. Asserts:
   the button is off with a tooltip when no LLM is configured; a press posts
   the profile URL, shows a busy state, and renders chips that fill the Topic
   tag input; an empty URL or a 502 shows an inline error and no chips.
   Uses node: builtins only. */
import assert from "node:assert/strict";

class Stub extends EventTarget {
  dataset = {}; children = []; hidden = false; value = ""; textContent = ""; className = "";
  attributes = new Map();
  classList = { toggle() {}, add() {}, remove() {} };
  // Like the real DOM: the disabled property reflects the attribute.
  get disabled() { return this.attributes.has("disabled"); }
  set disabled(v) { if (v) this.attributes.set("disabled", ""); else this.attributes.delete("disabled"); }
  setAttribute(n, v) {
    this.attributes.set(n, String(v));
    if (n.startsWith("data-")) this.dataset[n.slice(5)] = String(v);
  }
  getAttribute(n) { return this.attributes.has(n) ? this.attributes.get(n) : null; }
  removeAttribute(n) { this.attributes.delete(n); }
  append(...kids) { for (const k of kids.flat(9)) if (k != null && k !== false) this.children.push(k); }
  replaceChildren(...kids) { this.children = kids.flat(9).filter(k => k != null && k !== false); }
  closest() { return null; }
  querySelector() { return new Stub(); }
  querySelectorAll() { return []; }
  focus() { document.activeElement = this; }
}
globalThis.localStorage = { getItem: () => null, setItem() {} };
globalThis.scrollTo = () => {};
globalThis.document = {
  cookie: "", activeElement: null,
  createElement: () => new Stub(),
  createElementNS: ns => { const n = new Stub(); n.namespaceURI = ns; return n; },
  querySelector: () => new Stub(),
  querySelectorAll: () => [],
};
globalThis.addEventListener = () => {};

const { S, out, setView } = await import("./clipshelf/static/clipshelf/core.js");
await import("./clipshelf/static/clipshelf/panels.js");  // registers the settings enter hook

const json = (body, status = 200) => ({ ok: status < 400, status, text: async () => JSON.stringify(body) });
const suggests = [];
let reply = () => { throw new Error("no suggest reply set"); };
globalThis.fetch = async (path, opts = {}) => {
  if (path === "/api/follows") return json({ follows: [] });
  if (path === "/api/follows/suggest-tags") { suggests.push(opts); return reply(); }
  throw new Error("unexpected request: " + path);
};

const settle = () => new Promise(r => setTimeout(r, 0));
const all = n => [n, ...(n.children || []).flatMap(all)];
const find = pred => all(out).find(n => typeof n === "object" && pred(n)) || null;
const byId = id => find(n => n.getAttribute?.("id") === id);
const chipGroup = () => find(n => n.getAttribute?.("aria-label") === "Suggested tags");
const errText = () => find(n => n.className === "err")?.textContent ?? null;
const click = n => n.dispatchEvent(new Event("click"));
async function render(llm_configured) {
  S.me = { user: { email: "keeper@example.com" }, collections: [{ id: 1, name: "Main" }], llm_configured };
  setView("settings");
  await settle();  // the follows list loads, then the form renders
  const btn = find(n => n.textContent === "Suggest tags");
  assert.ok(btn, "the follow form renders a Suggest tags button");
  assert.equal(btn.getAttribute("type"), "button", "the button never submits the form");
  return { btn, url: byId("followUrl"), tag: byId("followTag") };
}

// No LLM configured: the button is off and says why.
const off = (await render(false)).btn;
assert.equal(off.disabled, true, "disabled without an LLM");
assert.match(off.getAttribute("title") || "", /admin must set up the LLM connection/);

const { btn, url, tag } = await render(true);
assert.equal(btn.disabled, false, "enabled with an LLM");
assert.equal(btn.getAttribute("title"), null, "no tooltip when enabled");

// Empty profile URL: inline error, focus on the URL field, no request.
click(btn);
await settle();
assert.equal(errText(), "Enter a profile URL first.");
assert.equal(document.activeElement, url, "focus moves to the profile URL");
assert.equal(suggests.length, 0, "no request without a URL");

// Success, with the busy state while the request is pending.
url.value = "  https://www.tiktok.com/@creator ";
let resolve;
reply = () => new Promise(r => { resolve = r; });
click(btn);
await settle();
assert.equal(btn.disabled, true, "disabled while pending");
assert.equal(btn.getAttribute("aria-busy"), "true", "aria-busy while pending");
assert.equal(btn.textContent, "Suggesting…");
assert.equal(errText(), null, "a new press clears the old error");
resolve(json({ tags: ["AI tools", "Video"] }));
await settle();
assert.equal(btn.disabled, false, "enabled again after the reply");
assert.equal(btn.getAttribute("aria-busy"), null, "aria-busy removed after the reply");
assert.equal(btn.textContent, "Suggest tags");
assert.equal(suggests.length, 1);
assert.equal(suggests[0].method, "POST");
assert.deepEqual(JSON.parse(suggests[0].body), { url: "https://www.tiktok.com/@creator" });

const group = chipGroup();
assert.ok(group, "the chips render in a labelled group");
assert.equal(group.getAttribute("role"), "group");
const chips = group.children;
assert.deepEqual(chips.map(c => c.textContent), ["AI tools", "Video"]);
for (const c of chips) {
  assert.equal(c.getAttribute("type"), "button");
  assert.equal(c.getAttribute("data-tag"), null, "no data-tag: the library filter handler must ignore chips");
}
click(chips[1]);
assert.equal(tag.value, "Video", "a chip fills the Topic tag input");
assert.deepEqual(chips.map(c => c.getAttribute("aria-pressed")), ["false", "true"]);
assert.equal(document.activeElement, tag, "focus moves to the Topic tag input");

// Server error: inline message, the old chips are gone.
reply = () => json({ detail: "The LLM did not answer." }, 502);
click(btn);
await settle();
assert.equal(errText(), "The LLM did not answer.");
assert.equal(chipGroup(), null, "no chips after an error");
assert.equal(btn.disabled, false, "enabled again after the error");

console.log("ok: Suggest tags button, chips, busy state and inline errors");
