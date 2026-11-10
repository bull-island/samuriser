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

## Develop

```sh
uv run pytest
uv run ruff check .
uv run ruff format .
uv run mypy .
```
