#!/usr/bin/env python3
"""Download YouTube transcripts for filtered videos and save them as Markdown."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from requests.exceptions import RequestException
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)
from youtube_transcript_api.proxies import GenericProxyConfig, WebshareProxyConfig


INPUT_PATH = Path(__file__).resolve().parent / "data" / "filtered_videos.json"
OUTPUT_ROOT = Path(__file__).resolve().parents[1] / "youtube-transcripts"
MAX_FILENAME_TITLE_LENGTH = 80
TRANSCRIPT_LANGUAGES = ("en", "en-US", "en-GB")
DOWNLOAD_DELAY_SECONDS = 10
MAX_TRANSCRIPT_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (5, 10)
TRANSIENT_ERROR_NAMES = {
    "IpBlocked",
    "RequestBlocked",
    "TooManyRequests",
    "YouTubeRequestFailed",
}
GENERIC_PROXY_ENV_VARS = (
    "YOUTUBE_TRANSCRIPT_HTTP_PROXY",
    "YOUTUBE_TRANSCRIPT_HTTPS_PROXY",
)
WEBSHARE_USERNAME_ENV_VAR = "YOUTUBE_TRANSCRIPT_WEBSHARE_USERNAME"
WEBSHARE_PASSWORD_ENV_VAR = "YOUTUBE_TRANSCRIPT_WEBSHARE_PASSWORD"


class TranscriptDownloadError(Exception):
    """Raised for user-facing transcript download failures."""


@dataclass(frozen=True)
class DownloadResult:
    video: dict[str, Any]
    downloaded: bool
    message: str
    output_path: Path | None = None


@dataclass(frozen=True)
class DownloadSummary:
    videos_processed: int
    downloaded: int
    failed: int
    output_dir: Path


def progress(message: str) -> None:
    """Print progress messages consistently and flush immediately."""
    print(message, flush=True)


def configure_console_encoding() -> None:
    """Use UTF-8 console output when the runtime supports it."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")


def load_env_files() -> None:
    """Load simple KEY=VALUE pairs from nearby .env files without extra dependencies."""
    script_dir = Path(__file__).resolve().parent
    candidates = [
        Path.cwd() / ".env",
        script_dir / ".env",
        script_dir.parent / ".env",
        script_dir.parent.parent / ".env",
    ]

    for env_path in candidates:
        if not env_path.exists():
            continue

        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue

            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def load_filtered_videos(input_path: Path) -> list[dict[str, Any]]:
    """Load filtered video metadata from disk."""
    if not input_path.exists():
        raise TranscriptDownloadError(f"Input file does not exist: {input_path}")

    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TranscriptDownloadError(f"Input file contains malformed JSON: {exc}") from exc

    if not isinstance(payload, list):
        raise TranscriptDownloadError("Input JSON must contain a list of video objects.")

    return [item for item in payload if isinstance(item, dict)]


def get_output_dir(videos: list[dict[str, Any]]) -> Path:
    """Build the output directory from the first available channel name."""
    channel_name = next(
        (str(video.get("channel_name", "")).strip() for video in videos if video.get("channel_name")),
        "unknown-channel",
    )
    return OUTPUT_ROOT / slugify_channel_name(channel_name)


def slugify_channel_name(channel_name: str) -> str:
    """Convert a channel name into a simple folder slug."""
    slug = channel_name.casefold().strip().replace(" ", "-")
    slug = re.sub(r"[^a-z0-9-]+", "", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "unknown-channel"


def download_transcripts(videos: list[dict[str, Any]], output_dir: Path) -> list[DownloadResult]:
    """Download transcripts for all filtered videos."""
    if output_dir.resolve() == OUTPUT_ROOT.resolve():
        raise TranscriptDownloadError("Transcript output must be a channel-specific subfolder.")

    output_dir.mkdir(parents=True, exist_ok=True)
    transcript_api: YouTubeTranscriptApi | None = None
    results: list[DownloadResult] = []

    for index, video in enumerate(sorted(videos, key=video_rank)):
        rank = video_rank(video)
        title = str(video.get("title", "")).strip() or "Untitled Video"

        try:
            output_path = output_dir / build_markdown_filename(video)
            if output_path.exists():
                progress(f"✓ [{rank:02d}] {title}")
                progress("    Already downloaded — skipping")
                results.append(DownloadResult(video=video, downloaded=True, message="Already downloaded", output_path=output_path))
                progress("")
                continue

            if transcript_api is None:
                transcript_api = create_transcript_api()

            progress(f"    Waiting {DOWNLOAD_DELAY_SECONDS} seconds before transcript request...")
            time.sleep(DOWNLOAD_DELAY_SECONDS)

            transcript_text = fetch_transcript_text_with_retries(transcript_api, str(video.get("video_id", "")))
            output_path.write_text(render_markdown(video, transcript_text), encoding="utf-8")
            progress(f"✓ [{rank:02d}] {title}")
            progress("    Transcript downloaded")
            results.append(DownloadResult(video=video, downloaded=True, message="Transcript downloaded", output_path=output_path))
        except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable) as exc:
            progress(f"✗ [{rank:02d}] {title}")
            progress("    Transcript unavailable")
            results.append(DownloadResult(video=video, downloaded=False, message=str(exc)))
        except Exception as exc:
            progress(f"✗ [{rank:02d}] {title}")
            progress("    Transcript unavailable")
            if is_youtube_block_error(exc):
                progress("    YouTube is blocking this IP. Set a transcript proxy in .env and rerun.")
            results.append(DownloadResult(video=video, downloaded=False, message=f"Unexpected error: {exc}"))

        progress("")

    return results


