# Samuriser

Cuts videos and articles down to what matters, like a samurai.

A personal tool for getting through videos and articles faster. It summarises them, cuts the padding, and tells you when something only repeats ideas you've already seen.

## What it does

- **Is it worth my time?** Compares a video or article's specific ideas with everything you've marked as seen.
- **How padded is it?** Each item gets a rough estimate of how much teaches nothing, shown as a badge. Padded items are cut harder than dense ones.
- **Shorten it.** Keeps the useful parts of a video or article at three lengths. Every length opens with one line saying what it's about and ends with a short sign-off.
- **No download for YouTube.** A YouTube video is shortened from its captions alone. It plays shortened in the local player through YouTube's embedded player, or on YouTube through the extension. Videos from other sites are downloaded, because there is nowhere else to play them.
- **Watch and read.** A local player jumps over cuts in videos, and a reader shows shortened articles and can read them aloud.
- **Library.** Thumbnails, full-text search, and a history across platforms.

## Requirements

- macOS on Apple Silicon, for `mlx-whisper`
- [uv](https://docs.astral.sh/uv/) and [bun](https://bun.sh)
- `ffmpeg`
- Either a signed-in [Claude Code](https://claude.com/claude-code) or `ANTHROPIC_API_KEY`
- Optional: [Kokoro](https://github.com/remsky/Kokoro-FastAPI) on `127.0.0.1:8880` for natural reading voices. Without it, the reader uses the browser's voices.

## Run it

```sh
uv sync
uv run samuriser
```

Then open http://127.0.0.1:8765 and paste links, one per line.

To download a video and make a file with the timer and progress bars burned in:

```sh
uv run shorten <link> --ratio 0.3
```

Downloads, transcripts and edits are stored in `runs/`, which is not committed.

## Read it on your phone

The site only runs on your Mac. To use your library when the Mac is off, it can publish a read-only copy to any S3-style storage, such as Cloudflare R2. The copy holds summaries, cut lists and thumbnails. It never holds video files: YouTube videos stream from YouTube, and downloaded videos stay on the Mac.

The copy has no login. Anyone with its address can read it, so use an address that is hard to guess, such as R2's public development URL, and don't share it.

Put these in a `.env` file in this folder:

```sh
SAMURISER_PUBLISH_BUCKET=<bucket name>
SAMURISER_PUBLISH_ENDPOINT=https://<account id>.r2.cloudflarestorage.com
SAMURISER_PUBLISH_KEY_ID=<access key id>
SAMURISER_PUBLISH_SECRET=<secret access key>
```

An article is published as its summary and key ideas, with a link to the original. To include the full text so you can read it on your phone, add `SAMURISER_PUBLISH_ARTICLE_TEXT=1`. That puts a complete copy of someone else's writing at a public address, so only do it for your own reading and keep the address to yourself.

While the site is running it republishes after every change. To publish by hand, run `uv run publish`. Then open `<public address>/index.html` on your phone.

Search, marking things as watched and changing cuts only work on the local site.

To add videos from your phone, make an unlisted YouTube playlist and add its link to `.env`:

```sh
SAMURISER_INBOX_PLAYLIST=https://www.youtube.com/playlist?list=<id>
```

While the site is running it checks the playlist every five minutes and shortens anything new, so saving a video to that playlist in the YouTube app adds it to your library. Videos saved while the Mac is off are picked up the next time the site runs.

## Chrome extension

The `extension/` folder adds Samuriser to Chrome. It needs the local site running.

- **Three-dot menu on YouTube thumbnails:** has an **Add to Samuriser** item. YouTube has no supported way to extend that menu, so expect it to need fixing when YouTube changes.
- **Right-click** any video thumbnail, link or page and choose **Add to Samuriser**. It's added and checked in the background, and the verdict appears in the library.
- **Toolbar button:** adds the page you're looking at, as you see it, which helps with sites you're logged in to. It also links to your library.
- **On a YouTube video:** one line shows whether it's added and lets you shorten it. A shortened video can then play in YouTube's own player by jumping over the cuts.

To install it, open `chrome://extensions`, turn on **Developer mode**, choose **Load unpacked** and pick the `extension` folder. After changing its files, press the reload arrow on its card there.

## Develop

```sh
uv run pytest
uv run ruff check .
uv run ruff format .
uv run mypy .
```

## Use and licence

This is a personal tool for getting through things you would otherwise watch or read in full. It is released under the [MIT licence](LICENSE).

It reads videos and pages through [yt-dlp](https://github.com/yt-dlp/yt-dlp), which some sites' terms of service don't allow. You are responsible for how you use it: follow the terms of the sites you use it with, and don't share downloaded videos or copies of other people's work.
