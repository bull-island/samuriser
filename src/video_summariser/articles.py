"""Turn a web article into the same timed sentences the rest of the tool reads."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urldefrag, urlparse

import trafilatura

from video_summariser.fetch import download_page, load_info, save_info, save_thumbnail
from video_summariser.transcribe import Sentence

# Below this a page is a video's landing page or a stub, not something to read.
MIN_ARTICLE_WORDS = 100
# Above this a page counts as an article even if it also has a video clip embedded.
LONG_ARTICLE_WORDS = 400
# Sentences are timed by reading pace, about 240 words a minute, so "time" means position.
READING_WORDS_PER_SECOND = 4.0
BLOCKS_FILE = "blocks.json"
SENTENCE_BREAK = re.compile(r"(?:(?<=[.!?])|(?<=[.!?][\"”’)]))\s+(?=[\"“‘(\[]?[A-Z0-9])")


@dataclass(frozen=True)
class Article:
    url: str
    title: str
    site: str
    image: str | None
    # Each page block, in order: whether it's a heading, and its text.
    blocks: list[tuple[bool, str]]

    @property
    def words(self) -> int:
        return sum(len(text.split()) for _, text in self.blocks)

    @property
    def is_long(self) -> bool:
        return self.words >= LONG_ARTICLE_WORDS


@dataclass(frozen=True)
class Block:
    heading: bool
    first: int
    last: int


def _plain_text(line: str) -> str:
    without_prefix = re.sub(r"^(?:#+|[-*+])\s+", "", line.strip())
    return re.sub(r"\*{1,3}([^*]+?)\*{1,3}", r"\1", without_prefix).replace("`", "").strip()


def _site_name(url: str) -> str:
    return (urlparse(url).hostname or url).removeprefix("www.")


def read_article(url: str, page: str | None = None) -> Article | None:
    """Pull the main text out of a page, or return None if it isn't an article.

    Pass `page` when the browser already has the page open, which also covers sites
    that need a login.
    """
    html = page or download_page(url)
    if html is None:
        return None
    markdown = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_formatting=True,
        include_comments=False,
        include_tables=False,
    )
    if markdown is None:
        return None
    metadata = trafilatura.extract_metadata(html, default_url=url)
    title = (metadata.title if metadata else None) or url
    lines = [line for line in markdown.splitlines() if line.strip()]
    blocks = [(line.lstrip().startswith("#"), _plain_text(line)) for line in lines]
    # Pages usually repeat their title as the first heading.
    body = [b for b in blocks if b[1] and b[1].casefold() != title.casefold()]
    image = metadata.image if metadata else None
    article = Article(urldefrag(url).url, title, _site_name(url), image, body)
    return article if article.words >= MIN_ARTICLE_WORDS else None


def split_blocks(blocks: list[tuple[bool, str]]) -> tuple[list[Sentence], list[Block]]:
    sentences: list[Sentence] = []
    result: list[Block] = []
    words = 0
    for heading, text in blocks:
        first = len(sentences)
        for part in [text] if heading else SENTENCE_BREAK.split(text):
            start = words / READING_WORDS_PER_SECOND
            words += len(part.split())
            end = words / READING_WORDS_PER_SECOND
            sentences.append(Sentence(len(sentences), start, end, part))
        result.append(Block(heading, first, len(sentences) - 1))
    return sentences, result


def save_article(article: Article, runs_dir: Path) -> Path:
    """Store an article once, because later cuts refer to its exact sentences."""
    digest = hashlib.sha256(article.url.encode()).hexdigest()[:12]
    run_dir = runs_dir / f"article-{digest}"
    if (run_dir / "info.json").exists():
        return run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    sentences, blocks = split_blocks(article.blocks)
    if article.image:
        save_thumbnail([article.image], run_dir)
    (run_dir / BLOCKS_FILE).write_text(json.dumps([asdict(b) for b in blocks], indent=1))
    (run_dir / "transcript.json").write_text(json.dumps([asdict(s) for s in sentences], indent=1))
    # Written last, because the library lists a folder as soon as this file exists.
    save_info(
        run_dir,
        kind="article",
        title=article.title,
        chapters=[],
        url=article.url,
        duration=sentences[-1].end,
        platform=article.site,
    )
    return run_dir


def is_article(run_dir: Path) -> bool:
    return load_info(run_dir).get("kind") == "article"


def load_blocks(run_dir: Path) -> list[Block]:
    return [Block(**b) for b in json.loads((run_dir / BLOCKS_FILE).read_text())]
