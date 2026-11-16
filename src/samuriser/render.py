"""Render kept sentences into one video, cutting in the quiet between words."""

import json
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
from PIL import Image, ImageDraw, ImageFont

from samuriser.decide import Section
from samuriser.decide import timestamp as _clock
from samuriser.transcribe import Sentence

SAMPLE_RATE = 16000
WINDOW_SAMPLES = 320  # 20 ms
HOP_SAMPLES = 80  # 5 ms
SNAP_OUTWARD_SECONDS = 0.25
SNAP_INWARD_SECONDS = 0.05
MAX_PAUSE_SECONDS = 1.5
MIN_SEGMENT_SECONDS = 0.3
FADE_SECONDS = 0.015
OVERLAY_FONT = "/System/Library/Fonts/Menlo.ttc"
OVERLAY_MARGIN = 24
SURFACE = "#1a1a19"
PANEL_COLOUR = (26, 26, 25, 245)
TEXT_PRIMARY = "#f0efec"
TEXT_SECONDARY = "#c3c2b7"
ACCENT = "#3987e5"
ACCENT_CUT = "#234a78"
# Sections alternate between two greys, so only the current one carries colour.
KEPT_TONES = ("#96958c", "#6b6a64")
CUT_TONES = ("#45443f", "#36352f")

Energy = npt.NDArray[np.float64]


@dataclass(frozen=True)
class VideoInfo:
    fps: Fraction
    width: int
    height: int
    duration: float


@dataclass(frozen=True)
class Chapter:
    start: float
    end: float
    title: str


@dataclass(frozen=True)
class PanelLayout:
    size: tuple[int, int]
    padding: int
    gap: int
    bar_height: int
    bars_left: int
    bars_width: int
    text_left: int
    text_width: int
    row_centres: tuple[int, int]
    clock_font: ImageFont.FreeTypeFont
    small_font: ImageFont.FreeTypeFont


@dataclass(frozen=True)
class Segment:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def load_energy(wav: Path) -> Energy:
    """Return the mean squared amplitude of each 20 ms window, stepping every 5 ms."""
    with wave.open(str(wav)) as audio:
        samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype=np.int16)
    totals = np.concatenate(([0.0], np.cumsum(samples.astype(np.float64) ** 2)))
    starts = np.arange(0, max(len(samples) - WINDOW_SAMPLES, 0), HOP_SAMPLES)
    energy: Energy = (totals[starts + WINDOW_SAMPLES] - totals[starts]) / WINDOW_SAMPLES
    return energy


def quietest_time(energy: Energy, time: float, before: float, after: float) -> float:
    """Find the quietest moment near a word boundary, preferring the closest on a tie."""
    frames_per_second = SAMPLE_RATE / HOP_SAMPLES
    window_offset = WINDOW_SAMPLES / 2 / SAMPLE_RATE
    centre = int((time - window_offset) * frames_per_second)
    low = max(centre - int(before * frames_per_second), 0)
    high = min(centre + int(after * frames_per_second) + 1, len(energy))
    if low >= high:
        return time
    tie_break = np.abs(np.arange(low, high) - centre) * 1e-3
    return float(low + np.argmin(energy[low:high] + tie_break)) / frames_per_second + window_offset


def group_kept(
    sentences: list[Sentence], sections: list[Section], max_pause: float = MAX_PAUSE_SECONDS
) -> list[tuple[Sentence, Sentence]]:
    """Join runs of kept sentences, splitting wherever the speaker pauses for too long."""
    kept = {i for s in sections if s.keep for i in range(s.first, s.last + 1)}
    groups: list[tuple[Sentence, Sentence]] = []
    for sentence in sentences:
        if sentence.id not in kept:
            continue
        if groups:
            first, last = groups[-1]
            if last.id == sentence.id - 1 and sentence.start - last.end < max_pause:
                groups[-1] = (first, sentence)
                continue
        groups.append((sentence, sentence))
    return groups


