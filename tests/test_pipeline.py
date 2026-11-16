import json
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import replace
from fractions import Fraction
from io import BytesIO
from itertools import pairwise
from pathlib import Path
from typing import Any, Self

import numpy as np
import pytest
import yt_dlp
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import BaseModel

from video_summariser import library, pipeline, visuals
from video_summariser.articles import Article, read_article, save_article, split_blocks
from video_summariser.decide import (
    Decision,
    Section,
    build_prompt,
    decide,
    keep_sentence,
    save_sections,
    target_for,
    validate_sections,
)
from video_summariser.fetch import Source, canonical_url, load_info, preview, save_info
from video_summariser.library import Check, Digest, Idea, check_video, known_ideas, search
from video_summariser.llm import ask_claude
from video_summariser.publish import Settings, SiteFile, site_files, upload
from video_summariser.render import (
    Chapter,
    Segment,
    VideoInfo,
    build_chapters,
    build_filter_graph,
    group_kept,
    load_energy,
    panel_layout,
    plan_segments,
    quietest_time,
    render,
    tier_segments,
    write_overlay_frames,
)
from video_summariser.speech import list_voices
from video_summariser.transcribe import (
    MODEL,
    QUICK_MODEL,
    Sentence,
    Word,
    extract_audio,
    group_sentences,
    sentences_from_captions,
    transcribe,
    transcribe_quickly,
)
from video_summariser.web import (
    Job,
    JobQueue,
    create_app,
    publish_library,
    queue_from_playlist,
)

FPS = Fraction(30)


def sentence(id_: int, start: float, end: float) -> Sentence:
    return Sentence(id_, start, end, f"sentence {id_}.")


def test_sentences_split_on_punctuation_and_long_pauses() -> None:
    words = [
        Word("Hello", 0.0, 0.5),
        Word("there.", 0.5, 1.0),
        Word("Next", 1.1, 1.5),
        Word("thing", 3.0, 3.5),
    ]
    assert [s.text for s in group_sentences(words)] == ["Hello there.", "Next", "thing"]


def test_run_on_speech_is_split_at_the_hard_limit() -> None:
    words = [Word("word", float(i), float(i) + 0.9) for i in range(80)]
    assert max(s.end - s.start for s in group_sentences(words)) <= 36


def test_sections_must_cover_every_sentence_once() -> None:
    good = [
        Section(first=0, last=1, keep=True, note=""),
        Section(first=2, last=2, keep=False, note=""),
    ]
    validate_sections(good, 3)
    with pytest.raises(ValueError):
        validate_sections([Section(first=1, last=2, keep=True, note="")], 3)
    with pytest.raises(ValueError):
        validate_sections(good[:1], 3)


def test_only_the_sign_off_line_survives_a_long_outro() -> None:
    kept = Section(first=0, last=4, keep=True, note="core", tier=2)
    outro = Section(first=5, last=9, keep=False, note="plug and thanks", tier=3)

    sections = keep_sentence([kept, outro], 7, "Sign-off")

    assert [(s.first, s.last, s.keep, s.tier) for s in sections] == [
        (0, 4, True, 2),
        (5, 6, False, 3),
        (7, 7, True, 1),
        (8, 9, False, 3),
    ]
    validate_sections(sections, 10)


def test_a_sign_off_at_the_edge_of_a_section_leaves_nothing_empty() -> None:
    outro = Section(first=0, last=1, keep=False, note="thanks", tier=3)
    assert [(s.first, s.last) for s in keep_sentence([outro], 0, "x")] == [(0, 0), (1, 1)]
    assert [(s.first, s.last) for s in keep_sentence([outro], 1, "x")] == [(0, 0), (1, 1)]


def test_a_new_edit_ends_with_a_sign_off_even_if_the_chosen_line_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = Decision(
        sections=[
            Section(first=0, last=0, keep=True, note="core"),
            Section(first=1, last=2, keep=False, note="outro", tier=3),
        ],
        opening=None,
        sign_off=99,
    )
    monkeypatch.setattr("video_summariser.decide.ask_claude", lambda *_: answer)
    sentences = [sentence(0, 0, 1), sentence(1, 2, 3), sentence(2, 4, 5)]

    sections = decide("T", [], sentences, tmp_path, 0.3)

    assert (sections[-1].first, sections[-1].keep, sections[-1].tier) == (2, True, 1)


