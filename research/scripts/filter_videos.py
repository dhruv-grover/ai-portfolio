#!/usr/bin/env python3
"""Filter YouTube metadata down to the most relevant research videos."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MIN_DURATION_SECONDS = 180
TOP_VIDEOS = 15

TITLE_WEIGHT = 3
TAG_WEIGHT = 2
DESCRIPTION_WEIGHT = 1

KEYWORD_WEIGHTS = {
    "cold outreach": 12,
    "b2b saas": 12,
    "outbound": 10,
    "pipeline": 10,
    "sales pipeline": 9,
    "prospecting": 9,
    "sdr": 8,
    "sales development": 8,
    "icp": 8,
    "ideal customer profile": 8,
    "personalization": 8,
    "cold email": 7,
    "cold call": 7,
    "sales email": 6,
    "email sequence": 6,
    "sequence": 5,
    "cadence": 5,
    "targeting": 5,
    "messaging": 5,
    "meetings": 5,
    "book meetings": 5,
    "lead generation": 5,
    "sales leader": 4,
    "sales team": 4,
    "saas": 4,
}

KEYWORD_PATTERNS = {
    keyword: re.compile(rf"(?<!\w){re.escape(keyword)}(?!\w)", re.IGNORECASE)
    for keyword in KEYWORD_WEIGHTS
}

BONUS_RULES = [
    ("cold outreach + b2b saas", ("cold outreach", "b2b saas"), 15),
    ("outbound + pipeline", ("outbound", "pipeline"), 12),
    ("SDR + prospecting", ("sdr", "prospecting"), 10),
    ("ICP + personalization", ("icp", "personalization"), 10),
]

INPUT_PATH = Path(__file__).resolve().parent / "data" / "youtube_metadata.json"
OUTPUT_PATH = Path(__file__).resolve().parent / "data" / "filtered_videos.json"


class VideoFilterError(Exception):
    """Raised for user-facing filtering failures."""


@dataclass(frozen=True)
class FilterSummary:
    videos_loaded: int
    removed_short: int
    removed_missing_metadata: int
    remaining_after_filtering: int
    top_videos_selected: int


def progress(message: str) -> None:
    """Print progress messages consistently and flush immediately."""
    print(message, flush=True)


def load_metadata(input_path: Path) -> list[dict[str, Any]]:
    """Load source YouTube metadata from disk."""
    if not input_path.exists():
        raise VideoFilterError(f"Input file does not exist: {input_path}")

    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise VideoFilterError(f"Input file contains malformed JSON: {exc}") from exc

    if not isinstance(payload, list):
        raise VideoFilterError("Input JSON must contain a list of video objects.")

    return [item for item in payload if isinstance(item, dict)]


def filter_and_score_videos(videos: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], FilterSummary]:
    """Apply hard filters, score relevance, and return the top ranked videos."""
    removed_short = 0
    removed_missing_metadata = 0
    scored_videos: list[dict[str, Any]] = []

    for video in videos:
        duration_seconds = get_duration_seconds(video)
        if duration_seconds < MIN_DURATION_SECONDS:
            removed_short += 1
            continue

        if not has_required_metadata(video):
            removed_missing_metadata += 1
            continue

        scored_videos.append(score_video(video))

    ranked_videos = sorted(scored_videos, key=ranking_key, reverse=True)
    selected_videos = ranked_videos[:TOP_VIDEOS]
    output_videos = [
        format_output_video(video, rank)
        for rank, video in enumerate(selected_videos, start=1)
    ]

    return output_videos, FilterSummary(
        videos_loaded=len(videos),
        removed_short=removed_short,
        removed_missing_metadata=removed_missing_metadata,
        remaining_after_filtering=len(scored_videos),
        top_videos_selected=len(output_videos),
    )


def has_required_metadata(video: dict[str, Any]) -> bool:
    """Return True when title and description are both present."""
    return bool(str(video.get("title", "")).strip()) and bool(str(video.get("description", "")).strip())


def get_duration_seconds(video: dict[str, Any]) -> int:
    """Read duration from ISO 8601 when available, otherwise from HH:MM:SS."""
    duration_iso8601 = str(video.get("duration_iso8601", "")).strip()
    if duration_iso8601:
        return parse_iso8601_duration(duration_iso8601)

    duration = str(video.get("duration", "")).strip()
    return parse_hms_duration(duration)


def parse_iso8601_duration(duration: str) -> int:
    """Parse a YouTube ISO 8601 duration into seconds."""
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?",
        duration,
    )
    if not match:
        return 0

    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def parse_hms_duration(duration: str) -> int:
    """Parse HH:MM:SS or MM:SS duration text into seconds."""
    parts = duration.split(":")
    if not all(part.isdigit() for part in parts):
        return 0

    if len(parts) == 3:
        hours, minutes, seconds = (int(part) for part in parts)
        return hours * 3600 + minutes * 60 + seconds

    if len(parts) == 2:
        minutes, seconds = (int(part) for part in parts)
        return minutes * 60 + seconds

    return 0


def score_video(video: dict[str, Any]) -> dict[str, Any]:
    """Attach relevance score details to a video object."""
    title_text = str(video.get("title", ""))
    description_text = str(video.get("description", ""))
    tags_text = " ".join(str(tag) for tag in video.get("tags", []) if tag is not None)

    title_matches = matched_keywords_for_text(title_text)
    tag_matches = matched_keywords_for_text(tags_text)
    description_matches = matched_keywords_for_text(description_text)

    score = 0
    score += score_matches(title_matches, TITLE_WEIGHT)
    score += score_matches(tag_matches, TAG_WEIGHT)
    score += score_matches(description_matches, DESCRIPTION_WEIGHT)

    combined_matches = title_matches | tag_matches | description_matches
    bonus_score, matched_bonus_rules = score_bonus_rules(combined_matches)
    score += bonus_score

    enriched_video = dict(video)
    enriched_video["score"] = score
    enriched_video["matched_keywords"] = sorted(combined_matches)
    enriched_video["matched_bonus_rules"] = matched_bonus_rules
    return enriched_video


def matched_keywords_for_text(text: str) -> set[str]:
    """Return weighted keywords found in text, counting each keyword once."""
    return {
        keyword
        for keyword, pattern in KEYWORD_PATTERNS.items()
        if pattern.search(text)
    }


def score_matches(matches: set[str], field_weight: int) -> int:
    """Score keyword matches for a single field."""
    return sum(KEYWORD_WEIGHTS[keyword] * field_weight for keyword in matches)


def score_bonus_rules(matches: set[str]) -> tuple[int, list[str]]:
    """Award bonus points when important concepts appear together."""
    score = 0
    matched_rules: list[str] = []

    for label, keywords, bonus in BONUS_RULES:
        if all(keyword in matches for keyword in keywords):
            score += bonus
            matched_rules.append(label)

    return score, matched_rules


def ranking_key(video: dict[str, Any]) -> tuple[int, int, datetime]:
    """Sort by score, then views, then newest publish timestamp."""
    return (
        int(video.get("score") or 0),
        to_int_or_zero(video.get("view_count")),
        parse_published_at(str(video.get("published_at", ""))),
    )


def parse_published_at(value: str) -> datetime:
    """Parse a YouTube timestamp for deterministic ranking."""
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def format_output_video(video: dict[str, Any], rank: int) -> dict[str, Any]:
    """Return the compact output structure used by downstream scripts."""
    return {
        "rank": rank,
        "video_id": video.get("video_id", ""),
        "title": video.get("title", ""),
        "channel_name": video.get("channel_name", ""),
        "published_at": video.get("published_at", ""),
        "duration": video.get("duration", ""),
        "video_url": video.get("video_url", ""),
        "score": video.get("score", 0),
        "matched_keywords": video.get("matched_keywords", []),
        "matched_bonus_rules": video.get("matched_bonus_rules", []),
        "view_count": to_int_or_none(video.get("view_count")),
        "like_count": to_int_or_none(video.get("like_count")),
    }


def to_int_or_zero(value: Any) -> int:
    """Convert a value to int, returning zero when missing or invalid."""
    converted = to_int_or_none(value)
    return converted if converted is not None else 0


def to_int_or_none(value: Any) -> int | None:
    """Convert a value to int, returning None when missing or invalid."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def save_results(videos: list[dict[str, Any]], output_path: Path) -> None:
    """Save filtered video metadata to disk."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(videos, indent=2, ensure_ascii=False), encoding="utf-8")


def print_summary(summary: FilterSummary, output_path: Path) -> None:
    """Print a concise processing summary."""
    progress(f"Videos loaded: {summary.videos_loaded}")
    progress(f"Removed (<3 min): {summary.removed_short}")
    progress(f"Removed (missing metadata): {summary.removed_missing_metadata}")
    progress(f"Remaining after filtering: {summary.remaining_after_filtering}")
    progress(f"Top videos selected: {summary.top_videos_selected}")
    progress("Output saved to:")
    progress(display_path(output_path))


def print_keyword_summary(videos: list[dict[str, Any]]) -> None:
    """Print keyword match frequencies across the selected videos."""
    keyword_counts = {
        keyword: sum(1 for video in videos if keyword in video.get("matched_keywords", []))
        for keyword in KEYWORD_WEIGHTS
    }
    matched_counts = {
        keyword: count
        for keyword, count in keyword_counts.items()
        if count > 0
    }

    if not matched_counts:
        return

    sorted_counts = sorted(matched_counts.items(), key=lambda item: (-item[1], item[0].casefold()))
    label_width = max(len(keyword) for keyword in matched_counts)

    progress("=====================================")
    progress("Keyword Match Summary")
    progress("-------------------------------------")
    for keyword, count in sorted_counts:
        progress(f"{keyword:<{label_width}} : {count}")
    progress("=====================================")


def print_selected_videos(videos: list[dict[str, Any]]) -> None:
    """Print selected videos in ranked order."""
    if not videos:
        return

    progress("=====================================")
    progress("Top Selected Videos")
    progress("=====================================")
    progress("")

    for rank, video in enumerate(videos, start=1):
        progress(f"{rank}. {video.get('title', '')}")
        progress(f"   Score        : {video.get('score', 0)}")
        progress(f"   Upload Date  : {format_upload_date(str(video.get('published_at', '')))}")
        progress(f"   Duration     : {video.get('duration', '')}")
        progress(f"   Views        : {format_count(video.get('view_count'))}")
        progress("")

    progress("=====================================")


def format_upload_date(published_at: str) -> str:
    """Return the YYYY-MM-DD portion of a YouTube timestamp."""
    parsed_date = parse_published_at(published_at)
    if parsed_date == datetime.min.replace(tzinfo=timezone.utc):
        return ""
    return parsed_date.date().isoformat()


def format_count(value: Any) -> str:
    """Format numeric counts for console output."""
    converted = to_int_or_none(value)
    if converted is None:
        return ""
    return f"{converted:,}"


def display_path(path: Path) -> str:
    """Return a readable project-relative path when possible."""
    try:
        return path.relative_to(Path.cwd()).as_posix()
    except ValueError:
        return str(path)


def main() -> int:
    try:
        videos = load_metadata(INPUT_PATH)
        filtered_videos, summary = filter_and_score_videos(videos)
        save_results(filtered_videos, OUTPUT_PATH)
        print_summary(summary, OUTPUT_PATH)
        print_keyword_summary(filtered_videos)
        print_selected_videos(filtered_videos)
        return 0
    except VideoFilterError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
