import assert from "node:assert/strict";

class TestElement extends EventTarget {
  attributes = new Map();
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  append() {}
}

globalThis.localStorage = { getItem: () => null };
globalThis.document = {
  createElement: () => new TestElement(),
  querySelector: () => new TestElement(),
  querySelectorAll: () => [],
};
globalThis.addEventListener = () => {};

const { el } = await import("./clipshelf/static/clipshelf/core.js");
let clicks = 0;
const button = el("button", { type: "button", disabled: true, title: "Retry", onclick: () => { clicks++; } });
button.dispatchEvent(new Event("click"));
assert.equal(clicks, 1);
assert.equal(button.attributes.has("onclick"), false);
assert.equal(button.attributes.get("type"), "button");
assert.equal(button.attributes.get("disabled"), "");
assert.equal(button.attributes.get("title"), "Retry");