def plan_segments(
    groups: list[tuple[Sentence, Sentence]], energy: Energy, fps: Fraction
) -> list[Segment]:
    """Snap each group to nearby silence and to whole video frames, so audio stays in sync."""
    segments: list[Segment] = []
    for first, last in groups:
        start = quietest_time(energy, first.start, SNAP_OUTWARD_SECONDS, SNAP_INWARD_SECONDS)
        end = quietest_time(energy, last.end, SNAP_INWARD_SECONDS, SNAP_OUTWARD_SECONDS)
        start_frame = max(round(start * fps), 0)
        end_frame = round(end * fps)
        if segments:
            start_frame = max(start_frame, round(Fraction(segments[-1].end) * fps))
        if Fraction(end_frame - start_frame) / fps < MIN_SEGMENT_SECONDS:
            continue
        segments.append(Segment(float(start_frame / fps), float(end_frame / fps)))
    return segments


def _at_tier(sections: list[Section], tier: int) -> list[Section]:
    return [s.model_copy(update={"keep": s.keep and s.tier <= tier}) for s in sections]


def tier_segments(
    sentences: list[Sentence],
    sections: list[Section],
    energy: Energy,
    fps: Fraction,
    tier: int,
) -> list[Segment]:
    """Plan one length setting, using only kept sections at or below this tier."""
    return plan_segments(group_kept(sentences, _at_tier(sections, tier)), energy, fps)


def tier_spans(sentences: list[Sentence], sections: list[Section], tier: int) -> list[Segment]:
    """Plan one length setting from sentence times alone, for a video that wasn't downloaded."""
    groups = group_kept(sentences, _at_tier(sections, tier))
    return [Segment(first.start, last.end) for first, last in groups]


def build_filter_graph(segments: list[Segment], fps: Fraction) -> str:
    # Half a frame of offset keeps exactly one side of each boundary frame in the selection.
    half_frame = float(1 / (2 * fps))
    video_terms = "+".join(
        f"between(t,{s.start - half_frame:.5f},{s.end - half_frame:.5f})" for s in segments
    )
    # The overlay goes on before setpts, while each frame still carries its original time.
    video = (
        f"[0:v]fps={fps},select='{video_terms}'[kept];\n"
        f"[kept][1:v]overlay=W-w-{OVERLAY_MARGIN}:H-h-{OVERLAY_MARGIN}:shortest=1,"
        f"setpts=N*{fps.denominator}/{fps.numerator}/TB[v]"
    )

    count = len(segments)
    split_outputs = "".join(f"[a{i}]" for i in range(count))
    joined_inputs = "".join(f"[c{i}]" for i in range(count))
    clips = [
        f"[a{i}]atrim=start={s.start:.6f}:end={s.end:.6f},asetpts=PTS-STARTPTS,"
        f"afade=t=in:d={FADE_SECONDS},"
        f"afade=t=out:st={s.duration - FADE_SECONDS:.6f}:d={FADE_SECONDS}[c{i}]"
        for i, s in enumerate(segments)
    ]
    audio = [
        f"[0:a]asplit={count}{split_outputs}",
        *clips,
        f"{joined_inputs}concat=n={count}:v=0:a=1[a]",
    ]
    return ";\n".join([video, *audio])


def probe_video(video: Path) -> VideoInfo:
    output = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=avg_frame_rate,width,height:format=duration",
            "-of",
            "json",
            str(video),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    probed = json.loads(output)
    stream = probed["streams"][0]
    return VideoInfo(
        Fraction(stream["avg_frame_rate"]),
        int(stream["width"]),
        int(stream["height"]),
        float(probed["format"]["duration"]),
    )


def build_chapters(
    video_chapters: list[dict[str, Any]],
    sentences: list[Sentence],
    sections: list[Section],
    duration: float,
) -> list[Chapter]:
    """Use the video's own chapters, or the kept topics when it has none."""
    if video_chapters:
        return [Chapter(c["start_time"], c["end_time"], c["title"]) for c in video_chapters]
    kept = [s for s in sections if s.keep]
    starts = [0.0, *(sentences[s.first].start for s in kept[1:])]
    ends = [*starts[1:], duration]
    return [Chapter(a, b, s.note) for a, b, s in zip(starts, ends, kept)]


