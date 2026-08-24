// tiktok-extract.js — paste into DevTools console on any open www.tiktok.com
// tab. A panel opens bottom-right: paste your links (one per line, vm.tiktok
// shortlinks fine), click "extract".
//
// Engine: navigates a worker popup to each link — the browser resolves
// shortlink redirects natively and the page hydrates with your session, so
// no fetch/CORS/bot-detection issues. Allow pop-ups for tiktok.com.
//
// Downloads tiktok.json (descriptions + image/video bytes embedded as base64;
// media that refuses byte-reads keeps its URL and the CLI fetches it).
// Feed it to the tool:  python clipshelf.py tiktok.json

// best-quality-first, deduped play-mirror URLs; [] for photo posts.
// photo post = imagePost in the hydrate JSON, or a multi-slide DOM scrape
// (imgUrls.length is NOT a signal: the cover push makes it 1 on videos too)
function videoMirrors(item, multiSlide) {
  if (item?.imagePost || multiSlide) return [];
  const urls = (x) =>
    typeof x === "string"
      ? [x]
      : (x?.UrlList ?? x?.urlList ?? []).filter((u) => typeof u === "string");
  const ranked = (item?.video?.bitrateInfo ?? [])
    .map((b) => ({ bits: +b?.Bitrate || 0, urls: urls(b?.PlayAddr) }))
    .sort((a, b) => b.bits - a.bits)
    .flatMap((r) => r.urls);
  return [...new Set([...ranked, ...urls(item?.video?.playAddr), ...urls(item?.video?.downloadAddr)])];
}

async function tiktokExtract(urls, onProgress) {
  const toB64 = async (blob) => {
    const buf = new Uint8Array(await blob.arrayBuffer());
    let s = "";
    for (let i = 0; i < buf.length; i += 0x8000)
      s += String.fromCharCode(...buf.subarray(i, i + 0x8000));
    return btoa(s);
  };
  const worker = window.open("about:blank", "cs_worker", "width=1000,height=800");
  if (!worker) {
    alert("Pop-up blocked — allow pop-ups for tiktok.com and click extract again.");
    return [];
  }
  const out = [];
  let i = 0;
  for (const url of urls) {
    onProgress?.(++i, urls.length, url);
    if (!/tiktok\.com/.test(url)) {
      out.push({ url, passthrough: true }); // clipshelf ingests it directly
      continue;
    }
    try {
      worker.location.href = url;
      // poll for hydration JSON instead of a fixed sleep (<= 12 s per post)
      let item = null, doc = null;
      for (let t = 0; t < 48 && !item; t++) {
        await new Promise((r) => setTimeout(r, 250));
        try {
          doc = worker.document;
          const s = doc.querySelector("#__UNIVERSAL_DATA_FOR_REHYDRATION__");
          if (s) {
            const scope = JSON.parse(s.textContent).__DEFAULT_SCOPE__ ?? {};
            item =
              scope["webapp.video-detail"]?.itemInfo?.itemStruct ??
              scope["webapp.reflow.video.detail"]?.itemInfo?.itemStruct ??
              null;
          }
        } catch {} // cross-origin mid-redirect -> keep polling
      }
      // avatars/thumbnails carry -avt- or cropcenter:100:100; slides don't
      const junk = (s) =>
        !s ||
        !/tiktokcdn|tos-/.test(s) ||
        s.includes("-avt-") ||
        s.includes("cropcenter:100:100");
      // ponytail: TikTok renames these internals now and then; if fields come
      // back empty, dump the rehydration JSON and adjust the paths below.
      const imgUrls =
        item?.imagePost?.images
          ?.map(
            (im) =>
              im.imageURL?.urlList?.[0] ??
              im.displayImage?.urlList?.[0] ??
              im.ownerWatermarkImage?.urlList?.[0]
          )
          .filter(Boolean) ?? [];
      if (!imgUrls.length && item?.video?.cover) imgUrls.push(item.video.cover);
      if (!imgUrls.length && doc) {
        // slides live in the photo-detail swiper, never scrape the whole page
        imgUrls.push(
          ...new Set(
            [...doc.querySelectorAll(
              '[data-e2e="detail-photo"] img, .swiper-slide img'
            )].map((el) => el.src).filter((s) => !junk(s))
          )
        );
        const og = doc.querySelector('meta[property="og:image"]')?.content;
        if (!imgUrls.length && og && !junk(og)) imgUrls.push(og);
      }
      const images = [];
      for (const iu of imgUrls) {
        try {
          const r = await fetch(iu);
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          images.push({ url: iu, b64: await toB64(await r.blob()) });
        } catch {
          images.push({ url: iu }); // CLI fetches by URL at ingest time
        }
      }
      // video posts: grab the play stream too (same session, no bot-detection).
      // try each mirror: CDNs answer 403/error pages as 200s, so verify ftyp
      const mirrors = videoMirrors(item, imgUrls.length > 1);
      let video = null;
      for (const vu of mirrors) {
        try {
          // video CDN is cross-origin: without credentials cookies are
          // omitted and it answers 403 (fetch does NOT throw on that)
          const r = await fetch(vu, { credentials: "include" });
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          const blob = await r.blob();
          const head = new Uint8Array(await blob.slice(0, 16).arrayBuffer());
          if (head.length < 8 || String.fromCharCode(head[4], head[5], head[6], head[7]) !== "ftyp")
            throw new Error("not mp4 (CDN error body)");
          // ponytail: 80MB base64 ceiling keeps tiktok.json usable
          video = { urls: mirrors };
          if (blob.size < 80e6) video.b64 = await toB64(blob);
          break;
        } catch (e) {
          console.warn("video mirror failed:", e.message, vu.slice(0, 60));
        }
      }
      if (!video && mirrors.length)
        video = { url: mirrors[0], urls: mirrors }; // CLI tries URLs at ingest
      let resolved = url;
      try { resolved = worker.location.href; } catch {}
      out.push({
        url,
        resolvedUrl: resolved,
        author: item?.author?.uniqueId ?? "",
        desc:
          item?.desc ??
          doc?.querySelector('h1[data-e2e="browse-video-desc"]')?.innerText ??
          doc?.querySelector('meta[name="description"]')?.content ??
          "",
        images,
        ...(video && { video }),
      });
      console.log("ok", url, `${images.length} images${video?.b64 ? ", video" : ""}`);
    } catch (e) {
      out.push({ url, error: String(e) });
      console.warn("fail", url, e);
    }
    await new Promise((r) => setTimeout(r, 500)); // courtesy gap
  }
  worker.close();
  // browsers never expose the real save path; a timestamped name makes the
  // file findable and the clipshelf UI resolves bare names against Downloads
  const name = `tiktok-${new Date().toISOString().slice(0, 19).replace(/[T:]/g, "-")}.json`;
  const a = document.createElement("a");
  a.href = URL.createObjectURL(
    new Blob([JSON.stringify(out, null, 1)], { type: "application/json" })
  );
  a.download = name;
  a.click();
  tiktokExtract.lastFile = name;
  return out;
}

