"""The timeline domain model.

A project is a stack of tracks; a track is an ordered list of clips placed at
absolute times. Gaps between clips are legal and are rendered as filler, which
is what makes trimming the tail of a clip behave the way an editor should.

Two time bases are in play and the distinction matters everywhere:

* **source time** -- seconds into the original file (``in_point``)
* **timeline time** -- seconds along the sequence (``start``, ``duration``)

With ``speed == 1.0`` they advance together. At ``speed == 2.0`` a clip covering
4 seconds of source occupies only 2 seconds of timeline, so
``source_span == duration * speed``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Literal

from .errors import TimelineError

TrackKind = Literal["video", "audio", "text"]

#: Clips shorter than this are treated as accidental and rejected.
MIN_CLIP_DURATION = 0.02

VALID_TRACK_KINDS: tuple[str, ...] = ("video", "audio", "text")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _round(value: float) -> float:
    """Keep project files readable and diffable -- microsecond precision."""
    return round(float(value), 6)


@dataclass
class Marker:
    """A timestamp marker placed on the timeline ruler."""

    time: float
    id: str = field(default_factory=lambda: new_id("marker"))
    name: str = ""
    color: str = "#3b82f6"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "time": _round(self.time),
            "name": self.name,
            "color": self.color,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Marker":
        return cls(
            id=data.get("id") or new_id("marker"),
            time=float(data.get("time", 0.0)),
            name=data.get("name", ""),
            color=data.get("color", "#3b82f6"),
        )


@dataclass
class Transform:
    """Geometry applied to a clip before it joins its track."""

    x: int = 0
    y: int = 0
    scale: float = 1.0
    rotate: float = 0.0
    opacity: float = 1.0
    fit: Literal["contain", "cover", "stretch"] = "contain"

    def to_dict(self) -> dict:
        return {
            "x": self.x, "y": self.y, "scale": self.scale,
            "rotate": self.rotate, "opacity": self.opacity, "fit": self.fit,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "Transform":
        data = data or {}
        return cls(
            x=int(data.get("x", 0)),
            y=int(data.get("y", 0)),
            scale=float(data.get("scale", 1.0)),
            rotate=float(data.get("rotate", 0.0)),
            opacity=float(data.get("opacity", 1.0)),
            fit=data.get("fit", "contain"),
        )


@dataclass
class TextStyle:
    """Parameters for a text-track clip."""

    content: str = "Text"
    font_size: int = 64
    color: str = "white"
    font_file: str = ""
    x: str = "(w-text_w)/2"
    y: str = "(h-text_h)/2"
    box: bool = False
    box_color: str = "black@0.5"
    box_border: int = 12

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: dict | None) -> "TextStyle":
        data = data or {}
        known = {k: v for k, v in data.items() if k in cls.__annotations__}
        return cls(**known)


@dataclass
class Clip:
    """One piece of media on a track."""

    source: str = ""
    start: float = 0.0
    duration: float = 1.0
    in_point: float = 0.0
    speed: float = 1.0
    volume: float = 1.0
    id: str = field(default_factory=lambda: new_id("clip"))
    name: str = ""
    enabled: bool = True
    effects: list[dict] = field(default_factory=list)
    transform: Transform = field(default_factory=Transform)
    transition: dict | None = None
    text: TextStyle | None = None
    link_id: str = ""
    blend_mode: str = "normal"
    transform_keyframes: list[dict] = field(default_factory=list)
    """Ties a picture clip to the sound clip imported alongside it.

    Linked clips stay independent for editing -- you can retime one without the
    other -- but a transition set on the picture is applied to the sound too.
    Without that, a cross-dissolve lands on a hard audio cut and the two drift
    apart by the length of the transition.
    """

    # -- derived ---------------------------------------------------------

    @property
    def end(self) -> float:
        return _round(self.start + self.duration)

    @property
    def source_span(self) -> float:
        """How much of the source file this clip consumes."""
        return _round(self.duration * self.speed)

    @property
    def out_point(self) -> float:
        return _round(self.in_point + self.source_span)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        label = self.name or self.source or "gap"
        return f"<Clip {self.id} {label!r} @{self.start:.2f}+{self.duration:.2f}>"

    # -- serialisation ---------------------------------------------------

    def to_dict(self) -> dict:
        data: dict[str, Any] = {
            "id": self.id,
            "source": self.source,
            "start": _round(self.start),
            "duration": _round(self.duration),
            "in_point": _round(self.in_point),
            "speed": self.speed,
            "volume": self.volume,
            "enabled": self.enabled,
        }
        if self.name:
            data["name"] = self.name
        if self.effects:
            data["effects"] = self.effects
        if self.transform != Transform():
            data["transform"] = self.transform.to_dict()
        if self.transition:
            data["transition"] = self.transition
        if self.text:
            data["text"] = self.text.to_dict()
        if self.link_id:
            data["link_id"] = self.link_id
        if self.blend_mode and self.blend_mode != "normal":
            data["blend_mode"] = self.blend_mode
        if self.transform_keyframes:
            data["transform_keyframes"] = self.transform_keyframes
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Clip":
        try:
            duration = float(data["duration"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TimelineError(f"clip is missing a valid duration: {data!r}") from exc

        return cls(
            id=data.get("id") or new_id("clip"),
            source=data.get("source", ""),
            start=float(data.get("start", 0.0)),
            duration=duration,
            in_point=float(data.get("in_point", 0.0)),
            speed=max(float(data.get("speed", 1.0)), 0.01),
            volume=float(data.get("volume", 1.0)),
            name=data.get("name", ""),
            enabled=bool(data.get("enabled", True)),
            effects=list(data.get("effects") or []),
            transform=Transform.from_dict(data.get("transform")),
            transition=data.get("transition"),
            text=TextStyle.from_dict(data["text"]) if data.get("text") else None,
            link_id=data.get("link_id", ""),
            blend_mode=data.get("blend_mode", "normal"),
            transform_keyframes=list(data.get("transform_keyframes") or []),
        )


@dataclass
class Track:
    """A single lane of clips."""

    kind: TrackKind = "video"
    id: str = field(default_factory=lambda: new_id("track"))
    name: str = ""
    clips: list[Clip] = field(default_factory=list)
    muted: bool = False
    locked: bool = False
    blend_mode: str = "normal"
    ducking: bool = False
    duck_threshold: float = 0.1
    duck_ratio: float = 4.0
    duck_attack: float = 50.0
    duck_release: float = 300.0

    def __post_init__(self) -> None:
        if self.kind not in VALID_TRACK_KINDS:
            raise TimelineError(
                f"unknown track kind {self.kind!r}; expected one of {VALID_TRACK_KINDS}"
            )

    # -- queries ---------------------------------------------------------

    def sorted_clips(self) -> list[Clip]:
        return sorted(self.clips, key=lambda c: (c.start, c.id))

    def clip(self, clip_id: str) -> Clip:
        for candidate in self.clips:
            if candidate.id == clip_id:
                return candidate
        raise TimelineError(f"no clip {clip_id!r} on track {self.id!r}")

    def find_clip(self, clip_id: str) -> Clip | None:
        return next((c for c in self.clips if c.id == clip_id), None)

    @property
    def duration(self) -> float:
        clips = self.sorted_clips()
        if not clips:
            return 0.0
        total = 0.0
        cursor = 0.0
        for i, clip in enumerate(clips):
            if clip.start > cursor:
                total += clip.start - cursor
            clip_dur = clip.duration
            trans = clip.transition
            trans_type = trans.get("type") if isinstance(trans, dict) else None
            if i > 0 and trans and trans_type != "none" and abs(clip.start - cursor) < 1e-4:
                trans_dur = float(trans.get("duration", 0.0))
                limit = min(clips[i - 1].duration, clip_dur) * 0.98
                overlap = min(trans_dur, limit)
                clip_dur -= overlap
            total += clip_dur
            cursor = clip.end
        return _round(total)

    def next_free_start(self) -> float:
        """Where a clip appended to this track should land."""
        return _round(max((c.end for c in self.clips), default=0.0))

    def clip_at(self, time: float) -> Clip | None:
        for candidate in self.sorted_clips():
            if candidate.start <= time < candidate.end:
                return candidate
        return None

    def overlaps(self, start: float, duration: float,
                 ignore_id: str | None = None) -> Clip | None:
        end = start + duration
        for candidate in self.sorted_clips():
            if candidate.id == ignore_id:
                continue
            if start < candidate.end and candidate.start < end:
                return candidate
        return None

    def iter_with_gaps(self) -> Iterator[Clip | None]:
        """Yield clips and ``None`` placeholders for the gaps between them."""
        cursor = 0.0
        for clip in self.sorted_clips():
            if clip.start > cursor + 1e-6:
                yield None  # gap of clip.start - cursor
            yield clip
            cursor = clip.end

    def to_dict(self) -> dict:
        data: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "muted": self.muted,
            "locked": self.locked,
            "clips": [c.to_dict() for c in self.sorted_clips()],
        }
        if self.blend_mode and self.blend_mode != "normal":
            data["blend_mode"] = self.blend_mode
        if self.ducking:
            data["ducking"] = self.ducking
            data["duck_threshold"] = self.duck_threshold
            data["duck_ratio"] = self.duck_ratio
            data["duck_attack"] = self.duck_attack
            data["duck_release"] = self.duck_release
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Track":
        return cls(
            kind=data.get("kind", "video"),
            id=data.get("id") or new_id("track"),
            name=data.get("name", ""),
            muted=bool(data.get("muted", False)),
            locked=bool(data.get("locked", False)),
            blend_mode=data.get("blend_mode", "normal"),
            ducking=bool(data.get("ducking", False)),
            duck_threshold=float(data.get("duck_threshold", 0.1)),
            duck_ratio=float(data.get("duck_ratio", 4.0)),
            duck_attack=float(data.get("duck_attack", 50.0)),
            duck_release=float(data.get("duck_release", 300.0)),
            clips=[Clip.from_dict(c) for c in data.get("clips") or []],
        )


@dataclass
class Timeline:
    """The whole edit: settings plus an ordered stack of tracks.

    Track order is compositing order. Index 0 renders underneath; later video
    tracks are overlaid on top. Audio tracks are mixed together regardless of
    order.
    """

    tracks: list[Track] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)
    width: int = 1920
    height: int = 1080
    fps: float = 30.0
    sample_rate: int = 48000

    # -- markers ---------------------------------------------------------

    def add_marker(self, time: float, name: str = "", color: str = "#3b82f6") -> Marker:
        marker = Marker(time=_round(time), name=name, color=color)
        self.markers.append(marker)
        self.markers.sort(key=lambda m: m.time)
        return marker

    def remove_marker(self, marker_id: str) -> None:
        self.markers = [m for m in self.markers if m.id != marker_id]

    def find_marker(self, marker_id: str) -> Marker | None:
        return next((m for m in self.markers if m.id == marker_id), None)

    # -- track management ------------------------------------------------

    def add_track(self, kind: TrackKind = "video", name: str = "") -> Track:
        if kind not in VALID_TRACK_KINDS:
            raise TimelineError(f"unknown track kind {kind!r}")
        label = name or f"{kind[:1].upper()}{sum(1 for t in self.tracks if t.kind == kind) + 1}"
        track = Track(kind=kind, name=label)
        self.tracks.append(track)
        return track

    def track(self, track_id: str) -> Track:
        for candidate in self.tracks:
            if candidate.id == track_id:
                return candidate
        raise TimelineError(f"no track {track_id!r}")

    def tracks_of(self, kind: TrackKind) -> list[Track]:
        return [t for t in self.tracks if t.kind == kind]

    def remove_track(self, track_id: str) -> None:
        before = len(self.tracks)
        self.tracks = [t for t in self.tracks if t.id != track_id]
        if len(self.tracks) == before:
            raise TimelineError(f"no track {track_id!r}")

    def ensure_track(self, kind: TrackKind) -> Track:
        """Return the first track of this kind, creating one if needed."""
        existing = self.tracks_of(kind)
        return existing[0] if existing else self.add_track(kind)

    # -- clip lookup across tracks ---------------------------------------

    def find_clip(self, clip_id: str) -> tuple[Track, Clip] | None:
        for track in self.tracks:
            found = track.find_clip(clip_id)
            if found is not None:
                return track, found
        return None

    def require_clip(self, clip_id: str) -> tuple[Track, Clip]:
        found = self.find_clip(clip_id)
        if found is None:
            raise TimelineError(f"no clip {clip_id!r} in this project")
        return found

    # -- edits -----------------------------------------------------------

    def add_clip(self, clip: Clip, track_id: str | None = None,
                 track_kind: TrackKind | None = None) -> Clip:
        """Place a clip, defaulting to the tail of a suitable track."""
        if track_id:
            target = self.track(track_id)
        else:
            kind = track_kind or ("text" if clip.text else "video")
            target = self.ensure_track(kind)

        if target.kind == "text" and clip.text is None:
            clip.text = TextStyle()
        if target.kind != "text" and clip.text is not None:
            target = self.ensure_track("text")

        if clip.start < 0:
            clip.start = target.next_free_start()

        if target.overlaps(clip.start, clip.duration):
            # Push to the tail rather than silently creating a pile-up.
            clip.start = target.next_free_start()

        target.clips.append(clip)
        return clip

    def remove_clip(self, clip_id: str) -> Clip:
        track, clip = self.require_clip(clip_id)
        track.clips.remove(clip)
        return clip

    def move_clip(self, clip_id: str, start: float, track_id: str | None = None) -> Clip:
        track, clip = self.require_clip(clip_id)
        if track.locked:
            raise TimelineError(f"track {track.name!r} is locked")
        if start < 0:
            raise TimelineError("clips cannot start before 0")

        target = self.track(track_id) if track_id else track
        if target.locked:
            raise TimelineError(f"track {target.name!r} is locked")
        if target.kind == "text" and clip.text is None:
            raise TimelineError("a text clip can only live on a text track")
        if target.kind != "text" and clip.text is not None:
            raise TimelineError("a text clip must live on a text track")

        collision = target.overlaps(start, clip.duration, ignore_id=clip.id)
        if collision is not None:
            raise TimelineError(
                f"that would overlap clip {collision.id!r} at "
                f"{collision.start:.2f}s on {target.name!r}"
            )

        if target is not track:
            track.clips.remove(clip)
            target.clips.append(clip)
        clip.start = _round(start)
        return clip

    def trim_clip(self, clip_id: str, *, in_point: float | None = None,
                  duration: float | None = None,
                  max_source: float | None = None) -> Clip:
        """Adjust the source in-point and/or the timeline length."""
        _, clip = self.require_clip(clip_id)

        new_in = clip.in_point if in_point is None else max(0.0, in_point)
        new_duration = clip.duration if duration is None else duration

        if new_duration < MIN_CLIP_DURATION:
            raise TimelineError(
                f"a clip must be at least {MIN_CLIP_DURATION}s long "
                f"(asked for {new_duration:.3f}s)"
            )

        if max_source is not None:
            # Never read past the end of the source file.
            available = max(0.0, max_source - new_in)
            longest = available / clip.speed
            if longest < MIN_CLIP_DURATION:
                raise TimelineError(
                    f"only {available:.2f}s of source remains after "
                    f"in_point {new_in:.2f}s"
                )
            new_duration = min(new_duration, longest)

        clip.in_point = _round(new_in)
        clip.duration = _round(new_duration)
        return clip

    def split_clip(self, clip_id: str, at: float) -> tuple[Clip, Clip]:
        """Split a clip at a timeline time into two clips."""
        track, clip = self.require_clip(clip_id)
        if not (clip.start + MIN_CLIP_DURATION <= at
                <= clip.end - MIN_CLIP_DURATION):
            raise TimelineError(
                f"split point {at:.3f}s is outside clip "
                f"({clip.start:.3f}s - {clip.end:.3f}s)"
            )

        # Snapshot the originals first. `left` aliases `clip`, so anything read
        # off `clip` after the assignments below has already been rewritten --
        # and the right half's length is exactly such a read.
        offset = at - clip.start
        original_duration = clip.duration
        original_in_point = clip.in_point
        original_speed = clip.speed

        left = clip
        right = Clip.from_dict(clip.to_dict())

        left.duration = _round(offset)
        right.id = new_id("clip")
        right.start = _round(at)
        right.duration = _round(original_duration - offset)
        right.in_point = _round(original_in_point + offset * original_speed)
        # The incoming transition belongs to the left half; the right half
        # starts mid-shot and should not cross-fade with anything but itself.
        right.transition = None

        track.clips.append(right)
        return left, right

    def set_speed(self, clip_id: str, speed: float, *,
                  propagate_linked: bool = True) -> list[Clip]:
        """Retime a clip, holding the amount of source it consumes constant.

        Changing speed therefore changes how long the clip occupies the
        timeline -- that is what "make this shot play faster" means. Anything
        after it on the same track is pushed along to keep the cut clean.

        Linked clips are retimed together by default. Retiming the picture but
        not the sound is almost never what anyone wants, and it silently
        desynchronises the two.
        """
        if speed <= 0:
            raise TimelineError(f"speed must be positive, got {speed}")

        track, clip = self.require_clip(clip_id)
        targets = [clip]
        if propagate_linked and clip.link_id:
            for other_track in self.tracks:
                for other in other_track.clips:
                    if other is not clip and other.link_id == clip.link_id:
                        targets.append(other)

        touched: list[Clip] = []
        for target in targets:
            if abs(target.speed - speed) < 1e-9:
                continue

            # Hold source_span; recompute timeline duration.
            span = target.source_span
            old_end = target.end
            new_duration = span / speed

            if new_duration < MIN_CLIP_DURATION:
                raise TimelineError(
                    f"at {speed:g}x, clip {target.id!r} would be only "
                    f"{new_duration:.3f}s long"
                )

            owner = next(t for t in self.tracks if target in t.clips)
            # Keep the cut tidy: slide whatever follows out of the way, or
            # back, by the change in length.
            delta = new_duration - target.duration
            if delta > 1e-9:
                collision = owner.overlaps(target.start, new_duration,
                                           ignore_id=target.id)
                if collision is not None:
                    for later in owner.sorted_clips():
                        if later.start >= old_end - 1e-9:
                            later.start = _round(later.start + delta)

            target.speed = speed
            target.duration = _round(new_duration)
            touched.append(target)

        return touched

    def ripple_delete(self, clip_id: str, gap: float = 0.0) -> None:
        """Delete a clip and pull everything after it back to close the hole."""
        track, clip = self.require_clip(clip_id)
        removed_end = clip.end
        track.clips.remove(clip)
        for later in track.sorted_clips():
            if later.start >= removed_end - 1e-9:
                later.start = _round(max(0.0, later.start - clip.duration - gap))

    # -- derived ---------------------------------------------------------

    @property
    def duration(self) -> float:
        """Longest track, i.e. how long the rendered output runs."""
        return _round(max((t.duration for t in self.tracks), default=0.0))

    def is_empty(self) -> bool:
        return not any(t.clips for t in self.tracks)

    def validate(self) -> list[str]:
        """Return human-readable problems. Empty list means the timeline is sound."""
        problems: list[str] = []

        if self.width <= 0 or self.height <= 0:
            problems.append(f"invalid frame size {self.width}x{self.height}")
        if self.width % 2 or self.height % 2:
            # yuv420p needs even dimensions; the renderer would fail late.
            problems.append(
                f"frame size {self.width}x{self.height} must be even on both axes"
            )
        if self.fps <= 0:
            problems.append(f"invalid frame rate {self.fps}")

        seen_clip_ids: set[str] = set()
        for track in self.tracks:
            if track.kind == "text":
                for clip in track.clips:
                    if clip.text is None:
                        problems.append(
                            f"clip {clip.id} on text track {track.name!r} has no text"
                        )
            for clip in track.clips:
                if clip.id in seen_clip_ids:
                    problems.append(f"duplicate clip id {clip.id!r}")
                seen_clip_ids.add(clip.id)

                if clip.duration < MIN_CLIP_DURATION:
                    problems.append(
                        f"clip {clip.id!r} is shorter than {MIN_CLIP_DURATION}s"
                    )
                if clip.start < 0:
                    problems.append(f"clip {clip.id!r} starts before 0")
                if track.kind != "text" and not clip.source:
                    problems.append(f"clip {clip.id!r} has no source file")

            for i, clip in enumerate(track.sorted_clips()):
                for other in track.sorted_clips()[i + 1:]:
                    if clip.start < other.end - 1e-6 and other.start < clip.end - 1e-6:
                        problems.append(
                            f"clips {clip.id!r} and {other.id!r} overlap on "
                            f"{track.name!r}"
                        )
        return problems

    # -- serialisation ---------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "settings": {
                "width": self.width,
                "height": self.height,
                "fps": self.fps,
                "sample_rate": self.sample_rate,
            },
            "tracks": [t.to_dict() for t in self.tracks],
            "markers": [m.to_dict() for m in self.markers],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Timeline":
        settings = data.get("settings") or {}
        return cls(
            width=int(settings.get("width", 1920)),
            height=int(settings.get("height", 1080)),
            fps=float(settings.get("fps", 30.0)),
            sample_rate=int(settings.get("sample_rate", 48000)),
            tracks=[Track.from_dict(t) for t in data.get("tracks") or []],
            markers=[Marker.from_dict(m) for m in data.get("markers") or []],
        )

    def iter_clips(self) -> Iterable[Clip]:
        for track in self.tracks:
            yield from track.sorted_clips()

    # -- rendering -------------------------------------------------------

    def render(self, preset: str = "web-1080p", output: str | Path | None = None,
               **kwargs) -> Path:
        """Render this timeline. Thin sugar over :mod:`hatcut.render.pipeline`.

        Imported lazily: the render layer depends on this module, so a
        top-level import here would be circular.
        """
        from ..render.pipeline import render_timeline

        return render_timeline(self, preset, output, **kwargs)

    def plan(self, **kwargs):
        """Compile to a :class:`~hatcut.render.graph.RenderPlan` without running it."""
        from ..render.graph import build_plan

        return build_plan(self, **kwargs)
