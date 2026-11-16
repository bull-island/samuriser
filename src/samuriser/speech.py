"""Read text aloud with Kokoro, the local speech model that voice mode already runs."""

import json
import re
import urllib.request
from pathlib import Path
from uuid import uuid4

KOKORO_URL = "http://127.0.0.1:8880"
# American and British English, leaving out Kokoro's older "v0" voices.
ENGLISH_VOICE = re.compile(r"[ab][fm]_(?!v0)[a-z]+")
VOICES_TIMEOUT_SECONDS = 2
SPEECH_TIMEOUT_SECONDS = 60


def list_voices() -> list[str]:
    """Return the voices on offer, or nothing if Kokoro isn't running."""
    url = f"{KOKORO_URL}/v1/audio/voices"
    try:
        with urllib.request.urlopen(url, timeout=VOICES_TIMEOUT_SECONDS) as response:
            voices = json.load(response)["voices"]
    except (OSError, ValueError, KeyError):
        return []
    return [voice for voice in voices if ENGLISH_VOICE.fullmatch(voice)]


def is_voice_name(voice: str) -> bool:
    return ENGLISH_VOICE.fullmatch(voice) is not None


def write_speech(text: str, voice: str, path: Path) -> None:
    """Save one sentence as an MP3. Raises OSError if Kokoro can't produce it."""
    body = {"model": "kokoro", "voice": voice, "response_format": "mp3", "input": text}
    request = urllib.request.Request(
        f"{KOKORO_URL}/v1/audio/speech",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=SPEECH_TIMEOUT_SECONDS) as response:
        audio = response.read()
    path.parent.mkdir(parents=True, exist_ok=True)
    # The player and its read-ahead can ask for the same sentence at once.
    partial = path.with_name(f"{uuid4().hex}.partial")
    partial.write_bytes(audio)
    partial.replace(path)
