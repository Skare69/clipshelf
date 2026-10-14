/* test_inbox_retry_poll.mjs — Inbox polling must restart after "Try again".
   Run from the repository root:
       node test_inbox_retry_poll.mjs
   Imports the real panels.js against a stub DOM. The capture poller stops
   itself 3 s after load when nothing is active; when the user then retries a
   blocked job, the one-shot reload must start polling again, or the tracking
   line freezes on "Waiting to fetch". Passes only when at least two
   /api/captures requests happen in the 7 s after the click. Real timers:
   the poll interval is production code, not a knob. Uses node: builtins only. */
import assert from "node:assert/strict";

class Stub extends EventTarget {
  dataset = {}; children = []; hidden = false; value = ""; textContent = ""; className = "";
  attributes = new Map();
  classList = { toggle() {}, add() {}, remove() {} };
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
  focus() {}
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
await import("./clipshelf/static/clipshelf/panels.js");  // registers the inbox enter hook

// One capture with one blocked job. After the retry the server requeues it,
// so the job goes active and the poller has real work to do.
let retried = false, clicked = false, polls = 0, postClickPolls = 0;
const json = body => ({ ok: true, status: 200, text: async () => JSON.stringify(body) });
const job = () => ({
  id: 7, state: retried ? "queued" : "blocked",
  acquisition: retried ? "pending" : "blocked", interpretation: "pending",
  active: retried, needs_attention: !retried, can_retry: !retried,
  attempts: 1, error: "robots.txt",
});
globalThis.fetch = async (path, opts = {}) => {
  if (path === "/api/captures?collection_id=1") {
    polls++;
    if (clicked) postClickPolls++;
    return json({ captures: [{ id: 1, jobs: [job()] }] });
  }
  if (path === "/api/jobs/7/retry" && opts.method === "POST") {
    assert.equal(retried, false, "the retry fires once");
    retried = true;
    return json({});
  }
  throw new Error("unexpected request: " + path);
};

S.me = { user: { email: "keeper@example.com" }, collections: [{ id: 1, name: "Main" }] };
S.col = 1;
setView("inbox");
await new Promise(r => setTimeout(r, 300));            // first load renders the blocked job
const find = (n, fk) => n.dataset?.fk === fk ? n : (n.children || []).flatMap(c => find(c, fk) || [])[0] || null;
const button = find(out, "retry:7");
assert.ok(button, "the blocked job renders a Try again button");

await new Promise(r => setTimeout(r, 3200));           // the poller's first tick: nothing active, it stops
const idle = polls;
await new Promise(r => setTimeout(r, 500));
assert.equal(polls, idle, "polling is idle before the click");  // guards the premise

clicked = true;
button.dispatchEvent(new Event("click"));
await new Promise(r => setTimeout(r, 7000));
assert.ok(retried, "the retry request went through");
assert.ok(postClickPolls >= 2,
  `after Try again: ${postClickPolls} /api/captures requests in the 7 s after the click (need 2: ` +
  "the one-shot reload plus the restarted poller)");
console.log(`ok: ${postClickPolls} /api/captures polls in the 7 s after Try again`);
process.exit(0);  // the restarted poller keeps a timer alive; end the test here
