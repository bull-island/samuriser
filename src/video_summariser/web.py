"""A local website for checking, shortening and watching videos."""

import json
import queue
import re
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.types import Scope

from video_summariser.articles import is_article, load_blocks
from video_summariser.decide import Section, load_sections, save_sections, timestamp
from video_summariser.fetch import (
    THUMBNAIL_FILE,
    add_link,
    canonical_url,
    load_info,
    set_watched,
)
from video_summariser.library import (
    Check,
    Digest,
    read_model,
    search,
    sentences_for,
    video_dirs,
)
from video_summariser.pipeline import check, mark_watched, prepare, shorten_article
from video_summariser.render import build_chapters, load_energy, probe_video, tier_segments
from video_summariser.speech import is_voice_name, list_voices, write_speech
from video_summariser.transcribe import Sentence, load_sentences

STATIC_DIR = Path(__file__).parent / "static"
HOST = "127.0.0.1"
PORT = 8765
TIERS = (1, 2, 3)
DEFAULT_RATIO = 0.3
MAX_FAILED_JOBS_SHOWN = 5
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

    def view(self) -> dict[str, Any]:
        return {"kind": self.kind, "url": self.url, "stage": self.stage, "error": self.error}


class JobQueue:
    """Runs one job at a time, because transcribing and downloading are heavy."""

    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = runs_dir
        self._pending: queue.Queue[Job] = queue.Queue()
        self._jobs: list[Job] = []
        self._lock = threading.Lock()

    def add(self, job: Job) -> None:
        target = canonical_url(job.url)
        with self._lock:
            # A new attempt replaces an earlier failure for the same link.
            self._jobs = [
                j for j in self._jobs if j.error is None or canonical_url(j.url) != target
            ]
            self._jobs.append(job)
        self._pending.put(job)

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
        def report(stage: str) -> None:
            job.stage = stage

        summarise_first = mark_watched if job.kind == "watched" else check
        summary = summarise_first(job.url, self.runs_dir, report, job.page)
        add_link(summary.run_dir, canonical_url(job.url))
        if job.kind != "shorten":
            return
        if is_article(summary.run_dir):
            shorten_article(summary, self.runs_dir, DEFAULT_RATIO, report)
        else:
            prepare(job.url, self.runs_dir, DEFAULT_RATIO, report)
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


def player_ranges(run_dir: Path) -> Ranges:
    """Work out the parts to play at each length setting."""
    cache = run_dir / "segments.json"
    if cache.exists():
        cached: Ranges = json.loads(cache.read_text())
        return cached
    sentences = load_sentences(run_dir / "transcript.json")
    sections = load_sections(run_dir, len(sentences))
    fps = probe_video(run_dir / "source.mp4").fps
    energy = load_energy(run_dir / "audio.wav")
    ranges = {
        str(tier): [[s.start, s.end] for s in tier_segments(sentences, sections, energy, fps, tier)]
        for tier in TIERS
    }
    cache.write_text(json.dumps(ranges))
    return ranges


def _playable(run_dir: Path) -> bool:
    return (run_dir / "source.mp4").exists() and (run_dir / "cuts.json").exists()


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
        "verdict": verdict.verdict if verdict else None,
        "reason": verdict.reason if verdict else None,
        "last_activity": _last_activity(run_dir, info),
    }


def _readable_note(note: str, sentences: list[Sentence]) -> str:
    """Swap the sentence numbers in a note for times a viewer can use."""

    def to_time(match: re.Match[str]) -> str:
        index = int(match.group(1))
        return timestamp(sentences[index].start) if index < len(sentences) else match.group(0)

    return re.sub(r"\[(\d+)\]", to_time, note)


def _edit_details(run_dir: Path, duration: float) -> dict[str, Any]:
    info = load_info(run_dir)
    sentences = load_sentences(run_dir / "transcript.json")
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
    app = FastAPI(title="Video Summariser")
    jobs = JobQueue(runs_dir)
    if start_worker:
        threading.Thread(target=jobs.run_forever, daemon=True).start()

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
        cards = sorted(
            (_card(d) for d in video_dirs(runs_dir)),
            key=lambda c: c["last_activity"],
            reverse=True,
        )
        return {"jobs": jobs.snapshot(), "videos": cards}

    @app.post("/api/jobs")
    def add_jobs(request: JobRequest) -> dict[str, int]:
        urls = [u.strip() for u in request.urls if u.strip()]
        invalid = [u for u in urls if not _is_web_link(u)]
        if invalid or not urls:
            raise HTTPException(status_code=400, detail=f"Not a video link: {invalid[:1] or ''}")
        # A page only belongs to the one link it was read from.
        page = request.page if len(urls) == 1 else None
        for url in urls:
            jobs.add(Job(request.kind, url, page=page))
        return {"added": len(urls)}

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
        return _detail(run_dir, runs_dir)

    @app.post("/api/videos/{video_id}/watched")
    def change_watched(video_id: str, change: WatchedChange) -> dict[str, bool]:
        set_watched(run_dir_for(video_id), change.watched)
        return {"watched": change.watched}

    @app.get("/api/search")
    def search_videos(q: str) -> list[dict[str, Any]]:
        return [asdict(hit) for hit in search(runs_dir, q)]

    app.mount("/static", SiteFiles(directory=STATIC_DIR), name="static")
    return app


def main() -> None:
    print(f"Video Summariser is running at http://{HOST}:{PORT}")
    uvicorn.run(create_app(Path("runs")), host=HOST, port=PORT, log_level="warning")