def test_a_new_edit_opens_with_one_line_of_a_long_intro(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = Decision(
        sections=[
            Section(first=0, last=2, keep=False, note="intro", tier=3),
            Section(first=3, last=3, keep=True, note="core", tier=2),
            Section(first=4, last=4, keep=False, note="outro", tier=3),
        ],
        opening=1,
        sign_off=4,
    )
    monkeypatch.setattr("video_summariser.decide.ask_claude", lambda *_: answer)
    sentences = [sentence(i, i * 2, i * 2 + 1) for i in range(5)]

    sections = decide("T", [], sentences, tmp_path, 0.3)

    assert [(s.first, s.last, s.keep, s.tier, s.note) for s in sections] == [
        (0, 0, False, 3, "intro"),
        (1, 1, True, 1, "Opening"),
        (2, 2, False, 3, "intro"),
        (3, 3, True, 2, "core"),
        (4, 4, True, 1, "Sign-off"),
    ]


def test_prompt_numbers_every_sentence() -> None:
    prompt = build_prompt("T", [], [sentence(0, 0, 1), sentence(1, 61, 62)], 0.3)
    assert "[0 0:00:00] sentence 0." in prompt
    assert "[1 0:01:01] sentence 1." in prompt
    assert "30%" in prompt


def test_kept_sentences_join_unless_cut_or_paused() -> None:
    sentences = [sentence(0, 0, 1), sentence(1, 1.2, 2), sentence(2, 2.1, 3), sentence(3, 9, 10)]
    sections = [
        Section(first=0, last=1, keep=True, note=""),
        Section(first=2, last=2, keep=False, note=""),
        Section(first=3, last=3, keep=True, note=""),
    ]
    groups = group_kept(sentences, sections)
    assert [(a.id, b.id) for a, b in groups] == [(0, 1), (3, 3)]

    all_kept = [Section(first=0, last=3, keep=True, note="")]
    assert [(a.id, b.id) for a, b in group_kept(sentences, all_kept)] == [(0, 2), (3, 3)]


def test_quietest_time_finds_the_silence_near_a_word() -> None:
    energy = np.full(2000, 100.0)
    energy[190] = 0.0  # 0.95 s, plus half of the 20 ms window
    assert quietest_time(energy, 1.0, 0.25, 0.05) == pytest.approx(0.96)


def test_segments_land_on_whole_frames_and_never_overlap() -> None:
    energy = np.zeros(4000)
    groups = [
        (sentence(0, 1.003, 2.0), sentence(0, 1.003, 2.0)),
        (sentence(1, 2.01, 5.0), sentence(1, 2.01, 5.0)),
        (sentence(2, 6.0, 6.1), sentence(2, 6.0, 6.1)),  # too short to keep
    ]
    segments = plan_segments(groups, energy, FPS)
    assert len(segments) == 2
    assert segments[0].end <= segments[1].start
    for segment in segments:
        assert float(segment.start * 30) == pytest.approx(round(segment.start * 30))
        assert float(segment.end * 30) == pytest.approx(round(segment.end * 30))


def test_filter_graph_has_one_audio_clip_per_segment() -> None:
    graph = build_filter_graph([Segment(1, 2), Segment(4, 6)], FPS)
    assert "asplit=2[a0][a1]" in graph
    assert "[c0][c1]concat=n=2:v=0:a=1[a]" in graph
    assert graph.count("between(t,") == 2


def test_clock_is_drawn_while_frames_still_have_their_original_time() -> None:
    graph = build_filter_graph([Segment(1, 2)], FPS)
    assert graph.index("select=") < graph.index("overlay=") < graph.index("setpts=N")


def test_overlay_highlights_the_current_chapter_on_both_bars(tmp_path: Path) -> None:
    info = VideoInfo(FPS, 1920, 1080, 4.0)
    chapters = [Chapter(0, 2, "First"), Chapter(2, 4, "Second")]
    write_overlay_frames(tmp_path, info, chapters, [Segment(0, 1), Segment(2, 3)])

    frames = sorted(tmp_path.glob("*.png"))
    assert [f.name for f in frames] == [f"{i:05d}.png" for i in range(5)]
    assert len({Image.open(f).size for f in frames}) == 1

    layout = panel_layout(info)
    middle = layout.bars_left + layout.bars_width / 2

    def accent_columns_in_bars(path: Path) -> list[int]:
        pixels = np.array(Image.open(path).convert("RGB"))
        columns = np.nonzero((pixels == (0x39, 0x87, 0xE5)).all(axis=2).any(axis=0))[0]
        return [int(c) for c in columns if c < layout.bars_left + layout.bars_width]

    assert max(accent_columns_in_bars(frames[0])) < middle
    assert min(accent_columns_in_bars(frames[3])) >= middle


def test_video_chapters_are_used_when_present() -> None:
    raw: list[dict[str, Any]] = [{"start_time": 0, "end_time": 9, "title": "Intro"}]
    assert build_chapters(raw, [], [], 9.0) == [Chapter(0, 9, "Intro")]


def test_kept_topics_become_chapters_when_the_video_has_none() -> None:
    sentences = [sentence(0, 1, 2), sentence(1, 3, 4), sentence(2, 5, 6)]
    sections = [
        Section(first=0, last=0, keep=True, note="one"),
        Section(first=1, last=1, keep=False, note="padding"),
        Section(first=2, last=2, keep=True, note="two"),
    ]
    assert build_chapters([], sentences, sections, 10.0) == [
        Chapter(0.0, 5, "one"),
        Chapter(5, 10.0, "two"),
    ]


def _probe(path: Path) -> dict[str, tuple[float, int]]:
    output = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-count_frames",
            "-show_entries",
            "stream=codec_type,duration,nb_read_frames",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {
        s["codec_type"]: (float(s["duration"]), int(s["nb_read_frames"]))
        for s in json.loads(output)["streams"]
    }


def test_render_produces_the_planned_length_in_sync(tmp_path: Path) -> None:
    video = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=d=20:s=320x240:r=30",
            "-f",
            "lavfi",
            "-i",
            "sine=f=440:d=20",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-shortest",
            str(video),
        ],
        check=True,
    )
    extract_audio(video, tmp_path / "audio.wav")
    sentences = [sentence(0, 1, 4), sentence(1, 4.2, 6), sentence(2, 6.2, 10), sentence(3, 12, 16)]
    sections = [
        Section(first=0, last=1, keep=True, note="start"),
        Section(first=2, last=2, keep=False, note="padding"),
        Section(first=3, last=3, keep=True, note="end"),
    ]

    output = render(video, "Test", [], sentences, sections, tmp_path)

    streams = _probe(output)
    planned = (6 - 1) + (16 - 12)
    # Each of the four cut points may move to nearby quiet: 0.05 s inward or 0.25 s outward.
    assert planned - 0.2 <= streams["video"][0] <= planned + 1.0
    assert streams["audio"][0] == pytest.approx(streams["video"][0], abs=0.05)
    assert "keep" in (tmp_path / "report.md").read_text()


def _make_video(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=d=20:s=320x240:r=30",
            "-f",
            "lavfi",
            "-i",
            "sine=f=440:d=20",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
    )


class Verdict(BaseModel):
    verdict: str


def _fake_claude(
    monkeypatch: pytest.MonkeyPatch, event: dict[str, Any], captured: dict[str, Any]
) -> None:
    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["input"] = kwargs["input"]
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(event) + "\n", stderr="")

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("video_summariser.llm.shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr("video_summariser.llm.subprocess.run", fake_run)


def test_claude_code_runs_with_no_tools_and_returns_the_parsed_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    event = {"type": "result", "is_error": False, "structured_output": {"verdict": "yes"}}
    _fake_claude(monkeypatch, event, captured)

    assert ask_claude("system", "the question", Verdict).verdict == "yes"
    command = captured["command"]
    assert command[command.index("--tools") + 1] == ""
    assert "the question" in captured["input"]


def test_a_failed_claude_code_run_raises_instead_of_returning_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_claude(monkeypatch, {"type": "result", "is_error": True, "result": "limit reached"}, {})
    with pytest.raises(RuntimeError, match="limit reached"):
        ask_claude("system", "question", Verdict)


RATE_LIMITED = (
    "ERROR: Unable to download video subtitles for 'en': HTTP Error 429: Too Many Requests"
)


def _refusing_downloader(
    monkeypatch: pytest.MonkeyPatch,
    refusal: str = RATE_LIMITED,
    pictures: dict[str, bytes | None] | None = None,
) -> list[str]:
    """Stand in for yt-dlp and refuse every caption request. Returns the links it was asked for."""
    requested: list[str] = []
    listed = pictures or {}

    class RefusingDownloader:
        def __init__(self, _: dict[str, Any]) -> None:
            pass

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def extract_info(self, url: str, download: bool) -> dict[str, Any]:
            assert not download
            return {
                "id": "abc",
                "title": "A video",
                "duration": 9,
                "webpage_url_domain": url.split("/")[2],
                "thumbnails": [{"url": address} for address in listed],
            }

        def download(self, urls: list[str]) -> None:
            requested.extend(urls)
            raise yt_dlp.utils.DownloadError(refusal)

        def urlopen(self, address: str) -> BytesIO:
            picture = listed[address]
            if picture is None:
                raise yt_dlp.utils.YoutubeDLError("Not found")
            return BytesIO(picture)

    monkeypatch.setattr("video_summariser.fetch.yt_dlp.YoutubeDL", RefusingDownloader)
    return requested


def _picture(width: int, height: int) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height)).save(buffer, "PNG")
    return buffer.getvalue()


