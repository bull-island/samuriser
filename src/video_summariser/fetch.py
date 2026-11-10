"""Get videos, captions and details from a link or local path. Links can be from any yt-dlp site."""

import json
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urldefrag, urlparse

import yt_dlp
from PIL import Image

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}

# H.264 up to 1080p keeps on-screen text legible and decodes quickly on any Mac.
FORMAT = "bv*[height<=1080][vcodec^=avc1]+ba[ext=m4a]/b[height<=1080]/b"
# One track only: each extra language is another request towards YouTube's rate limit.
CAPTION_LANGUAGES = ["en"]
# The gap yt-dlp's own "sleep" preset leaves between caption requests.
CAPTION_GAP_SECONDS = 5
# A guess: yt-dlp's docs don't say how long a block lasts, and one try an hour costs little.
CAPTION_PAUSE = timedelta(hours=1)
CAPTION_PAUSES_FILE = "caption_pauses.json"
THUMBNAIL_FILE = "thumbnail.jpg"
# Twice the width it's shown at, so it stays sharp on high-resolution screens.
THUMBNAIL_SIZE = (480, 270)
# Platforms list sizes that a given video may not have, so try the best few.
THUMBNAIL_ATTEMPTS = 6


@dataclass(frozen=True)
class Source:
    run_dir: Path
    video: Path
    title: str
    chapters: list[dict[str, Any]]


@dataclass(frozen=True)
class Preview:
    run_dir: Path
    title: str
    captions: Path | None


def load_info(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "info.json"
    info: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}
    return info


def save_info(run_dir: Path, **fields: Any) -> None:
    """Add to a video's stored details without losing the ones already there."""
    (run_dir / "info.json").write_text(json.dumps(load_info(run_dir) | fields, indent=2))


def canonical_url(url: str) -> str:
    """Reduce a link to one form, so the same page is recognised however it was opened."""
    parsed = urlparse(url)
    video_id = None
    if parsed.hostname in YOUTUBE_HOSTS and parsed.path == "/watch":
        video_id = parse_qs(parsed.query).get("v", [None])[0]
    elif parsed.hostname == "youtu.be":
        video_id = parsed.path.strip("/") or None
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    return urldefrag(url).url


def add_link(run_dir: Path, url: str) -> None:
    """Remember a link that led to this item, for sites whose own address for it differs."""
    info = load_info(run_dir)
    links = info.get("links", [])
    if url != info.get("url") and url not in links:
        save_info(run_dir, links=[*links, url])


def set_watched(run_dir: Path, watched: bool) -> None:
    """Record whether a video has been watched, and when, for the history."""
    watched_at = datetime.now(UTC).isoformat() if watched else None
    save_info(run_dir, watched=watched, watched_at=watched_at)


def _options(**extra: Any) -> dict[str, Any]:
    options: dict[str, Any] = {"noplaylist": True, "quiet": True, "noprogress": True}
    # yt-dlp needs a JS runtime for YouTube's challenges and only enables deno by default.
    if shutil.which("bun"):
        options["js_runtimes"] = {"bun": {}}
    return options | extra


def _inspect(url: str, runs_dir: Path) -> tuple[Path, dict[str, Any]]:
    """Read a link's details and create the folder for its video."""
    with yt_dlp.YoutubeDL(_options()) as ydl:
        info = ydl.extract_info(url, download=False)
    if info is None or "entries" in info:
        raise ValueError("That link isn't a single video. Paste links to individual videos.")
    run_dir = runs_dir / re.sub(r"[^\w-]+", "_", info["id"])
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir, info


def download_page(url: str) -> bytes | None:
    """Fetch a web page, or return None if the site refuses."""
    try:
        with yt_dlp.YoutubeDL(_options()) as ydl, ydl.urlopen(url) as response:
            page: bytes = response.read()
    except (yt_dlp.utils.YoutubeDLError, OSError):
        return None
    return page


def save_thumbnail(best_first: list[str], run_dir: Path) -> None:
    """Keep a copy of the platform's own picture, because links to it can expire."""
    path = run_dir / THUMBNAIL_FILE
    if path.exists():
        return
    with yt_dlp.YoutubeDL(_options()) as ydl:
        for url in best_first[:THUMBNAIL_ATTEMPTS]:
            try:
                with ydl.urlopen(url) as response:
                    image = Image.open(BytesIO(response.read())).convert("RGB")
            except (yt_dlp.utils.YoutubeDLError, OSError):
                continue
            image.thumbnail(THUMBNAIL_SIZE)
            image.save(path, quality=85)
            return


