"""Subtitle import and export for Hatcut (SRT and WebVTT).

Zero dependencies. Parses and serializes standard SubRip (.srt) and WebVTT (.vtt)
subtitles into Hatcut text clips with accurate millisecond timings.
"""

from __future__ import annotations

import re
from typing import Sequence

from .timeline import Clip, TextStyle, new_id


def parse_timestamp(ts: str) -> float:
    """Parse an SRT (00:01:23,456) or VTT (00:01:23.456 or 01:23.456) timestamp into seconds."""
    ts = ts.strip().replace(",", ".")
    parts = ts.split(":")
    if len(parts) == 3:
        hours = float(parts[0])
        minutes = float(parts[1])
        seconds = float(parts[2])
        return hours * 3600.0 + minutes * 60.0 + seconds
    elif len(parts) == 2:
        minutes = float(parts[0])
        seconds = float(parts[1])
        return minutes * 60.0 + seconds
    else:
        return float(ts)


def format_srt_timestamp(seconds: float) -> str:
    """Format seconds into SRT timestamp: HH:MM:SS,mmm"""
    seconds = max(0.0, seconds)
    total_ms = int(round(seconds * 1000.0))
    ms = total_ms % 1000
    total_sec = total_ms // 1000
    sec = total_sec % 60
    total_min = total_sec // 60
    minutes = total_min % 60
    hours = total_min // 60
    return f"{hours:02d}:{minutes:02d}:{sec:02d},{ms:03d}"


def format_vtt_timestamp(seconds: float) -> str:
    """Format seconds into WebVTT timestamp: HH:MM:SS.mmm"""
    return format_srt_timestamp(seconds).replace(",", ".")


def parse_subtitles(content: str) -> list[dict]:
    """Parse SRT or WebVTT content into a list of cue dictionaries.

    Returns:
        [{"start": float, "end": float, "duration": float, "text": str}, ...]
    """
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cues: list[dict] = []
    
    # Regex matching: 00:00:01,000 --> 00:00:04,000 or 00:01.000 --> 00:04.000
    time_pattern = re.compile(
        r"((?:\d{1,2}:)?\d{2}:\d{2}[,\.]\d{3})\s*-->\s*((?:\d{1,2}:)?\d{2}:\d{2}[,\.]\d{3})"
    )

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].strip()
        # Skip WebVTT header or empty lines or standalone cue numbers
        if not line or line.upper().startswith("WEBVTT") or line.startswith("NOTE"):
            i += 1
            continue

        match = time_pattern.search(line)
        if not match:
            # Might be cue number on line i, and timestamp on line i+1
            if i + 1 < n:
                match = time_pattern.search(lines[i + 1].strip())
                if match:
                    i += 1

        if match:
            start = parse_timestamp(match.group(1))
            end = parse_timestamp(match.group(2))
            duration = max(0.05, end - start)
            
            i += 1
            text_lines = []
            while i < n and lines[i].strip():
                text_lines.append(lines[i].strip())
                i += 1
            
            text_content = "\n".join(text_lines)
            # Remove basic HTML formatting tags (<b>, <i>, <font>, etc.) if present
            clean_text = re.sub(r"<[^>]+>", "", text_content).strip()
            if clean_text:
                cues.append({
                    "start": round(start, 4),
                    "end": round(end, 4),
                    "duration": round(duration, 4),
                    "text": clean_text,
                })
        else:
            i += 1

    return cues


def cues_to_clips(cues: list[dict], *, font_size: int = 48, y: str = "h-th-60") -> list[Clip]:
    """Convert parsed cues to Hatcut Clip instances with TextStyle."""
    clips = []
    for cue in cues:
        style = TextStyle(
            content=cue["text"],
            font_size=font_size,
            color="white",
            x="(w-text_w)/2",
            y=y,
            box=True,
            box_color="black@0.6",
            box_border=8,
        )
        clip = Clip(
            id=new_id("sub"),
            start=cue["start"],
            duration=cue["duration"],
            name=f"Sub: {cue['text'][:20]}",
            text=style,
        )
        clips.append(clip)
    return clips


def export_srt(clips: Sequence[Clip]) -> str:
    """Export text clips as standard SubRip (.srt) string."""
    text_clips = [c for c in clips if c.text and c.text.content.strip()]
    sorted_clips = sorted(text_clips, key=lambda c: c.start)

    blocks: list[str] = []
    for idx, clip in enumerate(sorted_clips, start=1):
        start_ts = format_srt_timestamp(clip.start)
        end_ts = format_srt_timestamp(clip.end)
        content = clip.text.content.strip()
        blocks.append(f"{idx}\n{start_ts} --> {end_ts}\n{content}\n")

    return "\n".join(blocks)


def export_vtt(clips: Sequence[Clip]) -> str:
    """Export text clips as standard WebVTT (.vtt) string."""
    text_clips = [c for c in clips if c.text and c.text.content.strip()]
    sorted_clips = sorted(text_clips, key=lambda c: c.start)

    lines: list[str] = ["WEBVTT\n"]
    for idx, clip in enumerate(sorted_clips, start=1):
        start_ts = format_vtt_timestamp(clip.start)
        end_ts = format_vtt_timestamp(clip.end)
        content = clip.text.content.strip()
        lines.append(f"{idx}\n{start_ts} --> {end_ts}\n{content}\n")

    return "\n".join(lines)
