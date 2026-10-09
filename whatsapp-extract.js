// whatsapp-extract.js — paste into the DevTools console of a web.whatsapp.com
// tab with the chat open (your "message yourself" chat, or any other). To
// re-run later, save it once as a DevTools snippet (Sources → Snippets) and
// run it with Ctrl+Enter.
//
// Engine: WhatsApp Web's own message store (window.require), not the DOM:
// full text, exact timestamps and media keys, no scrolling or "Read more"
// scraping. It loads the whole chat: local history first, then it clicks
// WhatsApp's "get older messages from your phone" button until the chat
// start (keep the phone online). Images download decrypted; expired ones are
// re-requested from the phone, as a click on them in the UI does.
//
// Downloads whatsapp-<time>.json in the clipshelf library.json import shape:
//   links    every URL in a message (the server fetches the page), and every
//            image as base64 under https://web.whatsapp.com/?msg=<id>
//   prompts  text with no URL and no image (clipshelf keeps URL-less text
//            only as prompts)
// Every entry has `found`: the message time (ISO 8601, UTC).
// Import: clipshelf web UI → Import → choose the file.
//
// Incremental: exported message ids stay in this browser's localStorage, so
// the next run exports only messages it has not exported yet. Logging out of
// WhatsApp Web clears that list. To export everything again, run
//   localStorage.removeItem("clipshelf-wa-seen")
// Messages count as exported only after you confirm that the browser saved
// the file: Chrome holds a tab's second automatic download until you allow
// it, and a Save dialog can be cancelled. WhatsApp changes its internals
// without notice: a TypeError here usually means a renamed module.

const URL_RE = /https?:\/\/[^\s<>"'`]+/gi; // clipshelf services.URL_PATTERN
const SEEN_KEY = "clipshelf-wa-seen";
// ponytail: one run stays below the default 32 MiB import ceiling
// (CLIPSHELF_MAX_IMPORT_BYTES); the rest waits for the next run. Lower this
// if your server's ceiling is smaller.
const FILE_BUDGET = 30e6;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const iso = (t) => new Date(t * 1000).toISOString();

// clipshelf services._extract_urls: strip trailing punctuation, keep
// balanced closing parentheses
function cleanUrl(match) {
  let url = match.replace(/[.,;:!?\]}"']+$/, "");
  const trailing = url.length - url.replace(/\)+$/, "").length;
  if (trailing) {
    let balance = 0;
    for (const ch of url.slice(0, -trailing))
      if (ch === "(") balance++;
      else if (ch === ")" && balance) balance--;
    if (trailing > balance) url = url.slice(0, balance - trailing);
  }
  return url;
}

// one message -> [section, key, entry] rows of the library.json import
function entriesFor({ id, t, text = "", title = "", imageB64 = null }) {
  const found = iso(t);
  const urls = [...new Set((text.match(URL_RE) || []).map(cleanUrl))];
  const desc = text.replace(URL_RE, "").trim() ? text : undefined;
  const rows = urls.map((url) => [
    "links", url, { title: (urls.length === 1 && title) || undefined, desc, found },
  ]);
  if (imageB64)
    rows.push(["links", `https://web.whatsapp.com/?msg=${id}`,
      { desc: text || undefined, found, images: [{ b64: imageB64 }] }]);
  else if (!urls.length && text.trim()) rows.push(["prompts", id, { text, found }]);
  return rows;
}

// the button WhatsApp renders above the first message while older history
// stays on the phone; found by position, not by its localized text
async function olderMessagesButton() {
  for (let i = 0; i < 6; i++) {
    const panel = document.querySelector('[data-testid="conversation-panel-messages"]');
    if (!panel) return null;
    panel.scrollTop = 0;
    await sleep(700);
    const first = panel.querySelector("[data-id]");
    const button = first && [...panel.querySelectorAll("button")].find(
      (b) => b.compareDocumentPosition(first) & Node.DOCUMENT_POSITION_FOLLOWING);
    if (button) return button;
  }
  return null;
}

// local history first, then the phone's history until the chat start;
// false = stopped early (phone offline or no button)
async function loadWholeChat(chat) {
  const { loadEarlierMsgs } = window.require("WAWebChatLoadMessages");
  for (;;) {
    while ((await loadEarlierMsgs({ chat }))?.length) {}
    if (chat.msgs.msgLoadState.noEarlierMsgs) return true;
    const button = await olderMessagesButton();
    if (!button) return false;
    const oldest = chat.msgs.at(0)?.t;
    button.click();
    let more = false;
    for (let s = 0; s < 30 && !more; s++) {
      await sleep(1000);
      await loadEarlierMsgs({ chat });
      more = chat.msgs.at(0)?.t < oldest;
    }
    if (!more) return false;
    console.log(`loaded ${chat.msgs.length} messages, back to ${iso(chat.msgs.at(0).t)}`);
  }
}

// decrypted bytes; a CDN 404 (media older than ~30 days) asks the phone to
// re-upload first, as a click on the image in the UI does
async function imageBytes(m) {
  const { downloadManager } = window.require("WAWebDownloadManager");
  const qpl = { addAnnotations() { return this; }, addPoint() { return this; } };
  const get = () => downloadManager.downloadAndMaybeDecrypt({
    directPath: m.directPath, encFilehash: m.encFilehash, filehash: m.filehash,
    mediaKey: m.mediaKey, mediaKeyTimestamp: m.mediaKeyTimestamp, type: m.type,
    mimetype: m.mimetype, signal: AbortSignal.timeout(60e3), downloadQpl: qpl,
  });
  try {
    return await get();
  } catch {
    await Promise.race([
      m.downloadMedia({ downloadEvenIfExpensive: true, rmrReason: 1, isUserInitiated: true }),
      sleep(60e3).then(() => { throw new Error("the phone did not re-upload it within 60 s"); }),
    ]);
    return await get();
  }
}

