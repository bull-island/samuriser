"""A local website for checking, shortening and watching videos."""

import json
import math
import os
import queue
import re
import shutil
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.types import Scope

from samuriser.articles import is_article, load_blocks
from samuriser.decide import Section, load_sections, save_sections, timestamp
from samuriser.fetch import (
    THUMBNAIL_FILE,
    YOUTUBE_HOSTS,
    add_link,
    canonical_url,
    load_info,
    playlist_links,
    set_watched,
)
from samuriser.library import (
    Check,
    Digest,
    read_model,
    search,
    sentences_for,
    video_dirs,
)
from samuriser.pipeline import (
    DOWNLOAD_PLAN,
    PLANS,
    check,
    mark_watched,
    prepare,
    shorten_text,
)
from samuriser.publish import (
    STATE_FILE,
    Settings,
    load_settings,
    site_files,
    upload,
    without_article_text,
)
from samuriser.render import (
    build_chapters,
    load_energy,
    probe_video,
    tier_segments,
    tier_spans,
)
from samuriser.speech import is_voice_name, list_voices, write_speech
from samuriser.transcribe import Sentence, load_sentences

STATIC_DIR = Path(__file__).parent / "static"
HOST = "127.0.0.1"
PORT = 8765
TIERS = (1, 2, 3)
MAX_FAILED_JOBS_SHOWN = 5
# Jobs still to do, kept on disk so that restarting the site doesn't lose them.
JOBS_FILE = "jobs.json"
# Links already taken from the playlist, so each one is only shortened once.
INBOX_FILE = "inbox.json"
INBOX_CHECK_SECONDS = 300
# Without this, browsers keep reusing old copies of the site's files for hours after an update.
ALWAYS_CHECK_FOR_UPDATES = {"Cache-Control": "no-cache"}

JobKind = Literal["check", "shorten", "watched"]
Ranges = dict[str, list[list[float]]]


class SiteFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers.update(ALWAYS_CHECK_FOR_UPDATES)
        return response


@dataclass
class Job:
    kind: JobKind
    url: str
    stage: str = "Waiting"
    error: str | None = None
    # The page as the browser extension saw it, when it sent one.
    page: str | None = None
    title: str | None = None
    # Where the job is in its kind's plan, and when it got there.
    step: int = -1
    stage_started: float = 0.0
    downloads: bool = False

    def plan(self) -> list[tuple[str, float]]:
        return DOWNLOAD_PLAN if self.downloads else PLANS[self.kind]

    def enter(self, stage: str) -> None:
        """Move on to a stage, skipping any earlier ones that didn't apply."""
        plan = self.plan()
        ahead = [i for i, (name, _) in enumerate(plan) if name == stage and i > self.step]
        if ahead:
            self.step = ahead[0]
        self.stage = stage
        self.stage_started = time.monotonic()

    def progress(self) -> int:
        """A rough percentage: finished stages, plus a share of the current one that grows
        with time but never quite completes it."""
        if self.step < 0:
            return 0
        plan = self.plan()
        typical = plan[self.step][1]
        finished = sum(seconds for _, seconds in plan[: self.step])
        elapsed = time.monotonic() - self.stage_started
        under_way = typical * (1 - math.exp(-elapsed / typical))
        return min(int(100 * (finished + under_way) / sum(s for _, s in plan)), 99)

    def view(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "url": self.url,
            "title": self.title,
            "stage": self.stage,
            "error": self.error,
            "progress": self.progress(),
        }

    def covers(self, other: "Job") -> bool:
        """Whether this job already does everything the other one would."""
        return self.kind == other.kind or (self.kind == "shorten" and other.kind == "check")


