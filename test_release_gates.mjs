/* test_release_gates.mjs — release-publishing jobs are gated on wait-ci.
   Run from the repository root:
       node test_release_gates.mjs
   Pins the v0.11.0 release-review finding: the signed release APK build
   (android-release.yml `signed-apk`) must declare `needs` that include
   `wait-ci`, so a v* tag on a red or still-running commit never builds an
   APK with the production key, and the artifact exists only for the gated
   build that `attach-apk` consumes. Reads the YAML as lines (no YAML
   dependency): every job is a two-space-indented key, and a job's `needs:`
   sits inside its block. Uses node: builtins only. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

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
