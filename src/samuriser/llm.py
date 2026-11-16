"""Ask Claude for a structured answer, via an API key if one is set, else the Claude Code login."""

import base64
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TypeVar, cast

import anthropic
from pydantic import BaseModel

MODEL = "claude-opus-5-5"
TIMEOUT_SECONDS = 1800

T = TypeVar("T", bound=BaseModel)


class MissingCredentialsError(RuntimeError):
    pass


def _content_blocks(prompt: str, images: Sequence[Path]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.standard_b64encode(image.read_bytes()).decode(),
            },
        }
        for image in images
    ]
    return [*blocks, {"type": "text", "text": prompt}]


def _via_api(system: str, content: list[dict[str, Any]], schema: type[T]) -> T:
    client = anthropic.Anthropic()
    with client.beta.messages.stream(
        model=MODEL,
        max_tokens=64000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "high"},
        output_format=schema,
        system=system,
        messages=cast(Any, [{"role": "user", "content": content}]),
    ) as stream:
        message = stream.get_final_message()
    if message.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"Claude stopped early: {message.stop_reason}")
    parsed = message.parsed_output
    if parsed is None:
        raise RuntimeError("Claude returned no parseable answer")
    return parsed


def _claude_code_command(system: str, schema: type[T]) -> list[str]:
    # No tools and no user settings: text inside a transcript can't make this do anything.
    return [
        "claude",
        "-p",
        "--model",
        MODEL,
        "--effort",
        "high",
        "--tools",
        "",
        "--no-session-persistence",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--system-prompt",
        system,
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--json-schema",
        json.dumps(schema.model_json_schema()),
    ]


def _via_claude_code(system: str, content: list[dict[str, Any]], schema: type[T]) -> T:
    if shutil.which("claude") is None:
        raise MissingCredentialsError("Set ANTHROPIC_API_KEY or install and log in to Claude Code.")
    message = {"type": "user", "message": {"role": "user", "content": content}}
    with tempfile.TemporaryDirectory() as empty_directory:
        completed = subprocess.run(
            _claude_code_command(system, schema),
            input=json.dumps(message) + "\n",
            capture_output=True,
            text=True,
            cwd=empty_directory,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    events = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")]
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if result is None or result.get("is_error") or result.get("structured_output") is None:
        detail = (result or {}).get("result") or completed.stderr
        raise RuntimeError(f"Claude Code returned no answer: {str(detail)[:500]}")
    return schema.model_validate(result["structured_output"])


def ask_claude(system: str, prompt: str, schema: type[T], images: Sequence[Path] = ()) -> T:
    content = _content_blocks(prompt, images)
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _via_api(system, content, schema)
    return _via_claude_code(system, content, schema)
