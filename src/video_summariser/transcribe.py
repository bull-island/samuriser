"""Transcribe a video into sentences with accurate start and end times."""

import json
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import mlx_whisper

MODEL = "mlx-community/whisper-large-v3-turbo"
# The skip check only reads a video's ideas, so it can trade accuracy for speed.
QUICK_MODEL = "mlx-community/whisper-base-mlx"
QUICK_TRANSCRIPT = "quick_transcript.json"
SENTENCE_ENDINGS = (".", "?", "!")
PAUSE_BREAK_SECONDS = 1.2
SOFT_MAX_SECONDS = 20.0
HARD_MAX_SECONDS = 35.0


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float


@dataclass(frozen=True)
class Sentence:
    id: int
    start: float
    end: float
    text: str


def extract_audio(video: Path, wav: Path) -> None:
    """Write 16 kHz mono audio, the format Whisper expects and silence snapping reads."""
    if wav.exists():
        return
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(wav),
        ],
        check=True,
    )


def _words_from_whisper(wav: Path) -> list[Word]:
    result = mlx_whisper.transcribe(
        str(wav),
        path_or_hf_repo=MODEL,
        word_timestamps=True,
        # Stops Whisper repeating itself across long stretches of quiet audio.
        condition_on_previous_text=False,
    )
    return [
        Word(w["word"].strip(), float(w["start"]), float(w["end"]))
        for segment in result["segments"]
        for w in segment["words"]
        if w["word"].strip()
    ]


def _phrases_from_whisper(wav: Path) -> list[Word]:
    """Treat each phrase as one timed unit, because timing every word is slow."""
    result = mlx_whisper.transcribe(
        str(wav),
        path_or_hf_repo=QUICK_MODEL,
        condition_on_previous_text=False,
    )
    return [
        Word(s["text"].strip(), float(s["start"]), float(s["end"]))
        for s in result["segments"]
        if s["text"].strip()
    ]


def _ends_sentence(words: list[Word], index: int, sentence_start: float) -> bool:
    if index == len(words) - 1:
        return True
    word, next_word = words[index], words[index + 1]
    duration = word.end - sentence_start
    return (
        word.text.endswith(SENTENCE_ENDINGS)
        or next_word.start - word.end >= PAUSE_BREAK_SECONDS
        or (duration >= SOFT_MAX_SECONDS and word.text.endswith((",", ";", ":")))
        or duration >= HARD_MAX_SECONDS
    )


def group_sentences(words: list[Word]) -> list[Sentence]:
    sentences: list[Sentence] = []
    current: list[Word] = []
    for index, word in enumerate(words):
        current.append(word)
        if _ends_sentence(words, index, current[0].start):
            text = " ".join(w.text for w in current)
            sentences.append(Sentence(len(sentences), current[0].start, current[-1].end, text))
            current = []
    return sentences


def load_sentences(path: Path) -> list[Sentence]:
    return [Sentence(**s) for s in json.loads(path.read_text())]


def sentences_from_captions(path: Path) -> list[Sentence]:
    """Read YouTube's json3 captions. Timing is coarse, which is fine for summarising."""
    timed_text = [
        (float(event["tStartMs"] + seg.get("tOffsetMs", 0)) / 1000, seg["utf8"].strip())
        for event in json.loads(path.read_text()).get("events", [])
        for seg in event.get("segs", [])
        if seg.get("utf8", "").strip()
    ]
    ends = [start for start, _ in timed_text[1:]] + [timed_text[-1][0] + 1.0] if timed_text else []
    words = [Word(text, start, end) for (start, text), end in zip(timed_text, ends)]
    return group_sentences(words)


def _cached_transcript(
    path: Path, source: Path, read_words: Callable[[Path], list[Word]]
) -> list[Sentence]:
    if path.exists():
        return load_sentences(path)
    wav = path.parent / "audio.wav"
    extract_audio(source, wav)
    sentences = group_sentences(read_words(wav))
    path.write_text(json.dumps([asdict(s) for s in sentences], indent=1))
    return sentences


def transcribe(video: Path, run_dir: Path) -> list[Sentence]:
    return _cached_transcript(run_dir / "transcript.json", video, _words_from_whisper)


def transcribe_quickly(audio: Path, run_dir: Path) -> list[Sentence]:
    """Make a rougher transcript, kept separate so shortening still gets the accurate one."""
    return _cached_transcript(run_dir / QUICK_TRANSCRIPT, audio, _phrases_from_whisper)