async function whatsappExtract() {
  if (typeof window.require !== "function")
    throw new Error("run this in the console of an open web.whatsapp.com tab");
  const chat = window.require("WAWebCollections").Chat.getModelsArray().find((c) => c.active);
  if (!chat) throw new Error("open the chat first, then run this again");
  const complete = await loadWholeChat(chat);
  if (!complete)
    console.warn("older messages stay on the phone; exporting back to " +
      `${chat.msgs.length ? iso(chat.msgs.at(0).t) : "nothing"}. Keep the phone online and run again for the rest.`);
  const seen = new Set(JSON.parse(localStorage.getItem(SEEN_KEY) || "[]"));
  const fresh = chat.msgs.getModelsArray().filter((m) => !seen.has(m.id.id));
  if (!fresh.length) return { file: null, note: "nothing new since the last export" };
  if (!confirm(`Export ${fresh.length} new message(s) from "${chat.formattedTitle}" for clipshelf?`))
    return { file: null, note: "cancelled" };

  const out = { links: {}, prompts: {} };
  const done = [], skipped = [], failed = [];
  let bytes = 0, full = false;
  for (const m of fresh) {
    if (m.type !== "chat" && m.type !== "image") {
      skipped.push(`${m.type} ${iso(m.t)}`); // reported once, then marked done
      done.push(m.id.id);
      continue;
    }
    let imageB64 = null;
    if (m.type === "image") {
      console.log(`image ${iso(m.t)}`);
      try {
        imageB64 = new Uint8Array(await imageBytes(m)).toBase64();
      } catch (e) {
        failed.push(`image ${iso(m.t)}: ${e?.message ?? e}`); // not done: retried next run
        continue;
      }
    }
    const rows = entriesFor({ id: m.id.id, t: m.t, title: m.title || "", imageB64,
      text: (m.type === "image" ? m.caption : m.body) || "" });
    const size = new Blob([JSON.stringify(rows)]).size;
    if (bytes && bytes + size > FILE_BUDGET) { full = true; break; }
    bytes += size;
    for (const [section, key, entry] of rows) out[section][key] ??= entry;
    done.push(m.id.id);
  }

  let file = null;
  if (Object.keys(out.links).length || Object.keys(out.prompts).length) {
    file = `whatsapp-${new Date().toISOString().slice(0, 19).replace(/[T:]/g, "-")}.json`;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([JSON.stringify(out, null, 1)], { type: "application/json" }));
    a.download = file;
    a.click();
    // ponytail: the browser never reports a blocked or cancelled save, so ask
    if (!confirm(`Did your browser save ${file}? OK marks ${done.length} message(s) as ` +
        "exported. Cancel exports them again next run."))
      return { file: null, note: "not saved: nothing marked as exported" };
  }
  localStorage.setItem(SEEN_KEY, JSON.stringify([...seen, ...done]));
  const summary = {
    file, messages: done.length, links: Object.keys(out.links).length,
    prompts: Object.keys(out.prompts).length, skipped, failed,
    complete, budgetReached: full,
  };
  console.log("whatsapp-extract:", summary);
  if (full) console.warn("file budget reached: run again for the remaining messages");
  return summary;
}

if (typeof document !== "undefined")
  whatsappExtract().catch((e) => console.error("whatsapp-extract:", e));

// self-test: node whatsapp-extract.js   (pure helpers, no WhatsApp needed)
if (typeof document === "undefined" && typeof module !== "undefined") {
  const assert = require("assert");
  const rows = (msg) => JSON.parse(JSON.stringify(entriesFor({ id: "ID", t: 0, ...msg })));
  const found = "1970-01-01T00:00:00.000Z";
  assert.strictEqual(cleanUrl("https://a.example/x)."), "https://a.example/x");
  assert.strictEqual(cleanUrl("https://w.example/Foo_(bar))"), "https://w.example/Foo_(bar)");
  assert.strictEqual(cleanUrl('https://a.example/x?q=1",'), "https://a.example/x?q=1");
  // a bare link: no desc, the preview title rides along
  assert.deepStrictEqual(rows({ text: "https://vm.tiktok.com/Z1/", title: "Clip" }),
    [["links", "https://vm.tiktok.com/Z1/", { title: "Clip", found }]]);
  // text around links: kept as desc; one row per distinct URL, no title guess
  assert.deepStrictEqual(rows({ text: "see (https://a.example/x) and https://b.example https://b.example", title: "T" }), [
    ["links", "https://a.example/x", { desc: "see (https://a.example/x) and https://b.example https://b.example", found }],
    ["links", "https://b.example", { desc: "see (https://a.example/x) and https://b.example https://b.example", found }],
  ]);
  // URL-less text: a prompt keyed by message id; blank text: nothing
  assert.deepStrictEqual(rows({ text: "ASD-STE100" }), [["prompts", "ID", { text: "ASD-STE100", found }]]);
  assert.deepStrictEqual(rows({ text: "  " }), []);
  // image: own entry under the message key; a caption URL still gets fetched
  assert.deepStrictEqual(rows({ imageB64: "AAA=" }),
    [["links", "https://web.whatsapp.com/?msg=ID", { found, images: [{ b64: "AAA=" }] }]]);
  assert.deepStrictEqual(rows({ imageB64: "AAA=", text: "https://c.example" }), [
    ["links", "https://c.example", { found }],
    ["links", "https://web.whatsapp.com/?msg=ID", { desc: "https://c.example", found, images: [{ b64: "AAA=" }] }],
  ]);
  console.log("whatsapp-extract self-test ok");
}