def create_transcript_api() -> YouTubeTranscriptApi:
    """Create a transcript API client with optional proxy configuration."""
    proxy_config = build_proxy_config()
    if proxy_config is not None:
        progress("Using configured transcript proxy.")
    return YouTubeTranscriptApi(proxy_config=proxy_config)


def build_proxy_config() -> GenericProxyConfig | WebshareProxyConfig | None:
    """Build an optional proxy config from environment variables."""
    webshare_username = os.getenv(WEBSHARE_USERNAME_ENV_VAR)
    webshare_password = os.getenv(WEBSHARE_PASSWORD_ENV_VAR)
    if webshare_username and webshare_password:
        return WebshareProxyConfig(
            proxy_username=webshare_username,
            proxy_password=webshare_password,
        )

    http_proxy = os.getenv("YOUTUBE_TRANSCRIPT_HTTP_PROXY") or os.getenv("HTTP_PROXY")
    https_proxy = os.getenv("YOUTUBE_TRANSCRIPT_HTTPS_PROXY") or os.getenv("HTTPS_PROXY")
    if http_proxy or https_proxy:
        return GenericProxyConfig(http_url=http_proxy, https_url=https_proxy)

    return None


def fetch_transcript_text_with_retries(transcript_api: YouTubeTranscriptApi, video_id: str) -> str:
    """Fetch transcript text with retries for transient YouTube/network failures."""
    for attempt in range(1, MAX_TRANSCRIPT_ATTEMPTS + 1):
        try:
            return fetch_transcript_text(transcript_api, video_id)
        except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable):
            raise
        except Exception as exc:
            if not is_transient_error(exc) or attempt == MAX_TRANSCRIPT_ATTEMPTS:
                raise

            wait_seconds = RETRY_BACKOFF_SECONDS[attempt - 1]
            progress(
                f"    Attempt {attempt} failed with {exc.__class__.__name__}. "
                f"Retrying attempt {attempt + 1} in {wait_seconds} seconds..."
            )
            time.sleep(wait_seconds)

    raise TranscriptDownloadError("Transcript retry loop exited unexpectedly.")


def is_transient_error(exc: Exception) -> bool:
    """Return True for temporary YouTube rate-limit or network failures."""
    return isinstance(exc, (RequestException, OSError)) or exc.__class__.__name__ in TRANSIENT_ERROR_NAMES


def is_youtube_block_error(exc: Exception) -> bool:
    """Return True when YouTube is blocking transcript requests from this IP."""
    return exc.__class__.__name__ in {"IpBlocked", "RequestBlocked", "TooManyRequests"}


def fetch_transcript_text(transcript_api: YouTubeTranscriptApi, video_id: str) -> str:
    """Fetch the best English transcript, preferring manual tracks over generated tracks."""
    if not video_id:
        raise NoTranscriptFound(video_id, TRANSCRIPT_LANGUAGES, [])

    transcript_list = transcript_api.list(video_id)

    try:
        transcript = transcript_list.find_manually_created_transcript(TRANSCRIPT_LANGUAGES)
    except NoTranscriptFound:
        transcript = transcript_list.find_generated_transcript(TRANSCRIPT_LANGUAGES)

    fetched_transcript = transcript.fetch(preserve_formatting=False)
    return format_transcript_text([snippet.text for snippet in fetched_transcript])