def _store_details(run_dir: Path, url: str, info: dict[str, Any]) -> None:
    save_info(
        run_dir,
        title=info["title"],
        chapters=info.get("chapters") or [],
        url=info.get("webpage_url", url),
        duration=info.get("duration"),
        platform=info.get("webpage_url_domain"),
    )
    save_thumbnail([t["url"] for t in info.get("thumbnails") or []][::-1], run_dir)


def _caption_pauses(runs_dir: Path) -> dict[str, str]:
    path = runs_dir / CAPTION_PAUSES_FILE
    pauses: dict[str, str] = json.loads(path.read_text()) if path.exists() else {}
    return pauses


def _captions_paused(runs_dir: Path, site: str) -> bool:
    until = _caption_pauses(runs_dir).get(site)
    return until is not None and datetime.fromisoformat(until) > datetime.now(UTC)


def _pause_captions(runs_dir: Path, site: str) -> None:
    until = datetime.now(UTC) + CAPTION_PAUSE
    pauses = _caption_pauses(runs_dir) | {site: until.isoformat()}
    (runs_dir / CAPTION_PAUSES_FILE).write_text(json.dumps(pauses, indent=2))


def _saved_captions(run_dir: Path) -> Path | None:
    return next(iter(sorted(run_dir.glob("captions.*.json3"))), None)


def _download_captions(url: str, site: str, run_dir: Path, runs_dir: Path) -> Path | None:
    """Fetch captions if the site allows it. If not, the audio gets transcribed instead."""
    # A summarised video already has its text, so asking again could only risk a block.
    if (run_dir / "digest.json").exists() or _captions_paused(runs_dir, site):
        return _saved_captions(run_dir)
    options = _options(
        skip_download=True,
        writesubtitles=True,
        writeautomaticsub=True,
        subtitleslangs=CAPTION_LANGUAGES,
        subtitlesformat="json3",
        sleep_interval_subtitles=CAPTION_GAP_SECONDS,
        outtmpl=str(run_dir / "captions.%(ext)s"),
    )
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as error:
        if "HTTP Error 429" in str(error):
            _pause_captions(runs_dir, site)
    return _saved_captions(run_dir)


def preview(url: str, runs_dir: Path) -> Preview:
    """Get a video's details and captions without downloading the video."""
    run_dir, info = _inspect(url, runs_dir)
    _store_details(run_dir, url, info)
    site = info.get("webpage_url_domain") or "unknown"
    return Preview(run_dir, info["title"], _download_captions(url, site, run_dir, runs_dir))


def fetch_audio(url: str, run_dir: Path) -> Path:
    """Download only the sound, for videos with no captions."""
    existing = next(run_dir.glob("audio_source.*"), None)
    if existing is not None:
        return existing
    options = _options(format="bestaudio/best", outtmpl=str(run_dir / "audio_source.%(ext)s"))
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.extract_info(url, download=True)
    return next(run_dir.glob("audio_source.*"))


def _from_url(url: str, runs_dir: Path) -> Source:
    run_dir, _ = _inspect(url, runs_dir)
    video = run_dir / "source.mp4"
    if not (video.exists() and "title" in load_info(run_dir)):
        options = _options(
            format=FORMAT,
            merge_output_format="mp4",
            # Some sites only offer other containers, and the website needs one browsers can play.
            postprocessors=[{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
            outtmpl=str(run_dir / "source.%(ext)s"),
        )
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
        _store_details(run_dir, url, info)

    info = load_info(run_dir)
    return Source(run_dir, video, info["title"], info["chapters"])


def get_source(target: str, runs_dir: Path) -> Source:
    path = Path(target).expanduser()
    if path.is_file():
        run_dir = runs_dir / path.stem
        run_dir.mkdir(parents=True, exist_ok=True)
        save_info(run_dir, title=path.stem, chapters=[], platform="local file")
        return Source(run_dir, path.resolve(), path.stem, [])
    return _from_url(target, runs_dir)
