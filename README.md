# clipshelf

[![tests](https://github.com/Skare69/clipshelf/actions/workflows/test.yml/badge.svg)](https://github.com/Skare69/clipshelf/actions/workflows/test.yml)

Turn the links you stumble onto into a curated local library — GitHub repos, prompts, and guides — interpreted by your own AI agent.

![clipshelf](screenshot.jpg)

Everything runs on your machine: a localhost web UI, a JSON library on disk, and a CLI agent that reads the raw material (TikTok photo slides, cached web pages, video frames) and writes structured findings back in. No accounts, no cloud, no tracking.

## Quickstart

```
git clone https://github.com/Skare69/clipshelf
cd clipshelf
python clipshelf.py serve
```

Open <http://127.0.0.1:8765>. Python 3, stdlib only — nothing to `pip install`.

## The loop

1. **Import** — paste links into the UI, or feed it exports:
   - *TikTok*: paste `tiktok-extract.js` into the DevTools console on tiktok.com, paste your links (vm.tiktok shortlinks fine), let it download the export, then `python clipshelf.py tiktok.json`. Captions, photo slides, and video bytes come with it.
   - *Anything else*: paste text into the UI box; URLs are extracted, deduped, and scanned.
2. **Interpret** — press **start interpretation** in the Inbox banner (or run `python clipshelf.py pending` and process it yourself). Each pending entry is handed to your agent CLI, which reads the images / videos / pages and writes a findings JSON of repos, verbatim prompts, categories, and install commands. The protocol is one skill file: `.claude/skills/clipshelf-interpret/SKILL.md` — any CLI agent works if it can read a file and write one.
   - The built-in default command is the author's local agent (`omp -p --model llama.cpp/qwen3.8-27b`); set your own under the gear icon (Settings).
3. **Browse** — the **Library** view groups results by category (Coding agents, Image generation, Prompts, …) with star counts, card/list and Newest/A-Z toggles, one-click install, and source links. The **Inbox** view keeps every original link with its cached material.

## Layout

| file | what |
|---|---|
| `clipshelf.py` | launcher |
| `clipshelf/lib.py` | library model: triage, scan, ingest, merge, render |
| `clipshelf/server.py` | localhost server: page, intake, installs, interpret pool |
| `clipshelf/pool.py` | server-side worker pool for interpretation runs |
| `clipshelf/template.html` | the web UI (Material 3, no build step) |
| `tiktok-extract.js` | browser script: TikTok export with media bytes |
| `test_clipshelf.py` | stdlib-only test suite — `python test_clipshelf.py` |

Data lives in `library.json` (source of truth) + `cache/` (raw pages, images, videos) + `library.html` (the rendered UI). All three are gitignored — the library is yours, not the repo's. A fresh clone starts empty and renders on first visit.

## Privacy

- The server binds to `127.0.0.1` only and rejects cross-origin POSTs and unexpected `Host` headers, so a random web page cannot drive it.
- The library never leaves disk. The only outbound calls are the scan fetching page titles and your agent doing its job.
- Install commands run locally in the folder you pick, with a 10-minute cap and live output.

## License

MIT — see [LICENSE](LICENSE).