def test_the_best_thumbnail_that_loads_is_saved_at_card_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pictures = {
        "small": _picture(120, 90),
        "large": _picture(1920, 1080),
        "not-a-picture": b"junk",
        "largest-but-missing": None,
    }
    _refusing_downloader(monkeypatch, pictures=pictures)

    details = preview("https://example.com/video", tmp_path)

    assert Image.open(details.run_dir / "thumbnail.jpg").size == (480, 270)


def test_a_missing_thumbnail_does_not_stop_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refusing_downloader(monkeypatch, pictures={"missing": None})

    details = preview("https://example.com/video", tmp_path)

    assert details.title == "A video"
    assert not (details.run_dir / "thumbnail.jpg").exists()


def test_a_refused_caption_download_does_not_stop_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refusing_downloader(monkeypatch)

    details = preview("https://example.com/video", tmp_path)

    assert details.title == "A video"
    assert details.captions is None
    assert load_info(details.run_dir)["duration"] == 9


def test_a_rate_limit_stops_caption_requests_to_that_site_until_the_pause_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested = _refusing_downloader(monkeypatch)
    blocked, other = "https://youtube.com/watch", "https://vimeo.com/1"

    preview(blocked, tmp_path)
    preview(blocked, tmp_path)
    assert requested == [blocked]

    preview(other, tmp_path)
    assert requested == [blocked, other]

    pauses = tmp_path / "caption_pauses.json"
    pauses.write_text(json.dumps({"youtube.com": "2020-01-01T00:00:00+00:00"}))
    preview(blocked, tmp_path)
    assert requested == [blocked, other, blocked]


def test_other_caption_failures_do_not_pause_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested = _refusing_downloader(monkeypatch, "ERROR: Unable to download: HTTP Error 404")

    preview("https://example.com/video", tmp_path)
    preview("https://example.com/video", tmp_path)

    assert len(requested) == 2


def test_a_summarised_video_never_asks_for_captions_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested = _refusing_downloader(monkeypatch)
    run_dir = tmp_path / "abc"
    run_dir.mkdir()
    (run_dir / "digest.json").write_text("{}")

    preview("https://example.com/video", tmp_path)

    assert requested == []


def test_the_quick_transcript_never_replaces_the_accurate_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    models: list[str] = []

    def fake_whisper(_: str, path_or_hf_repo: str, **__: Any) -> dict[str, Any]:
        models.append(path_or_hf_repo)
        words = [{"word": "Accurate.", "start": 0.0, "end": 1.0}]
        return {"segments": [{"text": "Rough phrase.", "start": 0.0, "end": 1.0, "words": words}]}

    monkeypatch.setattr("video_summariser.transcribe.mlx_whisper.transcribe", fake_whisper)
    (tmp_path / "audio.wav").touch()

    def saved_text() -> list[str]:
        return [s.text for s in library.sentences_for(tmp_path) or []]

    assert [s.text for s in transcribe_quickly(tmp_path / "a.webm", tmp_path)] == ["Rough phrase."]
    assert saved_text() == ["Rough phrase."]

    assert [s.text for s in transcribe(tmp_path / "a.mp4", tmp_path)] == ["Accurate."]
    assert saved_text() == ["Accurate."]
    assert models == [QUICK_MODEL, MODEL]


def test_captions_become_timed_sentences(tmp_path: Path) -> None:
    path = tmp_path / "captions.en.json3"
    events = [
        {"tStartMs": 1000, "segs": [{"utf8": "Hello"}, {"utf8": " there.", "tOffsetMs": 400}]},
        {"tStartMs": 3000, "segs": [{"utf8": "\n"}]},
        {"tStartMs": 4000, "segs": [{"utf8": "Next one."}]},
        {"tStartMs": 5000},
    ]
    path.write_text(json.dumps({"events": events}))
    found = [(s.text, s.start) for s in sentences_from_captions(path)]
    assert found[0] == ("Hello there.", 1.0)
    assert found[-1] == ("Next one.", 4.0)


def _library_video(runs: Path, video_id: str, watched: bool, idea: str) -> Path:
    run_dir = runs / video_id
    run_dir.mkdir(parents=True)
    info = {"title": f"Title {video_id}", "chapters": [], "watched": watched}
    (run_dir / "info.json").write_text(json.dumps(info))
    digest = Digest(summary="A summary.", ideas=[Idea(start=5.0, text=idea)])
    (run_dir / "digest.json").write_text(digest.model_dump_json())
    return run_dir


def test_only_watched_videos_count_as_known(tmp_path: Path) -> None:
    new = _library_video(tmp_path, "new", False, "new idea")
    _library_video(tmp_path, "seen", True, "seen idea")
    _library_video(tmp_path, "queued", False, "queued idea")
    assert known_ideas(tmp_path, new) == ["seen idea"]


def test_a_video_is_never_compared_with_itself(tmp_path: Path) -> None:
    video = _library_video(tmp_path, "seen", True, "seen idea")
    assert known_ideas(tmp_path, video) == []


def test_empty_library_says_watch_without_calling_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def must_not_run(*_: Any, **__: Any) -> None:
        raise AssertionError("Claude should not be called")

    monkeypatch.setattr(library, "ask_claude", must_not_run)
    video = _library_video(tmp_path, "new", False, "an idea")
    digest = Digest(summary="s", ideas=[Idea(start=1.0, text="an idea")])
    check = check_video(video, "New", digest, tmp_path)
    assert (check.verdict, check.new_ideas) == ("watch", [0])
    assert (video / "check.json").exists()


def test_skip_check_compares_against_watched_ideas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, str] = {}

    def fake_ask(_: str, prompt: str, __: type[Check]) -> Check:
        captured["prompt"] = prompt
        return Check(verdict="skip", reason="All known.", overlaps=[], new_ideas=[])

    monkeypatch.setattr(library, "ask_claude", fake_ask)
    video = _library_video(tmp_path, "new", False, "fresh claim")
    _library_video(tmp_path, "seen", True, "seen claim")
    _library_video(tmp_path, "unseen", False, "unseen claim")
    digest = Digest(summary="s", ideas=[Idea(start=1.0, text="fresh claim")])

    assert check_video(video, "New", digest, tmp_path).verdict == "skip"
    assert "seen claim" in captured["prompt"]
    assert "unseen claim" not in captured["prompt"]
    assert "0. fresh claim" in captured["prompt"]


