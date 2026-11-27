<h1 align="center">clipshelf</h1>

<p align="center">Share a link from your phone. Your server fetches the post. A vision model files it in your library.</p>

<p align="center">
  <a href="#install">Install</a> &ensp;·&ensp;
  <a href="#quick-start">Quick start</a> &ensp;·&ensp;
  <a href="docs/how-it-works.md">Documentation</a> &ensp;·&ensp;
  <a href="https://github.com/Skare69/clipshelf/releases">Releases</a>
</p>

<p align="center">
  <a href="https://github.com/Skare69/clipshelf/actions/workflows/ci.yml"><img src="https://github.com/Skare69/clipshelf/actions/workflows/ci.yml/badge.svg" alt="tests"></a>
</p>

<p align="center">
  <a href="https://github.com/ottr-ai/ottr"><img src="https://raw.githubusercontent.com/ottr-ai/ottr/main/.github/assets/built-with-ottr.png" alt="Built with Ottr"></a>
</p>

Share a link from your phone and your own server fetches what is actually in the post: TikTok photo carousels slide by slide with their audio, video with its ASR captions or a local Whisper transcript, pages as text. A vision model that you host turns that into typed link and prompt entries (a repo is a link) with a summary and a category, not only tags. The Android app queues shares while offline and delivers them when it can. Self-hosted and account-based. A local model needs no third-party model service when optional screening is off. The worker still re-verifies github.com links that occur in captured pages (see [docs/privacy.md](docs/privacy.md)).

![clipshelf](screenshot.jpg)

## What you can do

- Share a link from the Android share sheet, even offline. The app queues it and delivers it later.
- Paste a URL into the web **Add** dialog from any browser.
- Follow a TikTok creator profile with a topic tag. The worker checks each followed profile on a schedule and captures its new posts by itself. Press **Suggest tags** to get topic tags from the configured model for the profile, and click one to use it.
- Keep the whole post: page text, TikTok video with its ASR captions, photo carousels slide by slide with their audio, and the original source for viewing.
- Get the spoken words of a TikTok video that has no captions. The worker transcribes its audio locally with Whisper (model `small`, inside the image). The audio does not leave your server. Press **Retry** on an older item to transcribe it.
- Get a summary and a category for every entry from a vision model that you host.
- Browse the Library, search it, and group entries into collections.
- Run one container on the NAS, with the SQLite database and the retained media on one data volume.

Data lives in one directory (`CLIPSHELF_DATA_DIR`): `db.sqlite3` plus `assets/`
(retained pages, images, video). The worker fetches sources, calls the model
endpoint you configure, and re-verifies up to 20 github.com or gist.github.com
URLs found in each captured page (see [docs/privacy.md](docs/privacy.md)).
Optional TypeSafe screening also sends capture material and findings to its
service.

## Install

### Deploy on the NAS

```
cp deploy/clipshelf.env.example .env      # fill in image, UID/GID, data dir, port
docker compose up -d
```

The container migrates itself on start. Open the web UI and complete the setup wizard. The full deploy steps are in [docs/install.md](docs/install.md#deploy-on-the-nas).

To run it locally for development, see [docs/development.md](docs/development.md#run-it-locally).

## Quick start

1. Open the URL and complete the setup wizard. The wizard creates the first administrator.
2. Install the APK from a [release](https://github.com/Skare69/clipshelf/releases), or sign in from a browser.
3. Share a link from the phone, or paste a URL into the web **Add** dialog. Each capture returns a receipt, and a retry with the same request id never duplicates it.
4. The worker fetches the post. The model writes the summary and the category, and the entry appears in the Library.
5. Optional: paste the screening key in **Admin → Screening** and press **Test**. Without a key, nothing changes.

## Learn more

- [docs/how-it-works.md](docs/how-it-works.md) covers the architecture, the capture loop, the job and outbox life cycles, and browser exports.
- [docs/install.md](docs/install.md) covers first start, NAS deploy, APK versions, and API compatibility between releases.
- [docs/configuration.md](docs/configuration.md) covers every setting and the hardening defaults.
- [docs/operations.md](docs/operations.md) covers staging retention and cleanup, backup and restore.
- [docs/privacy.md](docs/privacy.md) covers accounts, invitations, collections, and privacy defaults.
- [docs/development.md](docs/development.md) covers the local development setup, the repo layout, the tests, mutation testing, and dependency and build pinning.
- `research/` covers the plan and the research behind it.

## Contribute

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md)
for the setup and the checks, [ACCESSIBILITY.md](ACCESSIBILITY.md) for
accessibility, and [SECURITY.md](SECURITY.md) for security reports.

## License

MIT, see [LICENSE](LICENSE). The bundled third-party notices and the license gate are in [docs/development.md](docs/development.md#licenses).
