"""Publishes a read-only copy of the library to S3-style storage, so it can be read and
watched on a phone while this Mac is off."""

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3

STATIC_DIR = Path(__file__).parent / "static"
# What was last uploaded, so that only changes are sent.
STATE_FILE = "published.json"
SETTING_NAMES = ("BUCKET", "ENDPOINT", "KEY_ID", "SECRET")
STATIC_TYPES = {".js": "text/javascript", ".css": "text/css"}
HTML = "text/html; charset=utf-8"
JSON = "application/json"
# The pages hide everything that needs the local site when they carry this mark.
PUBLISHED_MARK = ('<html lang="en">', '<html lang="en" data-published>')
# Storage serves files, not folders, so the library needs a file name to link to.
HOME_LINK = ('href="/"', 'href="/index.html"')


@dataclass(frozen=True)
class Settings:
    bucket: str
    endpoint: str
    key_id: str
    secret: str
    # Whether the copy includes the full text of articles, which are other people's work.
    article_text: bool = False


@dataclass(frozen=True)
class SiteFile:
    body: bytes
    content_type: str


def load_settings() -> Settings | None:
    """Where to publish, or None when publishing hasn't been set up."""
    bucket, endpoint, key_id, secret = (
        os.environ.get(f"SAMURISER_PUBLISH_{name}", "") for name in SETTING_NAMES
    )
    if not (bucket and endpoint and key_id and secret):
        return None
    article_text = os.environ.get("SAMURISER_PUBLISH_ARTICLE_TEXT") == "1"
    return Settings(bucket, endpoint, key_id, secret, article_text)


def without_article_text(detail: dict[str, Any]) -> dict[str, Any]:
    """Leave an article with only its summary and key ideas."""
    return detail | {"blocks": []} if detail["kind"] == "article" else detail


def _page(name: str) -> SiteFile:
    html = (STATIC_DIR / name).read_text()
    for local, published in (PUBLISHED_MARK, HOME_LINK):
        html = html.replace(local, published)
    return SiteFile(html.encode(), HTML)


def _json(value: Any) -> SiteFile:
    return SiteFile(json.dumps(value).encode(), JSON)


def site_files(
    cards: list[dict[str, Any]], details: list[dict[str, Any]], thumbnails: dict[str, Path]
) -> dict[str, SiteFile]:
    """Every file of the published copy, under the same addresses the local site uses."""
    files = {"index.html": _page("index.html"), "api/videos": _json({"jobs": [], "videos": cards})}
    for path in sorted(STATIC_DIR.iterdir()):
        if path.suffix in STATIC_TYPES:
            files[f"static/{path.name}"] = SiteFile(path.read_bytes(), STATIC_TYPES[path.suffix])
    for detail in details:
        page = "read.html" if detail["kind"] == "article" else "watch.html"
        files[f"watch/{detail['id']}"] = _page(page)
        files[f"api/videos/{detail['id']}"] = _json(detail)
    for video_id, path in thumbnails.items():
        files[f"thumbnail/{video_id}"] = SiteFile(path.read_bytes(), "image/jpeg")
    return files


def upload(files: dict[str, SiteFile], settings: Settings, state_path: Path) -> int:
    """Send what changed since the last upload and delete what's gone. Returns how many
    files were sent or deleted."""
    client = boto3.client(
        "s3",
        endpoint_url=settings.endpoint,
        aws_access_key_id=settings.key_id,
        aws_secret_access_key=settings.secret,
        region_name="auto",
    )
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    # A different bucket has none of these files yet.
    sent: dict[str, str] = state["files"] if state.get("bucket") == settings.bucket else {}
    latest = {
        key: hashlib.sha256(file.content_type.encode() + file.body).hexdigest()
        for key, file in files.items()
    }
    changed = [key for key in files if sent.get(key) != latest[key]]
    removed = sorted(sent.keys() - files.keys())
    for key in changed:
        client.put_object(
            Bucket=settings.bucket,
            Key=key,
            Body=files[key].body,
            ContentType=files[key].content_type,
            CacheControl="no-cache",
        )
    for key in removed:
        client.delete_object(Bucket=settings.bucket, Key=key)
    state_path.write_text(json.dumps({"bucket": settings.bucket, "files": latest}))
    return len(changed) + len(removed)
