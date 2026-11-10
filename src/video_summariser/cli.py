"""Shorten a video. Every stage caches into runs/<id>/, so re-running only redoes the render."""

import argparse
import sys
from pathlib import Path

from video_summariser.llm import MissingCredentialsError
from video_summariser.pipeline import prepare
from video_summariser.render import render


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", help="video link or path to a local video")
    parser.add_argument(
        "--ratio", type=float, default=0.3, help="rough share of the runtime to keep (default 0.3)"
    )
    parser.add_argument("--runs-dir", type=Path, default=Path("runs"))
    args = parser.parse_args()

    try:
        prepared = prepare(args.target, args.runs_dir, args.ratio)
    except MissingCredentialsError as error:
        sys.exit(f"No way to reach Claude. {error}")
    source = prepared.source
    output = render(
        source.video,
        source.title,
        source.chapters,
        prepared.sentences,
        prepared.sections,
        source.run_dir,
    )
    print(f"Shortened video: {output}")
    print(f"What was kept and cut: {source.run_dir}/report.md")