def panel_layout(info: VideoInfo) -> PanelLayout:
    scale = info.height / 1080
    clock_font = ImageFont.truetype(OVERLAY_FONT, max(round(30 * scale), 8))
    small_font = ImageFont.truetype(OVERLAY_FONT, max(round(20 * scale), 6))
    padding = max(round(10 * scale), 2)
    row_offset = round(12 * scale)
    width = info.width - 2 * OVERLAY_MARGIN
    height = 2 * (padding + 2 * row_offset)
    text_width = max(round(510 * scale), round(clock_font.getlength("0:00:00 / 0:00:00")))
    text_left = width - padding - text_width
    bars_left = 2 * padding + round(small_font.getlength("Short"))
    return PanelLayout(
        size=(width, height),
        padding=padding,
        gap=max(round(2 * scale), 1),
        bar_height=max(round(12 * scale), 2),
        bars_left=bars_left,
        bars_width=max(text_left - padding - bars_left, 1),
        text_left=text_left,
        text_width=text_width,
        row_centres=(padding + row_offset, height - padding - row_offset),
        clock_font=clock_font,
        small_font=small_font,
    )


def _fit_text(text: str, font: ImageFont.FreeTypeFont, width: int) -> str:
    if font.getlength(text) <= width:
        return text
    while text and font.getlength(f"{text}…") > width:
        text = text[:-1]
    return f"{text.rstrip()}…"


def _bar_x(layout: PanelLayout, fraction: float) -> int:
    return layout.bars_left + round(fraction * layout.bars_width)


def _fill_bar(
    draw: ImageDraw.ImageDraw,
    layout: PanelLayout,
    row: int,
    start: float,
    end: float,
    colour: str,
) -> None:
    """Fill part of a bar between two fractions of its length. A zero-width part draws nothing."""
    centre = layout.row_centres[row]
    half = layout.bar_height // 2
    left, right = _bar_x(layout, start), _bar_x(layout, end) - 1
    if right >= left:
        draw.rectangle((left, centre - half, right, centre + half - 1), fill=colour)


def _draw_panel(
    layout: PanelLayout,
    duration: float,
    chapters: list[Chapter],
    segments: list[Segment],
    current: int,
) -> Image.Image:
    """Draw everything that stays fixed while one chapter is the current chapter."""
    panel = Image.new("RGBA", layout.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(panel)
    width, height = layout.size
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=layout.gap * 3, fill=PANEL_COLOUR)

    gap = layout.gap / layout.bars_width
    short_length = sum(s.duration for s in segments)
    for index, chapter in enumerate(chapters):
        tone = index % 2
        kept = ACCENT if index == current else KEPT_TONES[tone]
        cut = ACCENT_CUT if index == current else CUT_TONES[tone]

        chapter_end = chapter.end / duration - gap
        _fill_bar(draw, layout, 0, chapter.start / duration, chapter_end, cut)
        for segment in segments:
            start, end = max(segment.start, chapter.start), min(segment.end, chapter.end)
            if start < end:
                _fill_bar(draw, layout, 0, start / duration, min(end / duration, chapter_end), kept)

        short_start = _short_video_time(segments, chapter.start) / short_length
        short_end = _short_video_time(segments, chapter.end) / short_length
        _fill_bar(draw, layout, 1, short_start, short_end - gap, kept)

    for row, label in enumerate(("Full", "Short")):
        position = (layout.padding, layout.row_centres[row])
        draw.text(position, label, font=layout.small_font, fill=TEXT_SECONDARY, anchor="lm")

    centre = layout.row_centres[1]
    half = layout.bar_height // 2
    draw.rectangle(
        (layout.text_left, centre - half, layout.text_left + 2 * half - 1, centre + half - 1),
        fill=ACCENT,
    )
    title_left = layout.text_left + 2 * half + layout.padding // 2
    title = _fit_text(
        chapters[current].title,
        layout.small_font,
        layout.text_left + layout.text_width - title_left,
    )
    draw.text((title_left, centre), title, font=layout.small_font, fill=TEXT_PRIMARY, anchor="lm")
    return panel