def test_key_ideas_are_saved_in_time_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    draft = library.DraftDigest(
        summary="s",
        padding=140,
        ideas=[
            library.DraftIdea(sentence=1, text="later"),
            library.DraftIdea(sentence=0, text="sooner"),
        ],
    )
    monkeypatch.setattr(library, "ask_claude", lambda *_: draft)
    digest = library.ensure_digest(tmp_path, "T", [sentence(0, 1, 2), sentence(1, 5, 6)])
    assert [i.text for i in digest.ideas] == ["sooner", "later"]
    assert digest.padding == 100


def test_an_older_summary_gains_a_padding_estimate_without_being_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _library_video(tmp_path, "abc", False, "an idea")
    asked: list[type] = []

    def fake_ask(_: str, __: str, schema: type) -> library.PaddingEstimate:
        asked.append(schema)
        return library.PaddingEstimate(padding=55)

    monkeypatch.setattr(library, "ask_claude", fake_ask)
    sentences = [sentence(0, 1, 2)]

    first = library.ensure_digest(run_dir, "T", sentences)
    second = library.ensure_digest(run_dir, "T", sentences)

    assert (first.padding, first.ideas[0].text) == (55, "an idea")
    assert second == first
    assert asked == [library.PaddingEstimate]


@pytest.mark.parametrize(
    ("padding", "target"), [(None, 0.3), (20, 0.3), (0, 0.375), (44, 0.21), (70, 0.15), (95, 0.15)]
)
def test_padded_items_are_cut_harder_than_dense_ones(padding: int | None, target: float) -> None:
    assert target_for(padding) == pytest.approx(target)


def test_an_article_is_cut_to_the_target_its_padding_suggests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    targets: list[float] = []

    def fake_decide(*args: Any, **__: Any) -> list[Section]:
        targets.append(args[4])
        return []

    monkeypatch.setattr(pipeline, "decide", fake_decide)
    dense = pipeline.Summary(tmp_path, "T", Digest(summary="s", ideas=[], padding=20))

    pipeline.shorten_text(dense, tmp_path)
    pipeline.shorten_text(dense, tmp_path, ratio=0.25)

    assert targets == [pytest.approx(0.3), 0.25]


def test_the_library_card_carries_the_padding_estimate(tmp_path: Path) -> None:
    run_dir = _library_video(tmp_path, "abc", False, "an idea")
    digest = Digest(summary="s", ideas=[], padding=72)
    (run_dir / "digest.json").write_text(digest.model_dump_json())
    client = TestClient(create_app(tmp_path, start_worker=False))
    assert client.get("/api/videos").json()["videos"][0]["padding"] == 72


def test_search_lists_key_ideas_before_transcript_lines(tmp_path: Path) -> None:
    run_dir = _library_video(tmp_path, "one", False, "Skills are reusable prompts")
    transcript = [{"id": 0, "start": 9.0, "end": 10.0, "text": "We talked about reusable skills"}]
    (run_dir / "transcript.json").write_text(json.dumps(transcript))

    hits = search(tmp_path, "REUSABLE skills")
    assert [(h.kind, h.start) for h in hits] == [("idea", 5.0), ("transcript", 9.0)]
    assert search(tmp_path, "   ") == []


def test_similar_frames_are_dropped_and_sheets_are_filled(tmp_path: Path) -> None:
    def frame(name: str, grey: int) -> Path:
        path = tmp_path / name
        Image.new("RGB", (480, 270), (grey, grey, grey)).save(path)
        return path

    dark, same, bright = frame("a.jpg", 10), frame("b.jpg", 11), frame("c.jpg", 200)
    kept = visuals.drop_similar_frames([(0.0, dark), (20.0, same), (40.0, bright)])
    assert [seconds for seconds, _ in kept] == [0.0, 40.0]

    frames = [(float(i), dark) for i in range(17)]
    assert len(visuals.write_sheets(frames, tmp_path)) == 2


def test_each_length_setting_only_plays_sections_up_to_its_tier() -> None:
    sentences = [sentence(0, 1, 3), sentence(1, 5, 7), sentence(2, 9, 11)]
    sections = [
        Section(first=0, last=0, keep=True, note="core", tier=1),
        Section(first=1, last=1, keep=True, note="depth", tier=2),
        Section(first=2, last=2, keep=False, note="padding", tier=1),
    ]
    energy = np.zeros(4000)
    assert len(tier_segments(sentences, sections, energy, FPS, 1)) == 1
    assert len(tier_segments(sentences, sections, energy, FPS, 2)) == 2
    assert len(tier_segments(sentences, sections, energy, FPS, 3)) == 2


@pytest.fixture
def site(tmp_path: Path) -> tuple[TestClient, Path]:
    run_dir = tmp_path / "abc"
    run_dir.mkdir()
    _make_video(run_dir / "source.mp4")
    extract_audio(run_dir / "source.mp4", run_dir / "audio.wav")
    assert load_energy(run_dir / "audio.wav").size
    (run_dir / "info.json").write_text(json.dumps({"title": "A video", "chapters": []}))
    transcript = [
        {"id": 0, "start": 1.0, "end": 4.0, "text": "first part."},
        {"id": 1, "start": 6.0, "end": 8.0, "text": "padding part."},
        {"id": 2, "start": 10.0, "end": 14.0, "text": "last part."},
    ]
    (run_dir / "transcript.json").write_text(json.dumps(transcript))
    save_sections(
        run_dir,
        [
            Section(first=0, last=0, keep=True, note="first", tier=1),
            Section(first=1, last=1, keep=False, note="repeats [2]", tier=3),
            Section(first=2, last=2, keep=True, note="last", tier=2),
        ],
    )
    return TestClient(create_app(tmp_path, start_worker=False)), run_dir


def test_site_gives_the_player_each_length_and_section(site: tuple[TestClient, Path]) -> None:
    client, _ = site
    detail = client.get("/api/videos/abc").json()
    assert detail["playable"] is True
    assert [s["keep"] for s in detail["sections"]] == [True, False, True]
    assert detail["sections"][1]["note"] == "repeats 0:00:10"
    assert len(detail["tiers"]["1"]) == 1
    assert len(detail["tiers"]["3"]) == 2
    assert client.get("/api/videos").json()["videos"][0]["short_duration"] > 0


