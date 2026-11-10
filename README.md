# Video Summariser

A personal tool for getting through videos and articles faster. It summarises them, cuts the padding, and tells you when something only repeats ideas you've already seen.

## What it does

- **Is it worth my time?** Compares a video or article's specific ideas with everything you've marked as seen.
- **Shorten it.** Keeps the useful parts of a video or article at three lengths, and always keeps a short sign-off.
- **Watch and read.** A local player jumps over cuts, and a reader shows shortened articles and can read them aloud.
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
uv run summariser
```

Then open http://127.0.0.1:8765 and paste links, one per line.

To make a video file with the timer and progress bars burned in:

```sh
uv run shorten <link> --ratio 0.3
```

Downloads, transcripts and edits are stored in `runs/`, which is not committed.

## Chrome extension

The `extension/` folder adds Summariser to Chrome. It needs the local site running.

- **Three-dot menu on YouTube thumbnails:** has an **Add to Summariser** item. YouTube has no supported way to extend that menu, so expect it to need fixing when YouTube changes.
- **Right-click** any video thumbnail, link or page and choose **Add to Summariser**. It's added and checked in the background, and the verdict appears in the library.
- **Toolbar button:** adds the page you're looking at, as you see it, which helps with sites you're logged in to. It also links to your library.
- **On a YouTube video:** one line shows whether it's added. A shortened video can also play in YouTube's own player by jumping over the cuts.

To install it, open `chrome://extensions`, turn on **Developer mode**, choose **Load unpacked** and pick the `extension` folder. After changing its files, press the reload arrow on its card there.

## Develop

```sh
uv run pytest
uv run ruff check .
uv run ruff format .
uv run mypy .
```