def _draw_playhead(
    draw: ImageDraw.ImageDraw, layout: PanelLayout, row: int, fraction: float
) -> None:
    x = _bar_x(layout, fraction)
    centre = layout.row_centres[row]
    reach = layout.bar_height // 2 + 2 * layout.gap
    # A ring in the panel colour keeps the marker readable over any section.
    ring = 2 * layout.gap
    draw.rectangle((x - ring, centre - reach, x + ring, centre + reach), fill=SURFACE)
    half_width = layout.gap // 2 + 1
    draw.rectangle(
        (x - half_width, centre - reach, x + half_width, centre + reach), fill=TEXT_PRIMARY
    )


def write_overlay_frames(
    directory: Path, info: VideoInfo, chapters: list[Chapter], segments: list[Segment]
) -> None:
    """Draw one image per second of the source: both progress bars, the clock and the section."""
    layout = panel_layout(info)
    panels = [
        _draw_panel(layout, info.duration, chapters, segments, index)
        for index in range(len(chapters))
    ]
    short_length = sum(s.duration for s in segments)
    total = _clock(info.duration)
    for second in range(int(info.duration) + 1):
        current = max((i for i, c in enumerate(chapters) if c.start <= second), default=0)
        frame = panels[current].copy()
        draw = ImageDraw.Draw(frame)
        _draw_playhead(draw, layout, 0, second / info.duration)
        _draw_playhead(draw, layout, 1, _short_video_time(segments, second) / short_length)
        draw.text(
            (layout.text_left, layout.row_centres[0]),
            f"{_clock(second)} / {total}",
            font=layout.clock_font,
            fill=TEXT_PRIMARY,
            anchor="lm",
        )
        frame.save(directory / f"{second:05d}.png", compress_level=1)


def encode(video: Path, overlay_dir: Path, graph_path: Path, output: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-framerate",
            "1",
            "-start_number",
            "0",
            "-i",
            str(overlay_dir / "%05d.png"),
            "-/filter_complex",
            str(graph_path),
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-map_chapters",
            "-1",
            "-c:v",
            "h264_videotoolbox",
            "-q:v",
            "60",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(output),
        ],
        check=True,
    )


def _short_video_time(segments: list[Segment], source_time: float) -> float:
    """Convert a time in the source video to its position in the shortened video."""
    position = 0.0
    for segment in segments:
        if source_time < segment.end:
            return position + max(source_time - segment.start, 0.0)
        position += segment.duration
    return position


def write_report(
    path: Path,
    title: str,
    sentences: list[Sentence],
    sections: list[Section],
    segments: list[Segment],
) -> None:
    source_length = sentences[-1].end
    short_length = sum(s.duration for s in segments)
    lines = [
        f"# {title}",
        "",
        (
            f"Shortened from {_clock(source_length)} to {_clock(short_length)} "
            f"({short_length / source_length:.0%}), in {len(segments)} segments."
        ),
        "",
        "| Original | Short video | Decision | Note | Opening words |",
        "|---|---|---|---|---|",
    ]
    for section in sections:
        start = sentences[section.first].start
        short = _clock(_short_video_time(segments, start)) if section.keep else ""
        decision = "keep" if section.keep else "cut"
        opening = " ".join(sentences[section.first].text.split()[:10]).replace("|", "/")
        lines.append(f"| {_clock(start)} | {short} | {decision} | {section.note} | {opening} |")
    path.write_text("\n".join(lines) + "\n")


def render(
    video: Path,
    title: str,
    video_chapters: list[dict[str, Any]],
    sentences: list[Sentence],
    sections: list[Section],
    run_dir: Path,
) -> Path:
    info = probe_video(video)
    energy = load_energy(run_dir / "audio.wav")
    segments = plan_segments(group_kept(sentences, sections), energy, info.fps)

    graph_path = run_dir / "filter_graph.txt"
    graph_path.write_text(build_filter_graph(segments, info.fps))
    output = run_dir / "shortened.mp4"
    # Swapping in the finished file keeps the previous render playable until this one is done.
    partial = run_dir / "shortened.partial.mp4"
    chapters = build_chapters(video_chapters, sentences, sections, info.duration)
    with tempfile.TemporaryDirectory() as overlay_dir:
        write_overlay_frames(Path(overlay_dir), info, chapters, segments)
        encode(video, Path(overlay_dir), graph_path, partial)
    partial.replace(output)
    write_report(run_dir / "report.md", title, sentences, sections, segments)
    return output
