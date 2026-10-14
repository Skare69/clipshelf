/* test_inbox_status.mjs — Inbox tracking line and status sentence per job state.
   Run from the repository root:
       node test_inbox_status.mjs
   Imports the real panels.js against a minimal DOM stub and checks that every
   server job state maps to the right route position and plain status: a held
   or stopped job never reads as shelved, and an active job never as stopped.
   Uses node: builtins only. */
import assert from "node:assert/strict";

class Stub extends EventTarget {
  dataset = {}; children = []; hidden = false; value = "";
  setAttribute() {} append() {} replaceChildren() {} closest() { return null; }
}
globalThis.localStorage = { getItem: () => null, setItem() {} };
globalThis.document = {
  cookie: "", createElement: () => new Stub(),
  querySelector: () => new Stub(), querySelectorAll: () => [],
};
globalThis.addEventListener = () => {};

const { progress, statusOf } = await import("./clipshelf/static/clipshelf/panels.js");

const job = over => ({ state: "queued", acquisition: "pending", interpretation: "pending",
  active: true, guardrail: false, terminal: false, attempts: 0, error: "", ...over });
const cases = [
  // [label, job, {done, now, stop}, [kind, sentence]]
  ["queued", job({}), { done: 0, now: 1, stop: -1 }, ["", "Waiting to fetch"]],
  ["fetching", job({ state: "running" }), { done: 0, now: 1, stop: -1 }, ["", "Fetching the page"]],
  ["reading", job({ state: "running", acquisition: "complete" }), { done: 1, now: 2, stop: -1 }, ["", "Reading with the model"]],
  ["retry", job({ state: "retry", acquisition: "error", attempts: 2, error: "HTTP 503" }),
    { done: 0, now: 1, stop: -1 }, ["warn", "Retrying soon after 2 failed attempts", "HTTP 503"]],
  // requeue keeps the old error and attempts; defer sets an error with none.
  // Only a retry state means failed attempts: queued and running jobs keep
  // their own lines, and a queued job shows the old error as the note.
  ["requeued with old error", job({ attempts: 5, error: "HTTP 503" }),
    { done: 0, now: 1, stop: -1 }, ["", "Waiting to fetch", "HTTP 503"]],
  ["deferred with no attempts", job({ error: "rate limited" }),
    { done: 0, now: 1, stop: -1 }, ["", "Waiting to fetch", "rate limited"]],
  ["running with old error", job({ state: "running", error: "x" }),
    { done: 0, now: 1, stop: -1 }, ["", "Fetching the page", ""]],
  ["shelved", job({ state: "done", acquisition: "complete", interpretation: "complete", active: false }),
    { done: 3, now: -1, stop: -1 }, ["ok", "Shelved"]],
  ["shelved partial", job({ state: "done", acquisition: "partial", interpretation: "complete", active: false }),
    { done: 3, now: -1, stop: -1 }, ["warn", "Shelved, with notes"]],
  ["blocked at fetch", job({ state: "blocked", acquisition: "blocked", active: false, error: "robots.txt" }),
    { done: 0, now: -1, stop: 1 }, ["stop", "Stopped at Fetch", "robots.txt"]],
  ["guardrail", job({ state: "blocked", acquisition: "complete", interpretation: "blocked", active: false, guardrail: true }),
    { done: 1, now: -1, stop: 2 }, ["stop", "Held by screening"]],
  ["terminal host", job({ state: "done", acquisition: "complete", interpretation: "blocked",
      active: false, terminal: true,
      warnings: ["terminal host: retained as metadata only, never interpreted"] }),
    { done: 3, now: -1, stop: -1 }, ["warn", "Shelved as a link: this site is not read"]],
  ["findings withheld, non-terminal", job({ state: "done", acquisition: "complete",
      interpretation: "blocked", active: false, error: "withheld: danger 2.5" }),
    { done: 1, now: -1, stop: 2 }, ["stop", "Read, but the findings were withheld", "withheld: danger 2.5"]],
];
for (const [label, j, route, [kind, sentence, note = ""]] of cases) {
  assert.deepEqual(progress(j), route, `${label}: route position`);
  const [k, text, why] = statusOf(j);
  assert.equal(k, kind, `${label}: status kind`);
  assert.equal(text, sentence, `${label}: status sentence`);
  assert.equal(why ?? "", note, `${label}: status note`);
}
console.log(`ok: ${cases.length} job states map to the right route and status`);