class JobQueue:
    """Runs one job at a time, because transcribing and downloading are heavy."""

    def __init__(self, runs_dir: Path, on_done: Callable[[], None] = lambda: None) -> None:
        self.runs_dir = runs_dir
        self.on_done = on_done
        self._pending: queue.Queue[Job] = queue.Queue()
        self._jobs: list[Job] = []
        self._lock = threading.Lock()
        saved = runs_dir / JOBS_FILE
        for unfinished in json.loads(saved.read_text()) if saved.exists() else []:
            self.add(Job(unfinished["kind"], unfinished["url"]))

    def _save(self) -> None:
        unfinished = [{"kind": j.kind, "url": j.url} for j in self._jobs if j.error is None]
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        (self.runs_dir / JOBS_FILE).write_text(json.dumps(unfinished, indent=1))

    def add(self, job: Job) -> bool:
        """Queue a job, unless one already under way for the same link covers it."""
        target = canonical_url(job.url)
        with self._lock:
            same_link = [j for j in self._jobs if canonical_url(j.url) == target]
            if any(j.error is None and j.covers(job) for j in same_link):
                return False
            # A new attempt replaces an earlier failure for the same link.
            self._jobs = [
                j for j in self._jobs if j.error is None or canonical_url(j.url) != target
            ]
            self._jobs.append(job)
            self._save()
        self._pending.put(job)
        return True

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            active = [j.view() for j in self._jobs if j.error is None]
            failed = [j.view() for j in self._jobs if j.error is not None]
        return [*active, *failed[-MAX_FAILED_JOBS_SHOWN:]]

    def find(self, url: str) -> dict[str, Any] | None:
        """The job for a link, preferring one still running over one that failed."""
        target = canonical_url(url)
        with self._lock:
            matches = [j for j in self._jobs if canonical_url(j.url) == target]
        matches.sort(key=lambda j: j.error is not None)
        return matches[0].view() if matches else None

    def _run(self, job: Job) -> None:
        known = _find_by_url(self.runs_dir, job.url)
        job.title = load_info(known).get("title") if known else None
        summarise_first = mark_watched if job.kind == "watched" else check
        summary = summarise_first(job.url, self.runs_dir, job.enter, job.page)
        job.title = summary.title
        add_link(summary.run_dir, canonical_url(job.url))
        if job.kind != "shorten":
            return
        if is_article(summary.run_dir):
            shorten_text(summary, self.runs_dir, report=job.enter)
            return
        if _plays_in_place(summary.run_dir):
            shorten_text(summary, self.runs_dir, report=job.enter)
        else:
            job.downloads = True
            prepare(job.url, self.runs_dir, report=job.enter)
        player_ranges(summary.run_dir)

    def run_forever(self) -> None:
        while True:
            job = self._pending.get()
            try:
                self._run(job)
            except Exception as error:  # noqa: BLE001 - one bad link must not stop the queue
                job.error = str(error)[:300] or type(error).__name__
            else:
                with self._lock:
                    self._jobs.remove(job)
                self.on_done()
            with self._lock:
                self._save()


def queue_from_playlist(playlist: str, jobs: JobQueue) -> None:
    """Shorten each video saved to the playlist, the first time it's seen there."""
    seen_file = jobs.runs_dir / INBOX_FILE
    seen = set(json.loads(seen_file.read_text())) if seen_file.exists() else set()
    new = [link for link in playlist_links(playlist) if link not in seen]
    for link in new:
        jobs.add(Job("shorten", link))
    if new:
        seen_file.write_text(json.dumps(sorted(seen | set(new)), indent=1))


def watch_playlist(playlist: str, jobs: JobQueue) -> None:
    """Keep taking in videos saved to a playlist, such as from the YouTube app on a phone."""
    while True:
        try:
            queue_from_playlist(playlist, jobs)
        except Exception as error:  # noqa: BLE001 - YouTube being unreachable is routine
            print(f"Couldn't read the playlist: {error}")
        time.sleep(INBOX_CHECK_SECONDS)


def _plays_in_place(run_dir: Path) -> bool:
    """Whether the browser extension can play the shortened version on the video's own page,
    which makes downloading it unnecessary."""
    url = load_info(run_dir).get("url") or ""
    return urlparse(url).hostname in YOUTUBE_HOSTS and not (run_dir / "source.mp4").exists()


def player_ranges(run_dir: Path) -> Ranges:
    """Work out the parts to play at each length setting."""
    cache = run_dir / "segments.json"
    if cache.exists():
        cached: Ranges = json.loads(cache.read_text())
        return cached
    sentences = sentences_for(run_dir) or []
    sections = load_sections(run_dir, len(sentences))
    video = run_dir / "source.mp4"
    if video.exists():
        fps = probe_video(video).fps
        energy = load_energy(run_dir / "audio.wav")
        segments = {t: tier_segments(sentences, sections, energy, fps, t) for t in TIERS}
    else:
        segments = {t: tier_spans(sentences, sections, t) for t in TIERS}
    ranges = {str(t): [[s.start, s.end] for s in segments[t]] for t in TIERS}
    cache.write_text(json.dumps(ranges))
    return ranges


def _playable(run_dir: Path) -> bool:
    """Whether a video has been shortened, so there are cuts to jump over."""
    return (run_dir / "cuts.json").exists() and not is_article(run_dir)


