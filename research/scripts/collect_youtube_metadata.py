#!/usr/bin/env python3
"""Collect YouTube channel video metadata for a date range.

Usage:
    python collect_youtube_metadata.py "https://www.youtube.com/@OutboundSquad" 2026-01-01 2026-07-06

Set YOUTUBE_API_KEY or YOUTUBE_DATA_API_KEY in your environment or in a .env file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import urlopen


API_BASE_URL = "https://www.googleapis.com/youtube/v3"
OUTPUT_PATH = Path(__file__).resolve().parent / "data" / "youtube_metadata.json"
VIDEO_URL_TEMPLATE = "https://www.youtube.com/watch?v={video_id}"


class YouTubeMetadataError(Exception):
    """Raised for user-facing collection failures."""


@dataclass(frozen=True)
class DateRange:
    start_date: date
    end_date: date
    start_dt: datetime
    end_dt: datetime


def progress(message: str) -> None:
    """Print progress messages consistently and flush immediately."""
    print(message, flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect YouTube video metadata for a channel and date range."
    )
    parser.add_argument("channel_url", help="YouTube channel URL, e.g. https://www.youtube.com/@OutboundSquad")
    parser.add_argument("start_date", help="Start date in YYYY-MM-DD format")
    parser.add_argument("end_date", help="End date in YYYY-MM-DD format")
    return parser.parse_args()


def parse_date_range(start_value: str, end_value: str) -> DateRange:
    try:
        start_date = date.fromisoformat(start_value)
        end_date = date.fromisoformat(end_value)
    except ValueError as exc:
        raise YouTubeMetadataError("Dates must use YYYY-MM-DD format.") from exc

    if start_date > end_date:
        raise YouTubeMetadataError("Start date must be on or before end date.")

    return DateRange(
        start_date=start_date,
        end_date=end_date,
        start_dt=datetime.combine(start_date, time.min, tzinfo=timezone.utc),
        end_dt=datetime.combine(end_date, time.max, tzinfo=timezone.utc),
    )


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


def get_api_key() -> str:
    load_env_files()
    api_key = os.getenv("YOUTUBE_API_KEY") or os.getenv("YOUTUBE_DATA_API_KEY")
    if not api_key:
        raise YouTubeMetadataError(
            "Missing API key. Set YOUTUBE_API_KEY or YOUTUBE_DATA_API_KEY in your environment or .env file."
        )
    return api_key


def validate_channel_url(channel_url: str) -> str:
    parsed = urlparse(channel_url)
    if parsed.scheme not in {"http", "https"}:
        raise YouTubeMetadataError("Channel URL must start with http:// or https://.")

    host = parsed.netloc.lower().removeprefix("www.").removeprefix("m.")
    if host not in {"youtube.com", "youtube-nocookie.com"}:
        raise YouTubeMetadataError("Channel URL must be a youtube.com channel URL.")

    if not parsed.path.strip("/"):
        raise YouTubeMetadataError("Channel URL must include a channel path, handle, username, or custom URL.")

    return channel_url


def api_get(endpoint: str, params: dict[str, Any], api_key: str) -> dict[str, Any]:
    query = urlencode({**params, "key": api_key})
    url = f"{API_BASE_URL}/{endpoint}?{query}"

    try:
        with urlopen(url, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        message = extract_api_error_message(body) or exc.reason
        raise YouTubeMetadataError(f"YouTube API error ({exc.code}): {message}") from exc
    except URLError as exc:
        raise YouTubeMetadataError(f"Network error while calling YouTube API: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise YouTubeMetadataError("YouTube API returned an invalid JSON response.") from exc


def extract_api_error_message(response_body: str) -> str | None:
    try:
        payload = json.loads(response_body)
    except json.JSONDecodeError:
        return response_body.strip() or None

    error = payload.get("error", {})
    if isinstance(error, dict):
        return error.get("message")
    return None


def resolve_channel(channel_url: str, api_key: str) -> dict[str, str]:
    """Resolve a YouTube channel URL to ID, title, and uploads playlist ID."""
    parsed = urlparse(channel_url)
    path_parts = [part for part in parsed.path.split("/") if part]
    query = parse_qs(parsed.query)

    channel_payload: dict[str, Any] | None = None
    first_part = path_parts[0] if path_parts else ""

    if first_part == "channel" and len(path_parts) >= 2:
        channel_payload = get_channel_by_filter({"id": path_parts[1]}, api_key)
    elif first_part.startswith("@"):
        channel_payload = get_channel_by_filter({"forHandle": first_part}, api_key)
    elif first_part == "user" and len(path_parts) >= 2:
        channel_payload = get_channel_by_filter({"forUsername": path_parts[1]}, api_key)
    elif "channel_id" in query:
        channel_payload = get_channel_by_filter({"id": query["channel_id"][0]}, api_key)

    if channel_payload is None:
        search_term = path_parts[-1].lstrip("@") if path_parts else channel_url
        progress(f"Could not directly parse a channel ID/handle; searching for channel '{search_term}'...")
        channel_payload = search_channel(search_term, api_key)

    items = channel_payload.get("items", [])
    if not items:
        raise YouTubeMetadataError("Could not resolve the supplied URL to a YouTube channel.")

    channel = items[0]
    uploads_playlist_id = (
        channel.get("contentDetails", {})
        .get("relatedPlaylists", {})
        .get("uploads")
    )
    if not uploads_playlist_id:
        raise YouTubeMetadataError("Resolved channel does not expose an uploads playlist.")

    return {
        "id": channel["id"],
        "title": channel.get("snippet", {}).get("title", ""),
        "uploads_playlist_id": uploads_playlist_id,
    }


def get_channel_by_filter(filter_params: dict[str, str], api_key: str) -> dict[str, Any]:
    return api_get(
        "channels",
        {
            "part": "snippet,contentDetails",
            **filter_params,
            "maxResults": 1,
        },
        api_key,
    )


def search_channel(search_term: str, api_key: str) -> dict[str, Any]:
    search_payload = api_get(
        "search",
        {
            "part": "snippet",
            "type": "channel",
            "q": search_term,
            "maxResults": 1,
        },
        api_key,
    )

    items = search_payload.get("items", [])
    if not items:
        return {"items": []}

    channel_id = items[0].get("snippet", {}).get("channelId") or items[0].get("id", {}).get("channelId")
    if not channel_id:
        return {"items": []}

    return get_channel_by_filter({"id": channel_id}, api_key)


def collect_video_ids(uploads_playlist_id: str, date_range: DateRange, api_key: str) -> list[str]:
    video_ids: list[str] = []
    page_token: str | None = None
    page_count = 0
    saw_older_video = False

    while True:
        params: dict[str, Any] = {
            "part": "snippet,contentDetails",
            "playlistId": uploads_playlist_id,
            "maxResults": 50,
        }
        if page_token:
            params["pageToken"] = page_token

        page_count += 1
        progress(f"Scanning uploads page {page_count}...")
        payload = api_get("playlistItems", params, api_key)
        items = payload.get("items", [])

        for item in items:
            video_id = item.get("contentDetails", {}).get("videoId")
            published_at = item.get("contentDetails", {}).get("videoPublishedAt")
            published_dt = parse_youtube_datetime(published_at) if published_at else None

            if not video_id or published_dt is None:
                continue

            if date_range.start_dt <= published_dt <= date_range.end_dt:
                video_ids.append(video_id)
            elif published_dt < date_range.start_dt:
                saw_older_video = True

        progress(f"Found {len(video_ids)} matching videos so far.")

        page_token = payload.get("nextPageToken")
        if not page_token or saw_older_video:
            break

    return video_ids


def fetch_video_details(video_ids: list[str], channel_name: str, api_key: str) -> list[dict[str, Any]]:
    videos: list[dict[str, Any]] = []

    for batch_index, batch in enumerate(chunked(video_ids, 50), start=1):
        progress(f"Fetching video details batch {batch_index} ({len(batch)} videos)...")
        payload = api_get(
            "videos",
            {
                "part": "snippet,contentDetails,statistics",
                "id": ",".join(batch),
                "maxResults": 50,
            },
            api_key,
        )

        for item in payload.get("items", []):
            videos.append(format_video_metadata(item, channel_name))

    videos.sort(key=lambda video: video["published_at"])
    return videos


def format_video_metadata(item: dict[str, Any], channel_name: str) -> dict[str, Any]:
    video_id = item["id"]
    snippet = item.get("snippet", {})
    statistics = item.get("statistics", {})
    duration_iso8601 = item.get("contentDetails", {}).get("duration", "")

    return {
        "video_id": video_id,
        "title": snippet.get("title", ""),
        "description": snippet.get("description", ""),
        "published_at": snippet.get("publishedAt", ""),
        "duration": iso8601_duration_to_hms(duration_iso8601),
        "duration_iso8601": duration_iso8601,
        "video_url": VIDEO_URL_TEMPLATE.format(video_id=video_id),
        "thumbnail_url": best_thumbnail_url(snippet.get("thumbnails", {})),
        "view_count": to_int_or_none(statistics.get("viewCount")),
        "like_count": to_int_or_none(statistics.get("likeCount")),
        "channel_name": snippet.get("channelTitle") or channel_name,
        "tags": snippet.get("tags", []),
    }


def parse_youtube_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def iso8601_duration_to_hms(duration: str) -> str:
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?",
        duration,
    )
    if not match:
        return duration

    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0) + days * 24
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def best_thumbnail_url(thumbnails: dict[str, dict[str, Any]]) -> str | None:
    for quality in ("maxres", "standard", "high", "medium", "default"):
        thumbnail = thumbnails.get(quality)
        if thumbnail and thumbnail.get("url"):
            return thumbnail["url"]
    return None


def to_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def save_results(videos: list[dict[str, Any]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(videos, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    try:
        args = parse_args()
        channel_url = validate_channel_url(args.channel_url)
        date_range = parse_date_range(args.start_date, args.end_date)
        api_key = get_api_key()

        progress("Resolving channel URL...")
        channel = resolve_channel(channel_url, api_key)
        progress(f"Resolved channel: {channel['title']} ({channel['id']})")

        progress(
            f"Collecting videos published from {date_range.start_date.isoformat()} "
            f"through {date_range.end_date.isoformat()}..."
        )
        video_ids = collect_video_ids(channel["uploads_playlist_id"], date_range, api_key)
        progress(f"Found {len(video_ids)} videos in the requested date range.")

        videos = fetch_video_details(video_ids, channel["title"], api_key) if video_ids else []
        save_results(videos, OUTPUT_PATH)
        progress("✓ Metadata collection completed successfully.")
        progress(f"✓ {len(videos)} videos collected.")
        progress("✓ Output saved to:")
        progress(str(OUTPUT_PATH))
        progress("Ready for the filtering stage.")
        return 0
    except KeyboardInterrupt:
        print("\nStopped by user.", file=sys.stderr)
        return 130
    except YouTubeMetadataError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
