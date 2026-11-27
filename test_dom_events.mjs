/* test_dom_events.mjs — DOM event handling in core.js and panels.js.
   Run from the repository root:
       node test_dom_events.mjs
   Imports the real modules against a minimal DOM stub and checks that core.js
   builds elements whose handlers replace inline attributes. Uses node: builtins only. */
import assert from "node:assert/strict";

class TestElement extends EventTarget {
  attributes = new Map();
  dataset = {}; children = []; hidden = false; value = ""; open = false;
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  append() {} replaceChildren() {} closest() { return null; }
  matches() { return false; }
  close() { this.open = false; this.dispatchEvent(new Event("close")); }
}

globalThis.localStorage = { getItem: () => null, setItem() {} };
const els = new Map();
let modalOpen = false;
globalThis.document = {
  cookie: "", activeElement: null,
  createElement: () => new TestElement(),
  querySelector(sel) {
    if (sel === "dialog:modal") return modalOpen ? new TestElement() : null;
    if (!els.has(sel)) els.set(sel, new TestElement());
    return els.get(sel);
  },
  querySelectorAll: () => [],
};
const winHandlers = {};
globalThis.addEventListener = (type, fn) => (winHandlers[type] ||= []).push(fn);
const press = key => { for (const fn of winHandlers.keydown || []) fn({ key }); };

const { el } = await import("./clipshelf/static/clipshelf/core.js");
await import("./clipshelf/static/clipshelf/panels.js");

let clicks = 0;
const button = el("button", { type: "button", disabled: true, title: "Retry", onclick: () => { clicks++; } });
button.dispatchEvent(new Event("click"));
assert.equal(clicks, 1);
assert.equal(button.attributes.has("onclick"), false);
assert.equal(button.attributes.get("type"), "button");
assert.equal(button.attributes.get("disabled"), "");
assert.equal(button.attributes.get("title"), "Retry");

console.log("ok: element events");