def _duration(run_dir: Path, info: dict[str, Any]) -> float | None:
    if info.get("duration"):
        return float(info["duration"])
    video = run_dir / "source.mp4"
    return probe_video(video).duration if video.exists() else None


def _last_activity(run_dir: Path, info: dict[str, Any]) -> float:
    """When a video was last watched or, if it hasn't been, when it was added."""
    if info.get("watched_at"):
        return datetime.fromisoformat(info["watched_at"]).timestamp()
    return (run_dir / "info.json").stat().st_mtime


def _sentence_tiers(sections: list[Section]) -> dict[int, int]:
    return {i: s.tier for s in sections if s.keep for i in range(s.first, s.last + 1)}


def _article_sections(run_dir: Path) -> tuple[list[Sentence], list[Section] | None]:
    sentences = load_sentences(run_dir / "transcript.json")
    shortened = (run_dir / "cuts.json").exists()
    return sentences, load_sections(run_dir, len(sentences)) if shortened else None


def _article_short_duration(run_dir: Path) -> float | None:
    sentences, sections = _article_sections(run_dir)
    if sections is None:
        return None
    return sum(sentences[i].end - sentences[i].start for i in _sentence_tiers(sections))


def _article_details(run_dir: Path) -> dict[str, Any]:
    sentences, sections = _article_sections(run_dir)
    tiers = _sentence_tiers(sections or [])
    blocks = [
        {
            "heading": block.heading,
            "sentences": [
                {
                    "id": i,
                    "start": sentences[i].start,
                    "end": sentences[i].end,
                    "text": sentences[i].text,
                    "tier": tiers.get(i),
                }
                for i in range(block.first, block.last + 1)
            ],
        }
        for block in load_blocks(run_dir)
    ]
    return {
        "chapters": [],
        "sections": [],
        "tiers": {},
        "blocks": blocks,
        "shortened": sections is not None,
    }


def _short_duration(run_dir: Path, playable: bool) -> float | None:
    if is_article(run_dir):
        return _article_short_duration(run_dir)
    return sum(e - s for s, e in player_ranges(run_dir)["3"]) if playable else None


def _card(run_dir: Path) -> dict[str, Any]:
    info = load_info(run_dir)
    digest = read_model(run_dir / "digest.json", Digest)
    # Once you've watched a video, the case for or against watching it no longer applies.
    verdict = None if info.get("watched") else read_model(run_dir / "check.json", Check)
    playable = _playable(run_dir)
    return {
        "id": run_dir.name,
        "kind": info.get("kind", "video"),
        "title": info["title"],
        "url": info.get("url"),
        "duration": _duration(run_dir, info),
        "short_duration": _short_duration(run_dir, playable),
        "watched": bool(info.get("watched")),
        "watched_at": info.get("watched_at"),
        "platform": info.get("platform"),
        "has_video": (run_dir / "source.mp4").exists(),
        "thumbnail": (run_dir / THUMBNAIL_FILE).exists(),
        "playable": playable,
        "summary": digest.summary if digest else None,
        "padding": digest.padding if digest else None,
        "verdict": verdict.verdict if verdict else None,
        "reason": verdict.reason if verdict else None,
        "last_activity": _last_activity(run_dir, info),
    }


def _cards(runs_dir: Path) -> list[dict[str, Any]]:
    """Every item's card, most recently watched or added first."""
    cards = (_card(d) for d in video_dirs(runs_dir))
    return sorted(cards, key=lambda c: c["last_activity"], reverse=True)


def _readable_note(note: str, sentences: list[Sentence]) -> str:
    """Swap the sentence numbers in a note for times a viewer can use."""

    def to_time(match: re.Match[str]) -> str:
        index = int(match.group(1))
        return timestamp(sentences[index].start) if index < len(sentences) else match.group(0)

    return re.sub(r"\[(\d+)\]", to_time, note)


def _edit_details(run_dir: Path, duration: float) -> dict[str, Any]:
    info = load_info(run_dir)
    sentences = sentences_for(run_dir) or []
    sections = load_sections(run_dir, len(sentences))
    chapters = build_chapters(info["chapters"], sentences, sections, duration)
    return {
        "chapters": [asdict(c) for c in chapters],
        "sections": [
            {
                "index": index,
                "start": sentences[s.first].start,
                "end": sentences[s.last].end,
                "keep": s.keep,
                "tier": s.tier,
                "note": _readable_note(s.note, sentences),
                "opening": " ".join(sentences[s.first].text.split()[:12]),
            }
            for index, s in enumerate(sections)
        ],
        "tiers": player_ranges(run_dir),
    }