// auto-start: link-list panel (tiktokExtract stays callable manually)
if (typeof document !== "undefined") (() => {
  const box = document.createElement("div");
  box.style.cssText =
    "position:fixed;right:16px;bottom:16px;z-index:999999;width:420px;" +
    "background:#111;color:#eee;border:1px solid #444;border-radius:10px;" +
    "padding:14px;font:13px/1.5 monospace;box-shadow:0 8px 30px rgba(0,0,0,.5)";
  box.innerHTML =
    "<b>clipshelf tiktok export</b>" +
    '<textarea id="cs-urls" rows="8" placeholder="one tiktok link per line" ' +
    'style="display:block;width:100%;margin:8px 0;background:#1a1a1a;color:#eee;' +
    'border:1px solid #444;border-radius:6px;padding:6px;font:inherit;box-sizing:border-box">' +
    "</textarea>" +
    '<button id="cs-go" style="background:#2a6df4;color:#fff;border:0;' +
    'border-radius:6px;padding:6px 14px;font:inherit;cursor:pointer">extract</button> ' +
    '<span id="cs-status"></span>' +
    '<div id="cs-file" style="display:none;margin-top:8px;gap:6px">' +
    '<input id="cs-name" readonly style="flex:1;background:#1a1a1a;color:#eee;' +
    'border:1px solid #444;border-radius:6px;padding:6px;font:inherit">' +
    '<button id="cs-copy" style="background:#333;color:#fff;border:1px solid #555;' +
    'border-radius:6px;padding:6px 10px;font:inherit;cursor:pointer">copy</button></div>';
  document.body.append(box);
  const status = (t) => (box.querySelector("#cs-status").textContent = t);
  box.querySelector("#cs-go").onclick = async () => {
    const urls =
      box.querySelector("#cs-urls").value.match(/https?:\/\/\S+/g) || [];
    if (!urls.length) return status("no links found");
    box.querySelector("#cs-go").disabled = true;
    const out = await tiktokExtract(urls, (i, n, u) =>
      status(`${i}/${n} ${u.slice(0, 44)}`)
    );
    const ok = out.filter((o) => !o.error).length;
    status(`done: ${ok} ok, ${out.length - ok} failed. saved by your browser as:`);
    const f = box.querySelector("#cs-file");
    box.querySelector("#cs-name").value = tiktokExtract.lastFile;
    f.style.display = "flex";
    box.querySelector("#cs-copy").onclick = async () => {
      await navigator.clipboard.writeText(tiktokExtract.lastFile);
      box.querySelector("#cs-copy").textContent = "copied";
      setTimeout(() => (box.querySelector("#cs-copy").textContent = "copy"), 1200);
    };
    box.querySelector("#cs-go").disabled = false;
  };
})();

// self-test: node tiktok-extract.js   (pure helper, no DOM needed)
if (typeof document === "undefined" && typeof module !== "undefined") {
  const assert = require("assert");
  const vid = {
    video: {
      playAddr: "http://a/low.mp4",
      bitrateInfo: [
        { Bitrate: 100, PlayAddr: { UrlList: ["http://a/low.mp4"] } },
        { Bitrate: 900, PlayAddr: { UrlList: ["http://a/high.mp4", "http://b/high.mp4"] } },
      ],
    },
  };
  assert.deepStrictEqual(videoMirrors(vid, false), [
    "http://a/high.mp4",
    "http://b/high.mp4",
    "http://a/low.mp4",
  ]);
  assert.deepStrictEqual(videoMirrors({ imagePost: {} }, false), []);
  assert.deepStrictEqual(videoMirrors(vid, true), []);
  assert.deepStrictEqual(videoMirrors(null, false), []);
  console.log("tiktok-extract self-test ok");
}
