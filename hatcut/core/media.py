"""Media inspection via ffprobe."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .. import ffmpeg
from .errors import MediaNotFoundError, ProbeError


@dataclass(frozen=True)
class MediaInfo:
    """Everything the renderer needs to know about a source file."""

    path: Path
    duration: float
    has_video: bool
    has_audio: bool
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_codec: str = ""
    audio_codec: str = ""
    sample_rate: int = 0
    channels: int = 0
    rotation: int = 0
    size_bytes: int = 0
    container: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def is_image(self) -> bool:
        return self.has_video and self.duration <= 0.0

    @property
    def display_name(self) -> str:
        return self.path.name

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "name": self.path.name,
            "duration": self.duration,
            "has_video": self.has_video,
            "has_audio": self.has_audio,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "size_bytes": self.size_bytes,
            "container": self.container,
            "is_image": self.is_image,
        }


def _parse_fraction(value: str | None) -> float:
    """ffprobe reports frame rates as '30000/1001'. Tolerate junk."""
    if not value:
        return 0.0
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        return float(value)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _rotation_from(stream: dict) -> int:
    """Rotation is buried in tags or a display matrix side channel."""
    tags = stream.get("tags") or {}
    if "rotate" in tags:
        try:
            return int(float(tags["rotate"])) % 360
        except (TypeError, ValueError):
            pass
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            try:
                return int(float(side["rotation"])) % 360
            except (TypeError, ValueError):
                pass
    return 0


def probe(path: str | Path) -> MediaInfo:
    """Inspect a media file. Raises if the file is missing or unreadable."""
    path = Path(path).expanduser()
    if not path.exists():
        raise MediaNotFoundError(f"no such file: {path}")
    if not path.is_file():
        raise MediaNotFoundError(f"not a file: {path}")

    args = [
        ffmpeg.ffprobe_path(),
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        data = ffmpeg.run_json(args)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe returned unreadable output for {path}") from exc
    except Exception as exc:
        raise ProbeError(f"ffprobe failed on {path}: {exc}") from exc

    streams = data.get("streams") or []
    fmt = data.get("format") or {}

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    # An image has no meaningful stream duration; fall back to format duration.
    duration = 0.0
    for candidate in (
        (video or {}).get("duration"),
        fmt.get("duration"),
        (audio or {}).get("duration"),
    ):
        try:
            if candidate is not None and float(candidate) > 0:
                duration = float(candidate)
                break
        except (TypeError, ValueError):
            continue

    return MediaInfo(
        path=path,
        duration=duration,
        has_video=video is not None,
        has_audio=audio is not None,
        width=int((video or {}).get("width") or 0),
        height=int((video or {}).get("height") or 0),
        fps=_parse_fraction((video or {}).get("avg_frame_rate")
                            or (video or {}).get("r_frame_rate")),
        video_codec=(video or {}).get("codec_name", ""),
        audio_codec=(audio or {}).get("codec_name", ""),
        sample_rate=int((audio or {}).get("sample_rate") or 0),
        channels=int((audio or {}).get("channels") or 0),
        rotation=_rotation_from(video or {}),
        size_bytes=int(fmt.get("size") or 0),
        container=(fmt.get("format_name") or "").split(",")[0],
        raw=data,
    )


def probe_safe(path: str | Path) -> MediaInfo | None:
    """Probe, returning None instead of raising. For batch scans."""
    try:
        return probe(path)
    except Exception:
        return None


VIDEO_SUFFIXES = frozenset({
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpg", ".mpeg",
    ".wmv", ".flv", ".ts", ".m2ts", ".3gp", ".ogv", ".mxf",
})
AUDIO_SUFFIXES = frozenset({
    ".mp3", ".wav", ".aac", ".m4a", ".flac", ".ogg", ".opus", ".wma", ".aiff",
})
IMAGE_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif",
})
MEDIA_SUFFIXES = VIDEO_SUFFIXES | AUDIO_SUFFIXES | IMAGE_SUFFIXES


def is_media_file(path: str | Path) -> bool:
    return Path(path).suffix.lower() in MEDIA_SUFFIXES