def _detail(run_dir: Path, runs_dir: Path) -> dict[str, Any]:
    card = _card(run_dir)
    digest = read_model(run_dir / "digest.json", Digest)
    verdict = read_model(run_dir / "check.json", Check)
    overlap_ids = {o.video_id for o in verdict.overlaps} if verdict else set()
    titles = {d.name: load_info(d)["title"] for d in video_dirs(runs_dir) if d.name in overlap_ids}
    if is_article(run_dir):
        edit = _article_details(run_dir)
    elif card["playable"]:
        edit = _edit_details(run_dir, card["duration"])
    else:
        edit = {"chapters": [], "sections": [], "tiers": {}}
    return (
        card
        | edit
        | {
            "ideas": [i.model_dump() for i in digest.ideas] if digest else [],
            "overlaps": [o.model_dump() for o in verdict.overlaps] if verdict else [],
            "new_ideas": verdict.new_ideas if verdict else [],
            "overlap_titles": titles,
        }
    )


def publish_library(runs_dir: Path, settings: Settings) -> int:
    """Bring the published copy up to date. Returns how many files changed."""
    dirs = video_dirs(runs_dir)
    thumbnails = {d.name: d / THUMBNAIL_FILE for d in dirs if (d / THUMBNAIL_FILE).exists()}
    details = [_detail(d, runs_dir) for d in dirs]
    if not settings.article_text:
        details = [without_article_text(detail) for detail in details]
    files = site_files(_cards(runs_dir), details, thumbnails)
    return upload(files, settings, runs_dir / STATE_FILE)


class Publisher:
    """Republishes the library after each change, when publishing has been set up."""

    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = runs_dir
        self.settings = load_settings()
        self._changed = threading.Event()

    def changed(self) -> None:
        self._changed.set()

    def run_forever(self) -> None:
        if self.settings is None:
            return
        while True:
            self._changed.wait()
            self._changed.clear()
            try:
                publish_library(self.runs_dir, self.settings)
            except Exception as error:  # noqa: BLE001 - the local site works without it
                print(f"Publishing failed: {error}")


def _find_by_url(runs_dir: Path, url: str) -> Path | None:
    target = canonical_url(url)
    for run_dir in video_dirs(runs_dir):
        info = load_info(run_dir)
        if target == info.get("url") or target in info.get("links", []):
            return run_dir
    return None


class JobRequest(BaseModel):
    urls: list[str]
    kind: JobKind
    page: str | None = None


class SectionChange(BaseModel):
    keep: bool


class WatchedChange(BaseModel):
    watched: bool