def test_restoring_a_cut_saves_it_and_replans_playback(site: tuple[TestClient, Path]) -> None:
    client, run_dir = site
    before = client.get("/api/videos/abc").json()["tiers"]["1"]

    after = client.post("/api/videos/abc/sections/1", json={"keep": True}).json()

    assert after["sections"][1]["keep"] is True
    assert len(after["tiers"]["1"]) > len(before)
    assert json.loads((run_dir / "cuts.json").read_text())["sections"][1]["keep"] is True


def test_video_is_served_in_ranges_so_browsers_can_seek(site: tuple[TestClient, Path]) -> None:
    client, _ = site
    response = client.get("/media/abc", headers={"Range": "bytes=0-99"})
    assert response.status_code == 206
    assert len(response.content) == 100


@pytest.mark.parametrize("video_id", ["nope", "..%2F..%2Fetc", "a.b"])
def test_unknown_or_unsafe_video_ids_are_refused(
    site: tuple[TestClient, Path], video_id: str
) -> None:
    client, _ = site
    assert client.get(f"/api/videos/{video_id}").status_code == 404


def test_only_web_links_can_be_queued(site: tuple[TestClient, Path]) -> None:
    client, _ = site
    bad = client.post("/api/jobs", json={"urls": ["/etc/passwd"], "kind": "check"})
    assert bad.status_code == 400
    good = client.post("/api/jobs", json={"urls": ["https://example.com/v", " "], "kind": "check"})
    assert good.json() == {"added": 1}
    assert client.get("/api/videos").json()["jobs"][0]["stage"] == "Waiting"


def test_the_library_offers_a_thumbnail_only_when_there_is_one(
    site: tuple[TestClient, Path],
) -> None:
    client, run_dir = site
    assert client.get("/api/videos").json()["videos"][0]["thumbnail"] is False
    assert client.get("/thumbnail/abc").status_code == 404

    Image.new("RGB", (16, 9)).save(run_dir / "thumbnail.jpg")

    assert client.get("/api/videos").json()["videos"][0]["thumbnail"] is True
    assert client.get("/thumbnail/abc").headers["content-type"] == "image/jpeg"


@pytest.mark.parametrize("path", ["/", "/watch/abc", "/static/library.js", "/static/app.css"])
def test_browsers_are_told_to_check_for_updates(site: tuple[TestClient, Path], path: str) -> None:
    client, _ = site
    assert client.get(path).headers["cache-control"] == "no-cache"


def test_the_verdict_is_hidden_once_a_video_is_watched(site: tuple[TestClient, Path]) -> None:
    client, run_dir = site
    check = Check(verdict="watch", reason="Nothing to compare.", overlaps=[], new_ideas=[])
    (run_dir / "check.json").write_text(check.model_dump_json())
    before = client.get("/api/videos/abc").json()
    assert (before["verdict"], before["reason"]) == ("watch", "Nothing to compare.")

    client.post("/api/videos/abc/watched", json={"watched": True})

    for card in (
        client.get("/api/videos/abc").json(),
        client.get("/api/videos").json()["videos"][0],
    ):
        assert (card["verdict"], card["reason"]) == (None, None)


def test_the_library_lists_the_latest_watched_or_added_video_first(tmp_path: Path) -> None:
    # Written in this order so that file times alone would give the wrong answer.
    newer = _library_video(tmp_path, "newer", True, "idea")
    save_info(newer, watched_at="2026-03-01T09:00:00+00:00")
    older = _library_video(tmp_path, "older", True, "idea")
    save_info(older, watched_at="2026-01-01T09:00:00+00:00")
    _library_video(tmp_path, "just_added", False, "idea")
    client = TestClient(create_app(tmp_path, start_worker=False))

    videos = client.get("/api/videos").json()["videos"]

    assert [v["id"] for v in videos] == ["just_added", "newer", "older"]


def _long_paragraph(topic: str, sentences: int = 40) -> str:
    line = f"The {topic} matters for many ordinary reasons that people often overlook."
    return " ".join([line] * sentences)


ARTICLE_PAGE = f"""
<html><head><title>A Long Read</title>
<meta property="og:title" content="A Long Read">
<meta property="og:image" content="https://example.com/picture.jpg"></head>
<body><article><h1>A Long Read</h1><p><b>Bold</b> start. {_long_paragraph("first")}</p>
<h2>Second part</h2><p>{_long_paragraph("second")}</p></article></body></html>
"""


def test_an_article_page_becomes_plain_blocks_without_its_repeated_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("video_summariser.articles.download_page", lambda _: ARTICLE_PAGE.encode())

    article = read_article("https://www.example.com/post#comments")

    assert article is not None
    assert (article.title, article.site) == ("A Long Read", "example.com")
    assert article.url == "https://www.example.com/post"
    assert article.image == "https://example.com/picture.jpg"
    assert article.is_long
    assert (True, "Second part") in article.blocks
    assert all(text != "A Long Read" and "*" not in text for _, text in article.blocks)


def test_a_page_with_little_text_is_not_an_article(monkeypatch: pytest.MonkeyPatch) -> None:
    page = "<html><body><article><p>Just a short video description.</p></article></body></html>"
    monkeypatch.setattr("video_summariser.articles.download_page", lambda _: page.encode())
    assert read_article("https://example.com/video") is None


def test_articles_split_into_sentences_timed_by_reading_position() -> None:
    sentences, blocks = split_blocks([(True, "A heading"), (False, 'One two. "Three four." Five?')])

    assert [s.text for s in sentences] == ["A heading", "One two.", '"Three four."', "Five?"]
    assert [(b.heading, b.first, b.last) for b in blocks] == [(True, 0, 0), (False, 1, 3)]
    assert all(a.end == b.start for a, b in pairwise(sentences))


def _article(text: str = "Some words here.") -> Article:
    return Article("https://example.com/post", "A Long Read", "example.com", None, [(False, text)])


def test_saving_an_article_again_keeps_the_sentences_its_cuts_refer_to(tmp_path: Path) -> None:
    run_dir = save_article(_article("Original text."), tmp_path)
    assert save_article(_article("Edited text. It changed."), tmp_path) == run_dir
    assert [s.text for s in library.sentences_for(run_dir) or []] == ["Original text."]
    assert load_info(run_dir)["kind"] == "article"


@pytest.mark.parametrize(
    ("words", "video_outcome", "expected"),
    [
        (500, "unused", "article"),
        (150, "found", "video"),
        (150, "no video", "article"),
    ],
)
def test_links_go_to_the_article_or_video_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, words: int, video_outcome: str, expected: str
) -> None:
    video = pipeline.Summary(tmp_path, "video", Digest(summary="s", ideas=[]))

    def fake_video(*_: Any) -> pipeline.Summary:
        assert video_outcome != "unused"
        if video_outcome == "no video":
            raise yt_dlp.utils.DownloadError("Unsupported URL")
        return video

    monkeypatch.setattr(pipeline, "read_article", lambda *_: _article("word " * words))
    monkeypatch.setattr(pipeline, "_summarise_video", fake_video)
    monkeypatch.setattr(pipeline, "ensure_digest", lambda *_: Digest(summary="s", ideas=[]))

    summary = pipeline.summarise("https://example.com/x", tmp_path)

    assert ("article" if summary is not video else "video") == expected


