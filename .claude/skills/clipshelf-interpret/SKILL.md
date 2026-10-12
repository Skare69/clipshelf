---
name: clipshelf-interpret
description: Interpret collected clipshelf captures (TikTok screenshots, cached web pages, local video files) into github repos, verbatim prompts, and guide links, then merge them into the library with add_extracted. Use after ingesting a tiktok.json export, or after collecting captures the operator wants interpreted offline, or when the user says "interpret", "process pending", or "run clipshelf".
---

# clipshelf-interpret

Produce OFFLINE findings from material the operator hands you (screenshots,
URLs, local video files, page text) and merge them into the library. The server
no longer runs a per-item agent pool; the worker interprets jobs itself, so this
skill only adds findings from outside the server. Never edit `library.json` by
hand - merging and dedup are `clipshelf.py`'s job.

All paths are relative to the repo root (where `clipshelf.py` lives).

## Steps

1. Run `python clipshelf.py pending --user you@example.com` to see what the
   operator has queued. It prints plain text rows (one per pending, retrying,
   or blocked job, plus open imports and a summary) - NOT JSON. Use it for
   orientation only. Then work from the material the operator gives you for
   the items they choose.
2. For each item, gather findings from its sources:
   - **Images** (screenshots): inspect with vision, asking: "Transcribe all
     readable text. Then list: github repos mentioned (owner/name or full URL),
     verbatim prompt text if the image shows a prompt, and any other tool/guide
     URLs. Only report what is clearly readable; say 'unclear' otherwise."
   - **Local mp4 video**: use the `watch` skill on the local file path to get
     frames + transcript, then read the frames like slide images with the same
     question. Local files never touch yt-dlp/download, so tiktok
     bot-detection is not an issue. No Whisper key configured means no-caption
     videos come back frames-only; that is fine. If `watch` is unavailable,
     extract frames directly:
     `ffmpeg -i <mp4> -vf "select='gt(scene,0.3)',scale=512:-1" -vsync vfr -frames:v 20 f%02d.jpg`.
   - **Page text / cached HTML**: read it and extract prompt text, repo
     mentions, and guide links from the content.
   - **Captions**: the post's own caption text.
3. Write `findings.json` in the repo root:

   ```json
   [
     {
       "source": "<URL identifying where this finding came from>",
       "summary": "<one line: what this item was about>",
       "repos": ["owner/name or full github URL"],
       "links": [{ "url": "https://...", "title": "..." }],
       "prompts": [{ "title": "...", "text": "<verbatim prompt text>", "cat": "<category>" }],
       "categories": { "<repo as listed in repos>": "<category>" },
       "installs": { "<repo as listed in repos>": "<one shell command>" }
     }
   ]
   ```

   Rules:
   - `source` is optional. When set, it must be the captured item's URL: the
     merge normalizes it (`lib.norm` drops fragments, tracking params, and the
     trailing slash) and matches it to an existing entry in the operator's
     Personal collection, attaching your summary and source link to that entry.
     Nothing matches when the entry does not exist or the user removed it - a
     merge never resurrects a removed entry; the finding's repos, links, and
     prompts are still merged as new entries.
   - Repos only when clearly readable. A wrong repo name pollutes the library
     permanently. Unsure: leave it out and mention it in the report instead.
   - Prompts verbatim, never paraphrased. Give each prompt a `cat` too, by
     what the prompt is *for* (e.g. "Image generation", "Coding agents",
     "Writing & marketing") - same reuse-before-invent rule as repos.
   - Categorize every repo you report. Reuse existing categories before
     inventing new ones: `python -c "import json;print(sorted({e.get('cat')
     for e in json.load(open('library.json',encoding='utf-8'))['links'].values()
     if e.get('cat')}))"`. Categories are specific domains ("Video generation",
     "Claude Code ecosystem"), not "AI".
   - For each repo that is an installable tool, read its README (fetch
     `https://raw.githubusercontent.com/<owner>/<name>/HEAD/README.md`) and put
     the official install one-liner in `installs` (e.g. `pip install x`,
     `npm i -g x`, `npx skills add owner/name`, `git clone <url>`). It must be a
     single non-interactive command that works on Windows. The library UI runs it
     verbatim on an "install" click - no `curl | sh`, no multi-step scripts.
     Curated lists, datasets, or repos without a clear install command: omit.
   - Omit empty fields. Include an entry for every item you interpreted, even
     if it only has a summary.
4. Merge: `python clipshelf.py add_extracted --user you@example.com findings.json`.
   This validates the schema, merges findings into the user's Personal
   collection, and prints merge stats.
5. Report counts (items interpreted, repos/prompts/links added) and anything
   skipped as unreadable. Delete `findings.json`.