def _is_web_link(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def create_app(runs_dir: Path, start_worker: bool = True) -> FastAPI:
    app = FastAPI(title="Samuriser")
    publisher = Publisher(runs_dir)
    jobs = JobQueue(runs_dir, on_done=publisher.changed)
    if start_worker:
        threading.Thread(target=jobs.run_forever, daemon=True).start()
        threading.Thread(target=publisher.run_forever, daemon=True).start()
        # Catch up on anything that changed while the site wasn't running.
        publisher.changed()
        playlist = os.environ.get("SAMURISER_INBOX_PLAYLIST")
        if playlist:
            threading.Thread(target=watch_playlist, args=(playlist, jobs), daemon=True).start()

    def run_dir_for(video_id: str) -> Path:
        run_dir = runs_dir / video_id
        if not re.fullmatch(r"[\w-]+", video_id) or not (run_dir / "info.json").exists():
            raise HTTPException(status_code=404, detail="No such video")
        return run_dir

    @app.get("/")
    def library_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers=ALWAYS_CHECK_FOR_UPDATES)

    @app.get("/watch/{video_id}")
    def watch_page(video_id: str) -> FileResponse:
        page = "read.html" if is_article(run_dir_for(video_id)) else "watch.html"
        return FileResponse(STATIC_DIR / page, headers=ALWAYS_CHECK_FOR_UPDATES)

    @app.get("/media/{video_id}")
    def media(video_id: str) -> FileResponse:
        path = run_dir_for(video_id) / "source.mp4"
        if not path.exists():
            raise HTTPException(status_code=404, detail="This video hasn't been downloaded")
        return FileResponse(path, media_type="video/mp4")

    @app.get("/thumbnail/{video_id}")
    def thumbnail(video_id: str) -> FileResponse:
        path = run_dir_for(video_id) / THUMBNAIL_FILE
        if not path.exists():
            raise HTTPException(status_code=404, detail="This video has no thumbnail")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/speech/{video_id}/{sentence}")
    def speech(video_id: str, sentence: int, voice: str) -> FileResponse:
        run_dir = run_dir_for(video_id)
        sentences = sentences_for(run_dir) or []
        if not is_voice_name(voice) or not 0 <= sentence < len(sentences):
            raise HTTPException(status_code=404, detail="No such sentence or voice")
        path = run_dir / "speech" / voice / f"{sentence}.mp3"
        if not path.exists():
            try:
                write_speech(sentences[sentence].text, voice, path)
            except OSError as error:
                raise HTTPException(status_code=503, detail="Speech isn't available") from error
        return FileResponse(path, media_type="audio/mpeg")

    @app.get("/api/speech/voices")
    def speech_voices() -> list[str]:
        return list_voices()

    @app.get("/api/videos")
    def list_videos() -> dict[str, Any]:
        return {"jobs": jobs.snapshot(), "videos": _cards(runs_dir)}

    @app.post("/api/jobs")
    def add_jobs(request: JobRequest) -> dict[str, int]:
        urls = [u.strip() for u in request.urls if u.strip()]
        invalid = [u for u in urls if not _is_web_link(u)]
        if invalid or not urls:
            raise HTTPException(status_code=400, detail=f"Not a video link: {invalid[:1] or ''}")
        # A page only belongs to the one link it was read from.
        page = request.page if len(urls) == 1 else None
        added = [jobs.add(Job(request.kind, url, page=page)) for url in urls]
        return {"added": sum(added)}

    @app.get("/api/lookup")
    def lookup(url: str) -> dict[str, Any]:
        """What's known about a link: the saved item, and any job working on it."""
        run_dir = _find_by_url(runs_dir, url)
        item = _detail(run_dir, runs_dir) if run_dir else None
        return {"item": item, "job": jobs.find(url)}

    @app.get("/api/videos/{video_id}")
    def video_detail(video_id: str) -> dict[str, Any]:
        return _detail(run_dir_for(video_id), runs_dir)

    @app.post("/api/videos/{video_id}/sections/{index}")
    def change_section(video_id: str, index: int, change: SectionChange) -> dict[str, Any]:
        run_dir = run_dir_for(video_id)
        sentences = sentences_for(run_dir) or []
        sections = load_sections(run_dir, len(sentences))
        if not 0 <= index < len(sections):
            raise HTTPException(status_code=404, detail="No such section")
        # A section you bring back should show at every length, so it counts as core.
        sections[index] = sections[index].model_copy(update={"keep": change.keep, "tier": 1})
        save_sections(run_dir, sections)
        (run_dir / "segments.json").unlink(missing_ok=True)
        publisher.changed()
        return _detail(run_dir, runs_dir)

    @app.post("/api/videos/{video_id}/watched")
    def change_watched(video_id: str, change: WatchedChange) -> dict[str, bool]:
        set_watched(run_dir_for(video_id), change.watched)
        publisher.changed()
        return {"watched": change.watched}

    @app.post("/api/videos/{video_id}/remove")
    def remove_video(video_id: str) -> dict[str, bool]:
        """Delete an item and everything stored for it, including its place in the history."""
        run_dir = run_dir_for(video_id)
        info = load_info(run_dir)
        for link in [info.get("url"), *info.get("links", [])]:
            job = jobs.find(link) if link else None
            if job is not None and job["error"] is None:
                raise HTTPException(
                    status_code=409,
                    detail="It's still being worked on. Try again when that finishes.",
                )
        shutil.rmtree(run_dir)
        publisher.changed()
        return {"removed": True}

    @app.get("/api/search")
    def search_videos(q: str) -> list[dict[str, Any]]:
        return [asdict(hit) for hit in search(runs_dir, q)]

    app.mount("/static", SiteFiles(directory=STATIC_DIR), name="static")
    return app


def publish_main() -> None:
    load_dotenv()
    settings = load_settings()
    if settings is None:
        sys.exit('Publishing isn\'t set up. See "Read it on your phone" in the README.')
    changed = publish_library(Path("runs"), settings)
    print(f"Published. {changed} files changed.")


def main() -> None:
    load_dotenv()
    print(f"Samuriser is running at http://{HOST}:{PORT}")
    uvicorn.run(create_app(Path("runs")), host=HOST, port=PORT, log_level="warning")