def test_a_link_that_is_neither_explains_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_video(*_: Any) -> pipeline.Summary:
        raise yt_dlp.utils.DownloadError("Unsupported URL")

    monkeypatch.setattr(pipeline, "read_article", lambda *_: None)
    monkeypatch.setattr(pipeline, "_summarise_video", no_video)

    with pytest.raises(ValueError, match="No video or readable article"):
        pipeline.summarise("https://example.com/x", tmp_path)


def test_a_shortened_article_opens_in_the_reader_with_a_length_for_each_sentence(
    tmp_path: Path,
) -> None:
    run_dir = save_article(_article("One two three four. Five six seven eight."), tmp_path)
    save_sections(
        run_dir,
        [
            Section(first=0, last=0, keep=True, note="core", tier=1),
            Section(first=1, last=1, keep=False, note="padding", tier=3),
        ],
    )
    client = TestClient(create_app(tmp_path, start_worker=False))

    assert "/static/read.js" in client.get(f"/watch/{run_dir.name}").text
    detail = client.get(f"/api/videos/{run_dir.name}").json()
    assert detail["kind"] == "article"
    assert detail["shortened"] is True
    assert [s["tier"] for s in detail["blocks"][0]["sentences"]] == [1, None]
    assert detail["short_duration"] == pytest.approx(detail["duration"] / 2)


def test_an_unshortened_article_can_still_be_read_in_full(tmp_path: Path) -> None:
    run_dir = save_article(_article("One two three."), tmp_path)
    client = TestClient(create_app(tmp_path, start_worker=False))

    detail = client.get(f"/api/videos/{run_dir.name}").json()

    assert detail["shortened"] is False
    assert detail["short_duration"] is None
    assert detail["blocks"][0]["sentences"][0]["text"] == "One two three."


def test_a_sentence_is_read_aloud_once_then_served_from_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = save_article(_article("One two three."), tmp_path)
    requests: list[dict[str, Any]] = []

    def fake_kokoro(request: urllib.request.Request, timeout: int) -> BytesIO:
        assert isinstance(request.data, bytes)
        requests.append(json.loads(request.data))
        return BytesIO(b"mp3 audio")

    monkeypatch.setattr("video_summariser.speech.urllib.request.urlopen", fake_kokoro)
    client = TestClient(create_app(tmp_path, start_worker=False))
    url = f"/speech/{run_dir.name}/0?voice=af_sky"

    first = client.get(url)
    second = client.get(url)

    assert first.headers["content-type"] == "audio/mpeg"
    assert second.content == b"mp3 audio"
    assert [(r["voice"], r["input"]) for r in requests] == [("af_sky", "One two three.")]


def test_speech_explains_when_kokoro_is_not_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(*_: Any, **__: Any) -> None:
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr("video_summariser.speech.urllib.request.urlopen", refused)
    run_dir = save_article(_article("One two three."), tmp_path)
    client = TestClient(create_app(tmp_path, start_worker=False))

    assert client.get(f"/speech/{run_dir.name}/0?voice=af_sky").status_code == 503
    assert client.get("/api/speech/voices").json() == []


@pytest.mark.parametrize(
    ("sentence", "voice"), [("0", "..%2F..%2Fetc"), ("0", "af_v0sky"), ("5", "af_sky")]
)
def test_unsafe_voices_and_missing_sentences_are_refused(
    tmp_path: Path, sentence: str, voice: str
) -> None:
    run_dir = save_article(_article("One two three."), tmp_path)
    client = TestClient(create_app(tmp_path, start_worker=False))
    assert client.get(f"/speech/{run_dir.name}/{sentence}?voice={voice}").status_code == 404


def test_only_current_english_voices_are_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps({"voices": ["af_sky", "bm_daniel", "af_v0sky", "ef_dora"]}).encode()
    monkeypatch.setattr(
        "video_summariser.speech.urllib.request.urlopen", lambda *_, **__: BytesIO(body)
    )
    assert list_voices() == ["af_sky", "bm_daniel"]


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        (
            "https://www.youtube.com/watch?v=abc&list=WL&t=21s",
            "https://www.youtube.com/watch?v=abc",
        ),
        ("https://m.youtube.com/watch?v=abc", "https://www.youtube.com/watch?v=abc"),
        ("https://youtu.be/abc?t=5", "https://www.youtube.com/watch?v=abc"),
        ("https://example.com/post?page=2#comments", "https://example.com/post?page=2"),
    ],
)
def test_links_to_the_same_page_reduce_to_one_form(link: str, expected: str) -> None:
    assert canonical_url(link) == expected


def test_lookup_recognises_a_saved_item_however_it_was_opened(tmp_path: Path) -> None:
    run_dir = _library_video(tmp_path, "abc", False, "an idea")
    save_info(run_dir, url="https://www.youtube.com/watch?v=abc", links=["https://vimeo.com/1"])
    client = TestClient(create_app(tmp_path, start_worker=False))

    def found(link: str) -> str | None:
        item = client.get("/api/lookup", params={"url": link}).json()["item"]
        return item["id"] if item else None

    assert found("https://youtu.be/abc?t=9") == "abc"
    assert found("https://vimeo.com/1#t=3") == "abc"
    assert found("https://www.youtube.com/watch?v=other") is None


def test_lookup_reports_the_job_working_on_a_link(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path, start_worker=False))
    body = {"urls": ["https://youtu.be/abc"], "kind": "check", "page": "<html>big page</html>"}
    client.post("/api/jobs", json=body)

    found = client.get("/api/lookup", params={"url": "https://www.youtube.com/watch?v=abc&t=4"})

    assert found.json() == {
        "item": None,
        "job": {
            "kind": "check",
            "url": "https://youtu.be/abc",
            "title": None,
            "stage": "Waiting",
            "error": None,
            "progress": 0,
        },
    }
    assert "page" not in client.get("/api/videos").json()["jobs"][0]


def test_trying_a_link_again_replaces_its_earlier_failure(tmp_path: Path) -> None:
    jobs = JobQueue(tmp_path)
    failed = Job("check", "https://youtu.be/abc")
    jobs.add(failed)
    failed.error = "No captions"
    jobs.add(Job("check", "https://example.com/other"))

    jobs.add(Job("shorten", "https://www.youtube.com/watch?v=abc"))

    assert [(j["kind"], j["error"]) for j in jobs.snapshot()] == [
        ("check", None),
        ("shorten", None),
    ]


