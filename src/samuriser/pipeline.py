"""The steps from a link to a finished edit, shared by the command line and the website."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yt_dlp

from samuriser.articles import Article, is_article, read_article, save_article
from samuriser.decide import ARTICLE_PROMPT, SYSTEM_PROMPT, Section, decide, target_for
from samuriser.fetch import (
    Source,
    fetch_audio,
    get_source,
    load_info,
    preview,
    set_watched,
)
from samuriser.library import (
    Digest,
    check_video,
    ensure_digest,
    known_ideas,
    sentences_for,
)
from samuriser.transcribe import Sentence, extract_audio, transcribe, transcribe_quickly
from samuriser.visuals import describe_visuals

Report = Callable[[str], None]

# The stages each kind of job reports, in order, with roughly how many seconds each takes.
# They only drive a progress bar, so the figures needn't be exact. A job skips the stages
# that don't apply to it, such as downloading for an article.
_CHECK_PLAN: list[tuple[str, float]] = [
    ("Reading the page", 3),
    ("Reading captions", 8),
    ("No captions, so transcribing the sound", 40),
    ("Writing the summary", 20),
    ("Comparing with what you've watched", 12),
]
PLANS: dict[str, list[tuple[str, float]]] = {
    "check": _CHECK_PLAN,
    "watched": _CHECK_PLAN[:-1],
    "shorten": [*_CHECK_PLAN, ("Deciding what to cut", 150)],
}
# Shortening a video that has to be downloaded first, because it can't be played where it is.
DOWNLOAD_PLAN: list[tuple[str, float]] = [
    *_CHECK_PLAN,
    ("Downloading the video", 40),
    ("Transcribing", 80),
    ("Writing the summary", 2),
    ("Looking at what's on screen", 25),
    ("Deciding what to cut", 150),
]


def _ignore(_: str) -> None:
    pass


@dataclass(frozen=True)
class Summary:
    run_dir: Path
    title: str
    digest: Digest


@dataclass(frozen=True)
class Prepared:
    source: Source
    sentences: list[Sentence]
    sections: list[Section]


def _summarise_article(article: Article, runs_dir: Path, report: Report) -> Summary:
    run_dir = save_article(article, runs_dir)
    title = load_info(run_dir)["title"]
    report("Writing the summary")
    return Summary(run_dir, title, ensure_digest(run_dir, title, sentences_for(run_dir) or []))


def _summarise_video(url: str, runs_dir: Path, report: Report) -> Summary:
    """Write a video's summary and key ideas, without downloading the picture."""
    report("Reading captions")
    details = preview(url, runs_dir)
    sentences = sentences_for(details.run_dir)
    if not sentences:
        report("No captions, so transcribing the sound")
        audio = fetch_audio(url, details.run_dir)
        sentences = transcribe_quickly(audio, details.run_dir)
    report("Writing the summary")
    digest = ensure_digest(details.run_dir, details.title, sentences)
    return Summary(details.run_dir, details.title, digest)


def summarise(
    url: str, runs_dir: Path, report: Report = _ignore, page: str | None = None
) -> Summary:
    """Work out whether a link is an article or a video, then write its summary and key ideas."""
    report("Reading the page")
    article = read_article(url, page)
    if article is not None and article.is_long:
        return _summarise_article(article, runs_dir, report)
    try:
        return _summarise_video(url, runs_dir, report)
    except yt_dlp.utils.DownloadError as error:
        if article is None:
            raise ValueError(f"No video or readable article at that link. {error}") from error
        return _summarise_article(article, runs_dir, report)


def shorten_text(
    summary: Summary, runs_dir: Path, ratio: float | None = None, report: Report = _ignore
) -> None:
    """Cut an article, or a video from its captions alone, without downloading anything.

    Without a `ratio`, the target follows how padded it is.
    """
    article = is_article(summary.run_dir)
    report("Deciding what to cut")
    decide(
        summary.title,
        [] if article else load_info(summary.run_dir).get("chapters", []),
        sentences_for(summary.run_dir) or [],
        summary.run_dir,
        ratio if ratio is not None else target_for(summary.digest.padding),
        known_ideas(runs_dir, summary.run_dir),
        system_prompt=ARTICLE_PROMPT if article else SYSTEM_PROMPT,
    )


def check(url: str, runs_dir: Path, report: Report = _ignore, page: str | None = None) -> Summary:
    summary = summarise(url, runs_dir, report, page)
    report("Comparing with what you've watched")
    check_video(summary.run_dir, summary.title, summary.digest, runs_dir)
    return summary


def mark_watched(
    url: str, runs_dir: Path, report: Report = _ignore, page: str | None = None
) -> Summary:
    summary = summarise(url, runs_dir, report, page)
    set_watched(summary.run_dir, True)
    return summary


def prepare(
    target: str, runs_dir: Path, ratio: float | None = None, report: Report = _ignore
) -> Prepared:
    """Do everything up to the edit decisions. Each stage reuses earlier results.

    Without a `ratio`, the target follows how padded the video is.
    """
    report("Downloading the video")
    source = get_source(target, runs_dir)
    if not (source.run_dir / "transcript.json").exists():
        # Cuts made from captions number their sentences differently from the transcript.
        for stale in ("cuts.json", "segments.json"):
            (source.run_dir / stale).unlink(missing_ok=True)
    report("Transcribing")
    sentences = transcribe(source.video, source.run_dir)
    extract_audio(source.video, source.run_dir / "audio.wav")
    report("Writing the summary")
    digest = ensure_digest(source.run_dir, source.title, sentences)
    report("Looking at what's on screen")
    on_screen = describe_visuals(source.video, source.run_dir)
    report("Deciding what to cut")
    sections = decide(
        source.title,
        source.chapters,
        sentences,
        source.run_dir,
        ratio if ratio is not None else target_for(digest.padding),
        known_ideas(runs_dir, source.run_dir),
        on_screen,
    )
    return Prepared(source, sentences, sections)