def format_transcript_text(segments: list[str]) -> str:
    """Join transcript snippets into readable Markdown paragraphs without timestamps."""
    cleaned_segments = [normalize_whitespace(segment) for segment in segments if normalize_whitespace(segment)]
    text = " ".join(cleaned_segments)
    sentences = re.split(r"(?<=[.!?])\s+", text)

    paragraphs: list[str] = []
    current: list[str] = []

    for sentence in sentences:
        if not sentence:
            continue

        current.append(sentence)
        if len(" ".join(current)) >= 500:
            paragraphs.append(" ".join(current))
            current = []

    if current:
        paragraphs.append(" ".join(current))

    return "\n\n".join(paragraphs)


def normalize_whitespace(value: str) -> str:
    """Collapse whitespace in transcript text."""
    return re.sub(r"\s+", " ", value).strip()


def build_markdown_filename(video: dict[str, Any]) -> str:
    """Build a readable Markdown filename that preserves ranking order."""
    rank = video_rank(video)
    title = str(video.get("title", "")).strip() or "Untitled Video"
    safe_title = sanitize_filename(truncate_title(title, MAX_FILENAME_TITLE_LENGTH))
    return f"{rank:02d} - {safe_title}.md"


def truncate_title(title: str, max_length: int) -> str:
    """Truncate long titles cleanly without cutting words in half."""
    if len(title) <= max_length:
        return title

    truncated = title[:max_length].rsplit(" ", 1)[0].strip()
    return truncated or title[:max_length].strip()


def sanitize_filename(filename: str) -> str:
    """Remove characters that are illegal in Windows/macOS/Linux filenames."""
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", filename)
    sanitized = re.sub(r"\s+", " ", sanitized).strip().rstrip(".")
    return sanitized or "Untitled Video"


def render_markdown(video: dict[str, Any], transcript_text: str) -> str:
    """Render a transcript Markdown document."""
    title = str(video.get("title", "")).strip() or "Untitled Video"
    channel_name = str(video.get("channel_name", "")).strip()

    return "\n".join(
        [
            f"# {title}",
            "",
            f"**Expert:** {channel_name}",
            "",
            f"**Rank:** {video.get('rank', '')}",
            "",
            f"**Metadata Score:** {video.get('score', '')}",
            "",
            f"**Published:** {format_published_date(str(video.get('published_at', '')))}",
            "",
            f"**Duration:** {video.get('duration', '')}",
            "",
            f"**Views:** {format_count(video.get('view_count'))}",
            "",
            f"**Likes:** {format_count(video.get('like_count'))}",
            "",
            f"**Video URL:** {video.get('video_url', '')}",
            "",
            "---",
            "",
            "# Transcript",
            "",
            transcript_text,
            "",
        ]
    )


def format_published_date(published_at: str) -> str:
    """Return YYYY-MM-DD from a YouTube timestamp."""
    if not published_at:
        return ""

    try:
        return datetime.fromisoformat(published_at.replace("Z", "+00:00")).astimezone(timezone.utc).date().isoformat()
    except ValueError:
        return published_at[:10]


def format_count(value: Any) -> str:
    """Format numeric counts for Markdown output."""
    converted = to_int_or_none(value)
    if converted is None:
        return ""
    return f"{converted:,}"


def to_int_or_none(value: Any) -> int | None:
    """Convert a value to int, returning None when missing or invalid."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def video_rank(video: dict[str, Any]) -> int:
    """Return a video's rank, falling back to zero if missing."""
    converted = to_int_or_none(video.get("rank"))
    return converted if converted is not None else 0


def print_header() -> None:
    """Print the transcript download header."""
    progress("============================================")
    progress("Downloading transcripts...")
    progress("============================================")
    progress("")


def print_summary(summary: DownloadSummary) -> None:
    """Print a concise transcript download summary."""
    progress("============================================")
    progress("")
    progress(f"Videos processed : {summary.videos_processed}")
    progress(f"Downloaded       : {summary.downloaded}")
    progress(f"Failed           : {summary.failed}")
    progress("")
    progress("Saved to:")
    progress("")
    progress(f"{display_path(summary.output_dir)}/")
    progress("")
    progress("============================================")


def display_path(path: Path) -> str:
    """Return a readable project-relative path when possible."""
    try:
        return path.relative_to(Path.cwd()).as_posix()
    except ValueError:
        return str(path)


def main() -> int:
    configure_console_encoding()
    load_env_files()

    try:
        videos = load_filtered_videos(INPUT_PATH)
        output_dir = get_output_dir(videos)
        print_header()
        results = download_transcripts(videos, output_dir)
        downloaded = sum(1 for result in results if result.downloaded)
        summary = DownloadSummary(
            videos_processed=len(results),
            downloaded=downloaded,
            failed=len(results) - downloaded,
            output_dir=output_dir,
        )
        print_summary(summary)
        return 0
    except TranscriptDownloadError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
