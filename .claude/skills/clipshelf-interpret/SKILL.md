---
name: clipshelf-interpret
description: Interpret pending clipshelf items (TikTok screenshots, cached web pages) into github repos, verbatim prompts, and guide links, then merge them into the library. Use after ingesting a tiktok.json export, or when the user says "interpret", "process pending", or "run clipshelf".
---

# clipshelf-interpret

Turn pending library items into structured findings. Never edit `library.json` by
hand. Merging, dedup, and re-rendering are `clipshelf.py`'s job.

All paths are relative to the repo root (where `clipshelf.py` lives).

## Single-entry mode

The server's interpret pool runs one agent per pending entry. In that mode
the prompt names an entry file (`_interp/entry_<hash>.json`) and an output
file (`_interp/findings_<hash>.json`):

1. Read the entry file: `{url, desc, images, video, cache}` - the same fields
   as one `pending` item.
2. Gather findings from its sources (step 2 below), for that one entry only.
3. Write **exactly one findings entry** (the step-3 schema) as a JSON array to
   the output file.
4. Stop. Do not run `add-extracted`, do not edit `library.json`, do not delete
   files - the pool merges all findings once, after every entry finishes.


## Steps

1. Run `python clipshelf.py pending`. Output: JSON array of
   `{url, desc, images: [image paths], video: <mp4 path or null>, cache: <html path or null>}`.
   Empty array: report "nothing pending" and stop.
2. For each item, gather findings from all three sources:
   - **Each image path**: inspect with vision, asking: "Transcribe all readable
     text. Then list: github repos mentioned (owner/name or full URL), verbatim
     prompt text if the image shows a prompt, and any other tool/guide URLs.
     Only report what is clearly readable; say 'unclear' otherwise."
   - **`video` path** (local mp4 saved by the browser export): use the `watch`
     skill on the local file path to get frames + transcript, then read the
     frames like slide images with the same question. Local files never touch
     yt-dlp/download, so tiktok bot-detection is not an issue. No Whisper key
     configured means no-caption videos come back frames-only; that is fine.
     If `watch` is unavailable, extract frames directly:
     `ffmpeg -i <mp4> -vf "select='gt(scene,0.3)',scale=512:-1" -vsync vfr -frames:v 20 f%02d.jpg`.
   - **`cache` path** (raw HTML of a fetched page): read it and extract prompt
     text, repo mentions, and guide links from the page content.
   - **`desc`**: the post's own caption text.
3. Write `findings.json` in the repo root:

   ```json
   [
     {
       "source": "<item url exactly as given by pending>",
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
   - Omit empty fields, but include every pending item (even if only a summary)
     so its pending flag gets cleared.
4. Run `python clipshelf.py add-extracted findings.json`. This merges findings,
   fetches titles for newly found repos, and re-renders `library.html`.
5. Report counts (items interpreted, repos/prompts/links added) and anything
   skipped as unreadable. Delete `findings.json`.