def test_the_same_link_is_not_queued_twice(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path, start_worker=False))

    def add(link: str, kind: str) -> int:
        added: int = client.post("/api/jobs", json={"urls": [link], "kind": kind}).json()["added"]
        return added

    assert add("https://youtu.be/abc", "shorten") == 1
    assert add("https://www.youtube.com/watch?v=abc&t=3", "shorten") == 0
    # Shortening includes the check, so asking for the check as well adds nothing.
    assert add("https://youtu.be/abc", "check") == 0
    assert add("https://youtu.be/other", "check") == 1
    # A check doesn't shorten, so shortening can still be asked for.
    assert add("https://youtu.be/other", "shorten") == 1
    assert len(client.get("/api/videos").json()["jobs"]) == 3


def test_unfinished_jobs_survive_a_restart_and_failed_ones_do_not(tmp_path: Path) -> None:
    before = JobQueue(tmp_path)
    before.add(Job("shorten", "https://youtu.be/abc", page="<html>"))
    failed = Job("check", "https://youtu.be/broken")
    before.add(failed)
    failed.error = "No captions"
    before.add(Job("check", "https://example.com/article"))

    after = JobQueue(tmp_path)

    assert [(j["kind"], j["url"], j["stage"]) for j in after.snapshot()] == [
        ("shorten", "https://youtu.be/abc", "Waiting"),
        ("check", "https://example.com/article", "Waiting"),
    ]


def test_a_running_job_shows_its_title_and_how_long_it_has_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _library_video(tmp_path, "abc", False, "an idea")
    save_info(run_dir, url="https://www.youtube.com/watch?v=abc")
    seen: list[dict[str, Any]] = []
    jobs = JobQueue(tmp_path)
    job = Job("check", "https://youtu.be/abc")

    def fake_check(*_: Any) -> pipeline.Summary:
        seen.append(job.view())
        return pipeline.Summary(run_dir, "Title abc", Digest(summary="s", ideas=[]))

    monkeypatch.setattr("video_summariser.web.check", fake_check)

    jobs._run(job)

    assert seen[0]["title"] == "Title abc"


def test_progress_follows_the_stages_and_never_claims_to_be_finished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr("video_summariser.web.time.monotonic", lambda: clock[0])
    job = Job("shorten", "https://example.com/article")
    assert job.progress() == 0

    seen = []
    for stage in ["Reading the page", "Writing the summary", "Comparing with what you've watched"]:
        job.enter(stage)
        seen.append(job.progress())
        clock[0] += 5
    during_stage = job.progress()
    # An article skips the video-only stages, and the bar skips ahead with it.
    job.enter("Deciding what to cut")
    at_last_stage = job.progress()
    clock[0] += 3600

    assert seen == sorted(seen)
    assert seen[-1] < during_stage < at_last_stage
    assert job.progress() == 99


def test_every_stage_a_job_reports_has_a_place_in_the_progress_plan() -> None:
    source = Path(pipeline.__file__).read_text()
    reported = set(re.findall(r'report\("([^"]+)"\)', source))
    planned = {name for name, _ in pipeline.DOWNLOAD_PLAN}
    assert reported and reported <= planned


def test_a_job_uses_the_page_it_was_given_and_remembers_the_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _library_video(tmp_path, "abc", False, "an idea")
    save_info(run_dir, url="https://player.example.com/abc")
    pages: list[str | None] = []

    def fake_check(url: str, runs_dir: Path, report: Any, page: str | None) -> pipeline.Summary:
        pages.append(page)
        return pipeline.Summary(run_dir, "T", Digest(summary="s", ideas=[]))

    monkeypatch.setattr("video_summariser.web.check", fake_check)
    app = create_app(tmp_path, start_worker=False)

    JobQueue(tmp_path)._run(Job("check", "https://example.com/watch/abc#t=1", page="<html>"))

    assert pages == ["<html>"]
    found = TestClient(app).get("/api/lookup", params={"url": "https://example.com/watch/abc"})
    assert found.json()["item"]["id"] == "abc"


def test_a_page_from_the_browser_is_read_without_downloading_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def must_not_download(_: str) -> None:
        raise AssertionError("The page was already supplied")

    monkeypatch.setattr("video_summariser.articles.download_page", must_not_download)

    article = read_article("https://example.com/members-only", ARTICLE_PAGE)

    assert article is not None
    assert article.title == "A Long Read"


def test_removing_an_item_deletes_everything_stored_for_it(site: tuple[TestClient, Path]) -> None:
    client, run_dir = site

    assert client.post("/api/videos/abc/remove").json() == {"removed": True}

    assert not run_dir.exists()
    assert client.get("/api/videos").json()["videos"] == []
    assert client.post("/api/videos/abc/remove").status_code == 404


def test_an_item_cannot_be_removed_while_a_job_is_working_on_it(tmp_path: Path) -> None:
    run_dir = _library_video(tmp_path, "abc", False, "an idea")
    save_info(run_dir, url="https://www.youtube.com/watch?v=abc")
    client = TestClient(create_app(tmp_path, start_worker=False))
    client.post("/api/jobs", json={"urls": ["https://youtu.be/abc"], "kind": "shorten"})

    assert client.post("/api/videos/abc/remove").status_code == 409
    assert run_dir.exists()


def test_marking_as_watched_is_saved(site: tuple[TestClient, Path]) -> None:
    client, _ = site
    client.post("/api/videos/abc/watched", json={"watched": True})
    detail = client.get("/api/videos/abc").json()
    assert detail["watched"] is True
    assert detail["watched_at"] is not None

    client.post("/api/videos/abc/watched", json={"watched": False})
    assert client.get("/api/videos/abc").json()["watched_at"] is None


QUICK_SENTENCES = [
    {"id": 0, "start": 0.0, "end": 4.0, "text": "first part."},
    {"id": 1, "start": 4.0, "end": 9.0, "text": "padding part."},
    {"id": 2, "start": 9.0, "end": 15.0, "text": "last part."},
]


def _captioned_video(runs: Path, url: str) -> pipeline.Summary:
    run_dir = _library_video(runs, "abc", False, "an idea")
    save_info(run_dir, url=url, duration=15)
    (run_dir / "quick_transcript.json").write_text(json.dumps(QUICK_SENTENCES))
    return pipeline.Summary(run_dir, "Title abc", Digest(summary="s", ideas=[], padding=20))


def _keep_first_and_last(*args: Any, **__: Any) -> list[Section]:
    sections = [
        Section(first=0, last=0, keep=True, note="first", tier=1),
        Section(first=1, last=1, keep=False, note="padding", tier=3),
        Section(first=2, last=2, keep=True, note="last", tier=2),
    ]
    save_sections(args[3], sections)
    return sections


