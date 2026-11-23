/* test_release_gates.mjs — release-publishing jobs are gated on wait-ci.
   Run from the repository root:
       node test_release_gates.mjs
   Pins the v0.11.0 release-review finding: the signed release APK build
   (android-release.yml `signed-apk`) must declare `needs` that include
   `wait-ci`, so a v* tag on a red or still-running commit never builds an
   APK with the production key, and the artifact exists only for the gated
   build that `attach-apk` consumes. Reads the YAML as lines (no YAML
   dependency): every job is a two-space-indented key, and a job's `needs:`
   sits inside its block. Uses node: builtins only.

   Also pins CLIP-108: the wait-ci gate accepts only the push run on main
   for the tagged commit. A green pull_request run with the same head sha
   tests a different merge tree and must never satisfy the gate. Runs the
   real step script extracted from wait-ci.yml against a `gh` shim that
   emulates the Actions API filters, so the URL filter, the jq selection
   and the accept/reject logic are all exercised end to end. */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// Top-level `name:` block lines and their indented bodies, in file order.
function jobBlocks(path) {
  const blocks = {};
  let current = null;
  for (const line of readFileSync(path, "utf8").split("\n")) {
    const top = line.match(/^  ([a-z][a-z0-9-]*):$/);
    if (top) {
      current = top[1];
      blocks[current] = [];
    } else if (current && /^\s/.test(line)) {
      blocks[current].push(line);
    } else if (current && line.trim()) {
      current = null;
    }
  }
  return blocks;
}

function needsOf(blocks, job, path) {
  const line = blocks[job]?.find(l => l.trim().startsWith("needs:"));
  assert.ok(line, `${path}: job "${job}" declares needs (release gate)`);
  return line;
}

const apk = ".github/workflows/android-release.yml";
const blocks = jobBlocks(apk);

// The signing build itself waits for a green ci.yml run on the tagged commit.
assert.match(
  needsOf(blocks, "signed-apk", apk), /wait-ci/,
  "signed-apk must need wait-ci: no production-key build on a red tag");

// The artifact reaches the release only through that gated build.
assert.match(
  needsOf(blocks, "attach-apk", apk), /signed-apk/,
  "attach-apk must need signed-apk: attach only the gated artifact");

console.log("ok: android-release publishing is gated on wait-ci");

// --- CLIP-108: wait-ci accepts only the push run on main ---------------------

// The `run: |` block of a step, dedented: block scalar lines sit two columns
// deeper than `run:`; a less-indented line ends the block.
function stepScript(path) {
  const lines = readFileSync(path, "utf8").split("\n");
  const at = lines.findIndex((l) => /^\s*run: \|\s*$/.test(l));
  assert.ok(at >= 0, `${path}: run block found`);
  const indent = lines[at].match(/^\s*/)[0].length + 2;
  const body = [];
  for (const line of lines.slice(at + 1)) {
    if (line.trim() === "") continue;
    if (line.length < indent || line.slice(0, indent).trim() !== "") break;
    body.push(line.slice(indent));
  }
  assert.ok(
    body.some((l) => l.includes("head_sha=")),
    `${path}: gate script extracted`);
  return body.join("\n");
}

const wait = ".github/workflows/wait-ci.yml";
const gateScript = stepScript(wait);

// The runs query must be scoped server-side: head sha, push event, main branch.
assert.match(
  gateScript, /head_sha=\$GITHUB_SHA&event=push&branch=main/,
  "wait-ci must query event=push&branch=main: a pull_request run of the " +
  "same sha tests a different merge tree");

// Emulates the Actions API side of `GET .../workflows/ci.yml/runs`: the query
// filters (head_sha, event, branch) select from the fixture list, newest
// first; the gate then applies its own jq to the first survivor.
const serveMjs = `import { readFileSync } from "node:fs";
const q = new URL("https://gh.local/" + process.argv[2]).searchParams;
const all = JSON.parse(readFileSync(process.env.FIXTURE_RUNS, "utf8")).workflow_runs;
const workflow_runs = all.filter((r) =>
  (!q.has("head_sha") || r.head_sha === q.get("head_sha")) &&
  (!q.has("event") || r.event === q.get("event")) &&
  (!q.has("branch") || r.head_branch === q.get("branch")));
process.stdout.write(JSON.stringify({ total_count: workflow_runs.length, workflow_runs }));
`;

// Only `gh api <url> --jq <filter>`; serves the fixture list, then applies the
// gate's own jq filter so the script under test runs unmodified.
const ghShim = `#!/bin/sh
url=""; filter="."
while [ $# -gt 0 ]; do
  case "$1" in
    api) url="$2"; shift 2 ;;
    --jq) filter="$2"; shift 2 ;;
    *) shift ;;
  esac
done
node "$SERVE_MJS" "$url" | jq -r "$filter"
`;

const tmp = mkdtempSync(join(tmpdir(), "wait-ci-gate-"));
const gate = join(tmp, "gate.sh");
writeFileSync(gate, gateScript, { mode: 0o755 });
const serve = join(tmp, "serve-runs.mjs");
writeFileSync(serve, serveMjs);
writeFileSync(join(tmp, "gh"), ghShim, { mode: 0o755 });

let fixtureSeq = 0;
function runGate(runs) {
  const fix = join(tmp, `runs-${++fixtureSeq}.json`);
  writeFileSync(fix, JSON.stringify({ workflow_runs: runs }));
  return spawnSync("bash", [gate], {
    timeout: 30_000, // the gate sleeps only between polls; fixtures decide at once
    env: {
      ...process.env,
      GITHUB_REPOSITORY: "acme/clipshelf",
      GITHUB_SHA: SHA,
      GH_TOKEN: "shim-token",
      PATH: `${tmp}:${process.env.PATH}`,
      SERVE_MJS: serve,
      FIXTURE_RUNS: fix,
    },
  });
}

const SHA = "4f2e91cafe";
const runAt = (id, event, branch, conclusion) => ({
  id, event, head_branch: branch, head_sha: SHA, status: "completed", conclusion,
});

// Operator fixture: newest run is the green pull_request run, the push run on
// main for the same commit is red. The gate must fail on the red push run.
const prGreen = runAt(2, "pull_request", "pr-branch", "success");
const pushRed = runAt(1, "push", "main", "failure");
const mixed = runGate([prGreen, pushRed]);
assert.notEqual(mixed.status, 0, "gate must fail: only a red push run exists");
assert.match(
  `${mixed.stdout}`, /::error::ci\.yml on .*: completed failure/,
  "gate must fail on the red push run, not the green pull_request run");

// The real target state: the push run on main for the tagged commit is green.
const pushGreen = runGate([runAt(3, "push", "main", "success")]);
assert.equal(pushGreen.status, 0, "gate must pass a green push run on main");
assert.match(`${pushGreen.stdout}`, /ok: ci\.yml green/);

console.log("ok: wait-ci accepts only the push run on main");
