"""The steps from a link to a finished edit, shared by the command line and the website."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yt_dlp

from video_summariser.articles import Article, read_article, save_article
from video_summariser.decide import ARTICLE_PROMPT, Section, decide
from video_summariser.fetch import (
    Source,
    fetch_audio,
    get_source,
    load_info,
    preview,
    set_watched,
)
from video_summariser.library import (
    Digest,
    check_video,
    ensure_digest,
    known_ideas,
    sentences_for,
)
from video_summariser.transcribe import Sentence, extract_audio, transcribe, transcribe_quickly
from video_summariser.visuals import describe_visuals

Report = Callable[[str], None]


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


def summarise(url: str, runs_dir: Path, report: Report = _ignore) -> Summary:
    """Work out whether a link is an article or a video, then write its summary and key ideas."""
    report("Reading the page")
    article = read_article(url)
    if article is not None and article.is_long:
        return _summarise_article(article, runs_dir, report)
    try:
        return _summarise_video(url, runs_dir, report)
    except yt_dlp.utils.DownloadError as error:
        if article is None:
            raise ValueError(f"No video or readable article at that link. {error}") from error
        return _summarise_article(article, runs_dir, report)


def shorten_article(
    summary: Summary, runs_dir: Path, ratio: float, report: Report = _ignore
) -> None:
    report("Deciding what to cut")
    decide(
        summary.title,
        [],
        sentences_for(summary.run_dir) or [],
        summary.run_dir,
        ratio,
        known_ideas(runs_dir, summary.run_dir),
        system_prompt=ARTICLE_PROMPT,
    )


def check(url: str, runs_dir: Path, report: Report = _ignore) -> Summary:
    summary = summarise(url, runs_dir, report)
    report("Comparing with what you've watched")
    check_video(summary.run_dir, summary.title, summary.digest, runs_dir)
    return summary


def mark_watched(url: str, runs_dir: Path, report: Report = _ignore) -> None:
    summary = summarise(url, runs_dir, report)
    set_watched(summary.run_dir, True)


def prepare(target: str, runs_dir: Path, ratio: float, report: Report = _ignore) -> Prepared:
    """Do everything up to the edit decisions. Each stage reuses earlier results."""
    report("Downloading the video")
    source = get_source(target, runs_dir)
    report("Transcribing")
    sentences = transcribe(source.video, source.run_dir)
    extract_audio(source.video, source.run_dir / "audio.wav")
    report("Writing the summary")
    ensure_digest(source.run_dir, source.title, sentences)
    report("Looking at what's on screen")
    on_screen = describe_visuals(source.video, source.run_dir)
    report("Deciding what to cut")
    sections = decide(
        source.title,
        source.chapters,
        sentences,
        source.run_dir,
        ratio,
        known_ideas(runs_dir, source.run_dir),
        on_screen,
    )
    return Prepared(source, sentences, sections)
