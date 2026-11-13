"""The ``.hatcut`` project file.

The format is plain JSON. That is a deliberate choice: an edit is small, text
diffs are free, and you can hand-edit a project or generate one from a script
without linking anything. Media is referenced by path, never copied in, so a
project file stays tiny next to the gigabytes it points at.

Writing is atomic -- we write a sibling temp file and replace -- because losing
an edit to a half-finished save is the one bug users never forgive.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import media
from .errors import ProjectError
from .timeline import Clip, Timeline

#: Bumped whenever the on-disk shape changes incompatibly.
FORMAT_VERSION = 1
PROJECT_SUFFIX = ".hatcut"


def to_relative_path(path_str: str | None, base_dir: Path | None) -> str | None:
    """Convert an absolute media path to a path relative to base_dir, if possible."""
    if not path_str or base_dir is None:
        return path_str
    try:
        abs_p = Path(path_str).resolve()
        base = base_dir.resolve()
        rel = os.path.relpath(abs_p, base)
        return rel.replace("\\", "/")
    except (ValueError, OSError):
        return path_str


def resolve_relative_path(path_str: str | None, base_dir: Path | None) -> str | None:
    """Resolve a relative or absolute path against base_dir."""
    if not path_str or base_dir is None:
        return path_str
    p = Path(path_str)
    if p.is_absolute():
        return str(p)
    try:
        return str((base_dir / p).resolve())
    except (ValueError, OSError):
        return path_str


@dataclass
class Project:
    """A timeline plus the metadata that travels with it."""

    name: str = "Untitled"
    timeline: Timeline = field(default_factory=Timeline)
    path: Path | None = None
    description: str = ""
    #: Sources are cached here so the CLI can report missing media without
    #: re-probing every clip.
    media_cache: dict[str, dict] = field(default_factory=dict)

    # -- lifecycle -------------------------------------------------------

    @classmethod
    def new(cls, name: str = "Untitled", *, width: int = 1920,
            height: int = 1080, fps: float = 30.0) -> "Project":
        project = cls(name=name)
        project.timeline.width = width
        project.timeline.height = height
        project.timeline.fps = fps
        project.timeline.add_track("video", "V1")
        project.timeline.add_track("audio", "A1")
        return project

    # -- media -----------------------------------------------------------

    def register_media(self, path: str | Path) -> media.MediaInfo:
        """Probe a file and remember it. Raises if it cannot be read."""
        info = media.probe(path)
        self.media_cache[str(info.path)] = info.to_dict()
        return info

    def missing_media(self) -> list[str]:
        """Sources referenced by clips that are no longer on disk."""
        missing = []
        for clip in self.timeline.iter_clips():
            if clip.source and not Path(clip.source).exists():
                missing.append(clip.source)
        return sorted(set(missing))

    def media_duration(self, source: str) -> float | None:
        """Best-known duration of a source, preferring a live probe."""
        info = media.probe_safe(source)
        if info is not None:
            self.media_cache[str(info.path)] = info.to_dict()
            return info.duration
        cached = self.media_cache.get(source)
        return float(cached["duration"]) if cached else None

    def add_media(self, path: str | Path, *, start: float | None = None,
                  in_point: float = 0.0, duration: float | None = None,
                  with_audio: bool = True, track_id: str | None = None,
                  speed: float = 1.0) -> tuple[Clip | None, Clip | None]:
        """Import a file onto the timeline, linking its picture and sound.

        Returns ``(video_clip, audio_clip)``; either may be ``None`` when the
        source does not carry that stream, or when ``with_audio`` is off.

        Video and audio live on separate tracks by design -- that is what makes
        it possible to replace a shot's audio, or mute a track, without
        disturbing the picture. This helper keeps the two in step at import
        time so the common case stays one call.
        """
        from .timeline import Clip, TextStyle

        info = self.register_media(path)

        if not info.has_video and not info.has_audio:
            raise ProjectError(f"{info.path.name} has neither a video nor an audio stream")

        # A still has no intrinsic duration; give it a sane default.
        if duration is None:
            duration = info.duration if info.duration > 0 else 5.0
        if info.duration > 0:
            duration = min(duration, max(0.0, info.duration - in_point) / speed)
            if duration <= 0:
                raise ProjectError(
                    f"in_point {in_point:.2f}s is past the end of "
                    f"{info.path.name} ({info.duration:.2f}s)"
                )

        video_clip = audio_clip = None
        source = str(info.path)
        # One link id shared by the pair, so a transition set on the picture
        # also lands on the sound.
        link_id = f"link_{uuid.uuid4().hex[:10]}"

        if info.has_video:
            video_clip = Clip(
                source=source, start=start if start is not None else -1.0,
                duration=duration, in_point=in_point, speed=speed,
                name=info.path.name, link_id=link_id,
            )
            self.timeline.add_clip(video_clip, track_id=track_id,
                                   track_kind="video")

        if with_audio and info.has_audio:
            audio_start = video_clip.start if video_clip else (
                start if start is not None else 0.0)
            audio_clip = Clip(
                source=source, start=audio_start, duration=duration,
                in_point=in_point, speed=speed, name=info.path.name,
                link_id=link_id,
            )
            self.timeline.add_clip(audio_clip, track_kind="audio")

        return video_clip, audio_clip

    # -- validation ------------------------------------------------------

    def validate(self) -> list[str]:
        problems = list(self.timeline.validate())
        problems.extend(f"missing media: {p}" for p in self.missing_media())
        return problems

    # -- serialisation ---------------------------------------------------

    def to_dict(self, base_dir: Path | None = None) -> dict:
        base = base_dir if base_dir is not None else (self.path.parent if self.path else None)
        t_dict = self.timeline.to_dict()
        if base is not None:
            # Map clips to relative paths
            for track in t_dict.get("tracks", []):
                for clip in track.get("clips", []):
                    if clip.get("source"):
                        clip["source"] = to_relative_path(clip["source"], base)

            # Map media_cache
            rel_cache = {}
            for k, v in self.media_cache.items():
                rel_k = to_relative_path(k, base)
                item = dict(v) if isinstance(v, dict) else v
                if isinstance(item, dict) and "path" in item:
                    item["path"] = to_relative_path(str(item["path"]), base)
                rel_cache[rel_k] = item
            media_cache = rel_cache
        else:
            media_cache = self.media_cache

        return {
            "format": "hatcut",
            "version": FORMAT_VERSION,
            "name": self.name,
            "description": self.description,
            **t_dict,
            "media_cache": media_cache,
        }

    @classmethod
    def from_dict(cls, data: dict, path: Path | None = None) -> "Project":
        if not isinstance(data, dict):
            raise ProjectError("project file must contain a JSON object")

        fmt = data.get("format", "hatcut")
        if fmt != "hatcut":
            raise ProjectError(f"not a Hatcut project (format={fmt!r})")

        version = int(data.get("version", 0))
        if version > FORMAT_VERSION:
            raise ProjectError(
                f"project was written by a newer Hatcut (format version "
                f"{version}, this build understands {FORMAT_VERSION})"
            )

        base_dir = path.parent.resolve() if path else None
        timeline_data = dict(data)
        if base_dir is not None:
            tracks = []
            for t in data.get("tracks") or []:
                t_copy = dict(t)
                clips = []
                for c in t.get("clips") or []:
                    c_copy = dict(c)
                    if c_copy.get("source"):
                        c_copy["source"] = resolve_relative_path(c_copy["source"], base_dir)
                    clips.append(c_copy)
                t_copy["clips"] = clips
                tracks.append(t_copy)
            timeline_data["tracks"] = tracks

            cached_media = {}
            for k, v in (data.get("media_cache") or {}).items():
                resolved_k = resolve_relative_path(k, base_dir)
                item = dict(v) if isinstance(v, dict) else v
                if isinstance(item, dict) and "path" in item:
                    item["path"] = resolve_relative_path(str(item["path"]), base_dir)
                cached_media[resolved_k] = item
        else:
            cached_media = dict(data.get("media_cache") or {})

        return cls(
            name=data.get("name", "Untitled"),
            description=data.get("description", ""),
            timeline=Timeline.from_dict(timeline_data),
            media_cache=cached_media,
            path=path,
        )

    # -- disk ------------------------------------------------------------

    def save(self, path: str | Path | None = None) -> Path:
        """Write the project atomically. Returns the path written."""
        target = Path(path).expanduser() if path else self.path
        if target is None:
            raise ProjectError("no path given and the project has never been saved")
        if target.suffix != PROJECT_SUFFIX:
            target = target.with_suffix(PROJECT_SUFFIX)

        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(base_dir=target.parent), indent=2, ensure_ascii=False)

        # Write beside the target so os.replace stays on one filesystem.
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", delete=False,
            dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp",
        )
        try:
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, target)
        except Exception:
            Path(handle.name).unlink(missing_ok=True)
            raise

        self.path = target
        return target

    @classmethod
    def load(cls, path: str | Path) -> "Project":
        target = Path(path).expanduser()
        if not target.exists():
            raise ProjectError(f"no such project: {target}")
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ProjectError(f"{target} is not valid JSON: {exc}") from exc
        return cls.from_dict(data, path=target)

    # -- convenience -----------------------------------------------------

    def summary(self) -> str:
        clips = sum(len(t.clips) for t in self.timeline.tracks)
        duration = self.timeline.duration
        minutes, seconds = divmod(duration, 60)
        return (
            f"{self.name}: {len(self.timeline.tracks)} tracks, {clips} clips, "
            f"{int(minutes)}m{seconds:04.1f}s, "
            f"{self.timeline.width}x{self.timeline.height} @ {self.timeline.fps:g}fps"
        )