def test_a_youtube_video_is_shortened_from_its_text_without_being_downloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = _captioned_video(tmp_path, "https://www.youtube.com/watch?v=abc")

    def must_not_download(*_: Any, **__: Any) -> None:
        raise AssertionError("The video should not be downloaded")

    monkeypatch.setattr("video_summariser.web.check", lambda *_: summary)
    monkeypatch.setattr("video_summariser.web.prepare", must_not_download)
    monkeypatch.setattr(pipeline, "decide", _keep_first_and_last)

    JobQueue(tmp_path)._run(Job("shorten", "https://youtu.be/abc"))

    client = TestClient(create_app(tmp_path, start_worker=False))
    detail = client.get("/api/videos/abc").json()
    assert detail["playable"] is True
    assert detail["has_video"] is False
    assert detail["tiers"]["1"] == [[0.0, 4.0]]
    assert detail["tiers"]["3"] == [[0.0, 4.0], [9.0, 15.0]]
    assert detail["short_duration"] == 10.0
    assert [s["keep"] for s in detail["sections"]] == [True, False, True]


def test_a_video_that_cannot_play_in_place_is_downloaded_to_shorten_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = _captioned_video(tmp_path, "https://vimeo.com/abc")
    job = Job("shorten", "https://vimeo.com/abc")
    downloaded: list[str] = []

    monkeypatch.setattr("video_summariser.web.check", lambda *_: summary)
    monkeypatch.setattr(
        "video_summariser.web.prepare", lambda url, *_, **__: downloaded.append(url)
    )
    monkeypatch.setattr("video_summariser.web.player_ranges", lambda _: {})

    JobQueue(tmp_path)._run(job)

    assert downloaded == ["https://vimeo.com/abc"]
    assert job.plan() == pipeline.DOWNLOAD_PLAN


def test_downloading_a_video_discards_cuts_made_from_its_captions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = _captioned_video(tmp_path, "https://www.youtube.com/watch?v=abc")
    run_dir = summary.run_dir
    _keep_first_and_last(None, None, None, run_dir)
    (run_dir / "segments.json").write_text("{}")
    source = Source(run_dir, run_dir / "source.mp4", "Title abc", [])
    cuts_when_deciding: list[bool] = []

    def fake_decide(*_: Any, **__: Any) -> list[Section]:
        cuts_when_deciding.append((run_dir / "cuts.json").exists())
        return []

    monkeypatch.setattr(pipeline, "get_source", lambda *_: source)
    monkeypatch.setattr(pipeline, "transcribe", lambda *_: [])
    monkeypatch.setattr(pipeline, "extract_audio", lambda *_: None)
    monkeypatch.setattr(pipeline, "ensure_digest", lambda *_: summary.digest)
    monkeypatch.setattr(pipeline, "describe_visuals", lambda *_: [])
    monkeypatch.setattr(pipeline, "decide", fake_decide)

    pipeline.prepare("https://youtu.be/abc", tmp_path)

    assert cuts_when_deciding == [False]
    assert not (run_dir / "segments.json").exists()


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []

    def put_object(self, Bucket: str, Key: str, **details: Any) -> None:
        self.calls.append(("put", Key))
        self.objects[Key] = details

    def delete_object(self, Bucket: str, Key: str) -> None:
        self.calls.append(("delete", Key))
        del self.objects[Key]


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    fake = FakeStorage()
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: fake)
    return fake


PUBLISH_SETTINGS = Settings(
    bucket="library", endpoint="https://example.com", key_id="k", secret="s"
)


def test_the_published_copy_has_every_page_the_library_links_to(
    site: tuple[TestClient, Path], storage: FakeStorage
) -> None:
    _, run_dir = site

    publish_library(run_dir.parent, PUBLISH_SETTINGS)

    listing = json.loads(storage.objects["api/videos"]["Body"])
    assert [v["id"] for v in listing["videos"]] == ["abc"]
    assert json.loads(storage.objects["api/videos/abc"]["Body"])["tiers"]["3"]
    page = storage.objects["watch/abc"]
    assert page["ContentType"].startswith("text/html")
    assert b"data-published" in page["Body"]
    assert b'href="/index.html"' in page["Body"]
    assert storage.objects["static/watch.js"]["ContentType"] == "text/javascript"
    assert "index.html" in storage.objects


def test_only_what_changed_is_uploaded_and_removed_items_are_deleted(
    tmp_path: Path, storage: FakeStorage
) -> None:
    state = tmp_path / "published.json"
    files = {"a": SiteFile(b"one", "text/plain"), "b": SiteFile(b"two", "text/plain")}
    assert upload(files, PUBLISH_SETTINGS, state) == 2
    storage.calls.clear()

    files = {"a": SiteFile(b"changed", "text/plain")}
    assert upload(files, PUBLISH_SETTINGS, state) == 2

    assert storage.calls == [("put", "a"), ("delete", "b")]
    assert upload(files, PUBLISH_SETTINGS, state) == 0


def test_an_article_is_published_without_its_text_unless_asked(
    tmp_path: Path, storage: FakeStorage
) -> None:
    run_dir = save_article(_article("The whole article. It has two sentences."), tmp_path)
    detail = f"api/videos/{run_dir.name}"

    publish_library(tmp_path, PUBLISH_SETTINGS)
    assert b"The whole article." not in storage.objects[detail]["Body"]

    publish_library(tmp_path, replace(PUBLISH_SETTINGS, article_text=True))
    assert b"The whole article." in storage.objects[detail]["Body"]


def test_articles_are_published_with_the_reader_page() -> None:
    files = site_files([], [{"id": "x", "kind": "article"}, {"id": "y", "kind": "video"}], {})
    assert b"/static/read.js" in files["watch/x"].body
    assert b"/static/watch.js" in files["watch/y"].body


def test_a_video_saved_to_the_playlist_is_shortened_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = "https://www.youtube.com/watch?v=one", "https://www.youtube.com/watch?v=two"
    saved = [first]
    monkeypatch.setattr("video_summariser.web.playlist_links", lambda _: saved)
    jobs = JobQueue(tmp_path)

    queue_from_playlist("https://www.youtube.com/playlist?list=x", jobs)
    assert [(j["kind"], j["url"]) for j in jobs.snapshot()] == [("shorten", first)]

    # A later check only takes what was saved since.
    saved.append(second)
    queue_from_playlist("https://www.youtube.com/playlist?list=x", jobs)
    queue_from_playlist("https://www.youtube.com/playlist?list=x", jobs)
    assert [j["url"] for j in jobs.snapshot()] == [first, second]
    assert json.loads((tmp_path / "inbox.json").read_text()) == [first, second]
