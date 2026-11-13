"""The editor server: static UI plus a JSON API over one project.

Standard library only. ``http.server`` is not glamorous, but it means Hatcut
installs with no dependencies at all, and the API here is small enough that a
framework would be mostly ceremony.

Three things in here are worth reading if you are extending it:

* :class:`Workspace` owns every mutation. Handlers never touch the project
  directly -- they go through a method that holds the lock, so a render
  snapshot taken mid-edit cannot see a half-applied change.
* Proxy and thumbnail generation are keyed caches. Editing a 4K HEVC source
  in a browser would otherwise mean decoding it on every scrub.
* Media is served with real range support, because a ``<video>`` element that
  cannot seek is a broken editor.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .. import __version__, ffmpeg, presets
from ..core import media
from ..core.errors import (
    HatcutError,
    RenderCancelled,
    TimelineError,
)
from ..core.project import PROJECT_SUFFIX, Project
from ..core.timeline import Clip, TextStyle

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

#: Where proxies and thumbnails live. Kept outside the project so a cache
#: never ends up in someone's repository.
CACHE_ROOT = Path(os.environ.get("HATCUT_CACHE") or
                  (Path.home() / ".hatcut" / "cache"))

#: Proxy geometry. 480p is the point where scrubbing stays smooth on a laptop
#: while still being sharp enough to judge a cut.
PROXY_HEIGHT = 480
THUMB_WIDTH = 240


def _run(args: list[str], timeout: float = 300.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, creationflags=_CREATE_NO_WINDOW,
    )


def _cache_key(*parts: str) -> str:
    digest = hashlib.sha1("\x00".join(parts).encode("utf-8")).hexdigest()
    return digest[:20]


# ---------------------------------------------------------------------------
# render jobs
# ---------------------------------------------------------------------------

@dataclass
class RenderJobState:
    """Everything the UI needs to draw a progress bar and a result."""

    id: str
    preset: str
    output: Path
    status: str = "queued"
    done: float = 0.0
    duration: float = 0.0
    error: str = ""
    command: str = ""
    warnings: list[str] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0
    cancel: threading.Event = field(default_factory=threading.Event)

    @property
    def progress(self) -> float:
        if self.status == "done":
            return 1.0
        if self.duration <= 0:
            return 0.0
        return max(0.0, min(1.0, self.done / self.duration))

    @property
    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or time.time()
        return end - self.started_at

    def to_dict(self) -> dict:
        out_exists = self.output.exists() if self.output else False
        out_abs = str(self.output.resolve()) if self.output else ""
        return {
            "id": self.id,
            "preset": self.preset,
            "output": str(self.output),
            "output_absolute": out_abs,
            "output_name": self.output.name if self.output else "",
            "download_url": f"/api/render/{self.id}/download",
            "status": self.status,
            "progress": round(self.progress, 4),
            "done": round(self.done, 3),
            "duration": round(self.duration, 3),
            "elapsed": round(self.elapsed, 2),
            "error": self.error,
            "warnings": self.warnings,
            "size_bytes": (self.output.stat().st_size
                           if self.status == "done" and out_exists else 0),
        }


class RenderManager:
    """Runs renders on background threads, one at a time.

    Serialised on purpose: ffmpeg already saturates every core it can find, so
    two concurrent exports are slower than the same two run back to back, and
    they make the progress numbers lie.
    """

    def __init__(self, workspace: "Workspace") -> None:
        self.workspace = workspace
        self._jobs: dict[str, RenderJobState] = {}
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def get(self, job_id: str) -> RenderJobState | None:
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> list[dict]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.started_at,
                          reverse=True)
            return [j.to_dict() for j in jobs]

    @property
    def busy(self) -> bool:
        with self._lock:
            return any(j.status in ("queued", "running")
                       for j in self._jobs.values())

    def start(self, preset_name: str, output: Path | None,
              **overrides) -> RenderJobState:
        from ..render.pipeline import build_job, render

        with self.workspace.lock:
            timeline = self.workspace.project.timeline
            if timeline.is_empty():
                raise TimelineError("nothing to render -- the timeline is empty")
            job_id = f"job_{uuid.uuid4().hex[:10]}"
            if output is None:
                stem = self.workspace.project.path.stem if \
                    self.workspace.project.path else "export"
                output = self.workspace.export_dir / f"{stem}.{presets.get(preset_name).container}"

            built = build_job(timeline, preset_name, output, **overrides)

        state = RenderJobState(
            id=job_id, preset=preset_name, output=Path(built.output),
            duration=built.duration, command=built.command_line(),
            warnings=list(built.warnings),
        )
        with self._lock:
            self._jobs[job_id] = state

        def work() -> None:
            state.status = "running"
            state.started_at = time.time()

            def on_progress(done: float, total: float) -> None:
                state.done = done

            try:
                render(built, on_progress=on_progress, cancel=state.cancel)
                state.status = "done"
                state.done = built.duration
            except RenderCancelled:
                state.status = "cancelled"
            except HatcutError as exc:
                state.status = "failed"
                state.error = str(exc)
            except Exception as exc:  # pragma: no cover - defensive
                state.status = "failed"
                state.error = f"{type(exc).__name__}: {exc}"
            finally:
                state.finished_at = time.time()

        thread = threading.Thread(target=work, name=f"hatcut-{job_id}",
                                  daemon=True)
        with self._lock:
            self._thread = thread
        thread.start()
        return state

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.status not in ("queued", "running"):
            return False
        job.cancel.set()
        return True


# ---------------------------------------------------------------------------
# workspace
# ---------------------------------------------------------------------------

class Workspace:
    """The project under edit, plus every derived cache around it.

    All mutation goes through the methods here, under ``lock``. That is what
    lets a render build its filtergraph from a consistent snapshot while the
    user is still dragging clips around.
    """

    def __init__(self, project: Project, *, autosave: bool = True,
                 cache_root: Path | None = None) -> None:
        self.lock = threading.RLock()
        self.project = project
        self.autosave = autosave
        self.cache_root = Path(cache_root or CACHE_ROOT)
        self.renders = RenderManager(self)
        self._build_locks: dict[str, threading.Lock] = {}
        self._build_guard = threading.Lock()
        self._dirty = False
        self.cache_root.mkdir(parents=True, exist_ok=True)

    # -- paths -----------------------------------------------------------

    @property
    def export_dir(self) -> Path:
        base = (self.project.path.parent if self.project.path
                else Path.cwd())
        target = base / "exports"
        target.mkdir(parents=True, exist_ok=True)
        return target

    @property
    def browse_root(self) -> Path:
        """Where the file browser starts.

        The project's own folder is the right default -- the media for an edit
        almost always lives beside the project, not in the process's cwd.
        """
        if self.project.path:
            return self.project.path.parent
        return Path.cwd()

    def save(self) -> None:
        with self.lock:
            if self.project.path is None:
                return
            self.project.save()
            self._dirty = False

    def mark_dirty(self) -> None:
        self._dirty = True
        if self.autosave:
            self.save()

    # -- serialised state ------------------------------------------------

    def snapshot(self) -> dict:
        with self.lock:
            timeline = self.project.timeline
            return {
                "version": __version__,
                "name": self.project.name,
                "description": self.project.description,
                "path": str(self.project.path) if self.project.path else None,
                "dirty": self._dirty,
                "autosave": self.autosave,
                "browse_root": str(self.browse_root),
                "export_dir": str(self.export_dir),
                "timeline": timeline.to_dict(),
                "duration": timeline.duration,
                "problems": self.project.validate(),
                "missing_media": self.project.missing_media(),
                # The bin shows probed-but-unplaced media too, and the timeline
                # needs to know which sources are stills, so the whole cache
                # goes over. It is small -- one dict per distinct source.
                "media_cache": dict(self.project.media_cache),
                "renders": self.renders.all(),
                "presets": presets.catalog(),
                "encoders": presets.encoder.report(),
                "ffmpeg_version": ffmpeg.version(),
                "transitions": _transition_catalog(),
                "effects": _effect_catalog(),
            }

    # -- media -----------------------------------------------------------

    def probe(self, path: str) -> dict:
        return media.probe(path).to_dict()

    def import_media(self, path: str, *, start: float | None = None,
                     in_point: float = 0.0, duration: float | None = None,
                     with_audio: bool = True, speed: float = 1.0,
                     track_id: str | None = None) -> dict:
        with self.lock:
            video, audio = self.project.add_media(
                path, start=start, in_point=in_point, duration=duration,
                with_audio=with_audio, speed=speed, track_id=track_id,
            )
            self.mark_dirty()
            return {
                "video": video.id if video else None,
                "audio": audio.id if audio else None,
            }

    def browse(self, directory: str | None) -> dict:
        target = Path(directory).expanduser() if directory else self.browse_root
        try:
            target = target.resolve()
        except OSError:
            target = self.browse_root

        if not target.is_dir():
            target = self.browse_root

        entries: list[dict] = []
        try:
            children = sorted(target.iterdir(),
                              key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            children = []

        for child in children:
            if child.name.startswith("."):
                continue
            if child.is_dir():
                entries.append({"name": child.name, "path": str(child),
                                "kind": "dir"})
            elif media.is_media_file(child):
                try:
                    size = child.stat().st_size
                except OSError:
                    size = 0
                entries.append({"name": child.name, "path": str(child),
                                "kind": "media", "size_bytes": size})

        parent = str(target.parent) if target.parent != target else None
        return {"path": str(target), "parent": parent, "entries": entries,
                "roots": self._roots()}

    @staticmethod
    def _roots() -> list[dict]:
        home = Path.home()
        roots = [{"name": "Home", "path": str(home)}]
        for label, path in (("Desktop", home / "Desktop"),
                            ("Videos", home / "Videos"),
                            ("Downloads", home / "Downloads"),
                            ("Documents", home / "Documents")):
            if path.is_dir():
                roots.append({"name": label, "path": str(path)})
        if os.name == "nt":
            import string
            for letter in string.ascii_uppercase:
                drive = Path(f"{letter}:\\")
                if drive.exists():
                    roots.append({"name": f"{letter}:", "path": str(drive)})
        return roots

    # -- proxy / thumbnail caches ----------------------------------------

    def _single_flight(self, key: str) -> threading.Lock:
        """One lock per cache key, so two tabs asking for the same proxy
        build it once instead of racing to write the same file."""
        with self._build_guard:
            lock = self._build_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._build_locks[key] = lock
            return lock

    def proxy(self, source: str) -> Path:
        """A small, seekable stand-in for a source file."""
        path = Path(source).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"no such media: {source}")

        try:
            stamp = f"{path.stat().st_mtime_ns}:{path.stat().st_size}"
        except OSError:
            stamp = "0"
        key = _cache_key("proxy", str(path.resolve()), stamp, str(PROXY_HEIGHT))
        target = self.cache_root / f"{key}.mp4"
        if target.exists() and target.stat().st_size > 0:
            return target

        with self._single_flight(key):
            if target.exists() and target.stat().st_size > 0:
                return target

            info = media.probe_safe(path)
            if info is not None and info.is_image:
                # A still has no timeline to seek through; a one-frame video
                # is not useful either. Synthesize a short clip instead so the
                # player has something to show.
                args = [
                    ffmpeg.ffmpeg_path(), "-hide_banner", "-nostdin", "-y",
                    "-loglevel", "error",
                    "-loop", "1", "-i", str(path), "-t", "10",
                    "-vf", f"scale=-2:{PROXY_HEIGHT}",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                    str(target),
                ]
            else:
                args = [
                    ffmpeg.ffmpeg_path(), "-hide_banner", "-nostdin", "-y",
                    "-loglevel", "error", "-i", str(path),
                    "-vf", f"scale=-2:{PROXY_HEIGHT}",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                    "-c:a", "aac", "-b:a", "128k",
                    str(target),
                ]

            result = _run(args)
            if result.returncode != 0 or not target.exists():
                raise HatcutError(
                    f"could not build a preview for {path.name}: "
                    f"{result.stderr.strip()[:400]}"
                )
            return target

    def thumbnail(self, source: str, at: float = 0.0) -> Path:
        """One frame, as a JPEG, for the media bin and the timeline strip."""
        path = Path(source).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"no such media: {source}")

        try:
            stamp = f"{path.stat().st_mtime_ns}"
        except OSError:
            stamp = "0"
        key = _cache_key("thumb", str(path.resolve()), stamp,
                         f"{at:.3f}", str(THUMB_WIDTH))
        target = self.cache_root / f"{key}.jpg"
        if target.exists() and target.stat().st_size > 0:
            return target

        with self._single_flight(key):
            if target.exists() and target.stat().st_size > 0:
                return target
            args = [
                ffmpeg.ffmpeg_path(), "-hide_banner", "-nostdin", "-y",
                "-loglevel", "error",
                # -ss before -i is a fast input seek; for a still it is ignored.
                "-ss", f"{max(0.0, at):.3f}", "-i", str(path),
                "-frames:v", "1", "-update", "1",
                "-vf", f"scale={THUMB_WIDTH}:-2",
                "-q:v", "4", str(target),
            ]
            result = _run(args, timeout=60.0)
            if result.returncode != 0 or not target.exists():
                raise HatcutError(
                    f"could not read a frame from {path.name}: "
                    f"{result.stderr.strip()[:400]}"
                )
            return target

    def peaks(self, source: str, samples_per_sec: int = 50) -> list[float]:
        """Normalized audio waveform peak amplitudes [0.0..1.0] for an audio source."""
        path = Path(source).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"no such media: {source}")

        try:
            stamp = f"{path.stat().st_mtime_ns}:{path.stat().st_size}"
        except OSError:
            stamp = "0"
        key = _cache_key("peaks", str(path.resolve()), stamp, str(samples_per_sec))
        target = self.cache_root / f"{key}.json"
        if target.exists() and target.stat().st_size > 0:
            try:
                return json.loads(target.read_text("utf-8"))
            except Exception:
                pass

        with self._single_flight(key):
            if target.exists() and target.stat().st_size > 0:
                try:
                    return json.loads(target.read_text("utf-8"))
                except Exception:
                    pass

            info = media.probe_safe(path)
            if info is None or not info.has_audio:
                return []

            # Extract mono 16-bit PCM at 4000 Hz
            args = [
                ffmpeg.ffmpeg_path(), "-hide_banner", "-nostdin", "-y",
                "-loglevel", "error", "-i", str(path),
                "-vn", "-ac", "1", "-ar", "4000", "-f", "s16le", "-",
            ]
            proc = subprocess.run(
                args, capture_output=True, timeout=120, creationflags=_CREATE_NO_WINDOW
            )
            raw = proc.stdout
            count = len(raw) // 2
            if count == 0:
                return []

            samples = struct.unpack(f"<{count}h", raw)
            window = max(1, 4000 // max(1, samples_per_sec))
            peaks_list = []
            for i in range(0, count, window):
                chunk = samples[i:i + window]
                peak = round(max((abs(s) for s in chunk), default=0) / 32768.0, 3)
                peaks_list.append(peak)

            try:
                target.write_text(json.dumps(peaks_list), encoding="utf-8")
            except OSError:
                pass
            return peaks_list

    def restore_snapshot(self, data: dict) -> None:
        """Restore workspace timeline from an undo/redo snapshot."""
        with self.lock:
            timeline_data = data.get("timeline") or data
            from ..core.timeline import Timeline
            self.project.timeline = Timeline.from_dict(timeline_data)
            if "name" in data and data["name"]:
                self.project.name = str(data["name"])
            if "description" in data:
                self.project.description = str(data["description"])
            if "media_cache" in data and isinstance(data["media_cache"], dict):
                self.project.media_cache.update(data["media_cache"])
            self.mark_dirty()

    # -- edits -----------------------------------------------------------

    def _require(self, clip_id: str) -> tuple[Any, Clip]:
        found = self.project.timeline.find_clip(clip_id)
        if found is None:
            raise TimelineError(f"no clip {clip_id!r}")
        return found

    def update_clip(self, clip_id: str, patch: dict) -> Clip:
        with self.lock:
            _, clip = self._require(clip_id)

            if "start" in patch and patch["start"] is not None:
                self.project.timeline.move_clip(
                    clip_id, float(patch["start"]),
                    patch.get("track_id"))
            if "track_id" in patch and patch.get("track_id") \
                    and "start" not in patch:
                self.project.timeline.move_clip(clip_id, clip.start,
                                                patch["track_id"])
            if "duration" in patch and patch["duration"] is not None:
                source = clip.source
                max_source = (self.project.media_duration(source)
                              if source else None)
                self.project.timeline.trim_clip(
                    clip_id, duration=float(patch["duration"]),
                    max_source=max_source)
            if "in_point" in patch and patch["in_point"] is not None:
                self.project.timeline.trim_clip(
                    clip_id, in_point=float(patch["in_point"]))
            if "speed" in patch and patch["speed"] is not None:
                self.project.timeline.set_speed(clip_id, float(patch["speed"]))
            if "volume" in patch and patch["volume"] is not None:
                clip.volume = max(0.0, float(patch["volume"]))
            if "enabled" in patch:
                clip.enabled = bool(patch["enabled"])
            if "name" in patch:
                clip.name = str(patch["name"])
            if "transition" in patch:
                clip.transition = patch["transition"] or None
                # A linked A/V pair should cross-fade together, or the picture
                # dissolves while the sound hard-cuts and the two drift apart.
                if clip.link_id:
                    for other in self.project.timeline.iter_clips():
                        if other.link_id == clip.link_id and other is not clip:
                            other.transition = clip.transition
            if "transform" in patch and patch["transform"]:
                for key, value in patch["transform"].items():
                    if hasattr(clip.transform, key):
                        current = getattr(clip.transform, key)
                        setattr(clip.transform, key,
                                type(current)(value) if not isinstance(value, str)
                                else value)
            if "text" in patch and patch["text"] is not None:
                if clip.text is None:
                    clip.text = TextStyle()
                for key, value in patch["text"].items():
                    if hasattr(clip.text, key):
                        setattr(clip.text, key, value)
            if "effects" in patch and patch["effects"] is not None:
                clip.effects = list(patch["effects"])
            if "blend_mode" in patch:
                clip.blend_mode = str(patch["blend_mode"])
            if "transform_keyframes" in patch:
                clip.transform_keyframes = list(patch["transform_keyframes"] or [])

            self.mark_dirty()
            return clip

    def delete_clip(self, clip_id: str, *, ripple: bool = False) -> None:
        with self.lock:
            if ripple:
                self.project.timeline.ripple_delete(clip_id)
            else:
                self.project.timeline.remove_clip(clip_id)
            self.mark_dirty()

    def delete_clips(self, clip_ids: list[str], *, ripple: bool = False) -> None:
        with self.lock:
            for cid in clip_ids:
                try:
                    if ripple:
                        self.project.timeline.ripple_delete(cid)
                    else:
                        self.project.timeline.remove_clip(cid)
                except TimelineError:
                    pass
            self.mark_dirty()

    def move_clips(self, moves: list[dict]) -> list[dict]:
        with self.lock:
            updated = []
            for move in moves:
                cid = move.get("id")
                st = move.get("start")
                tid = move.get("track_id")
                if cid and st is not None:
                    try:
                        clip = self.project.timeline.move_clip(cid, float(st), tid)
                        updated.append(clip.to_dict())
                    except TimelineError:
                        pass
            self.mark_dirty()
            return updated

    def split_clip(self, clip_id: str, at: float) -> dict:
        with self.lock:
            left, right = self.project.timeline.split_clip(clip_id, at)
            self.mark_dirty()
            return {"left": left.id, "right": right.id}

    def add_marker(self, time: float, name: str = "", color: str = "#3b82f6") -> dict:
        with self.lock:
            marker = self.project.timeline.add_marker(time, name=name, color=color)
            self.mark_dirty()
            return marker.to_dict()

    def delete_marker(self, marker_id: str) -> None:
        with self.lock:
            self.project.timeline.remove_marker(marker_id)
            self.mark_dirty()

    def import_subtitles(self, content_or_path: str, track_name: str = "Subtitles") -> dict:
        with self.lock:
            from ..core.subtitles import parse_subtitles, cues_to_clips
            content = content_or_path
            p = Path(content_or_path)
            if p.is_file():
                content = p.read_text(encoding="utf-8", errors="replace")
            cues = parse_subtitles(content)
            if not cues:
                raise ValueError("no subtitle cues found in file or content")
            track = self.project.timeline.add_track("text", track_name)
            clips = cues_to_clips(cues)
            for clip in clips:
                track.clips.append(clip)
            self.mark_dirty()
            return track.to_dict()

    def export_subtitles(self, track_id: str | None = None, format: str = "srt") -> str:
        with self.lock:
            from ..core.subtitles import export_srt, export_vtt
            if track_id:
                track = self.project.timeline.track(track_id)
                clips = track.clips
            else:
                text_tracks = self.project.timeline.tracks_of("text")
                clips = [c for t in text_tracks for c in t.clips]
            if str(format).lower() == "vtt":
                return export_vtt(clips)
            return export_srt(clips)

    def add_text(self, content: str, *, start: float | None = None,
                 duration: float = 3.0, **style) -> Clip:
        with self.lock:
            text_style = TextStyle(content=content)
            for key, value in style.items():
                if hasattr(text_style, key):
                    setattr(text_style, key, value)
            clip = Clip(start=start if start is not None else -1.0,
                        duration=duration, name=content[:40], text=text_style)
            self.project.timeline.add_clip(clip, track_kind="text")
            self.mark_dirty()
            return clip

    def add_track(self, kind: str, name: str = "") -> dict:
        with self.lock:
            track = self.project.timeline.add_track(kind, name)
            self.mark_dirty()
            return track.to_dict()

    def delete_track(self, track_id: str) -> None:
        with self.lock:
            self.project.timeline.remove_track(track_id)
            self.mark_dirty()

    def set_timeline_settings(self, patch: dict) -> None:
        with self.lock:
            timeline = self.project.timeline
            for key in ("width", "height", "fps", "sample_rate"):
                if key in patch and patch[key] is not None:
                    setattr(timeline, key, type(getattr(timeline, key))(
                        patch[key]))
            if "name" in patch and patch["name"]:
                self.project.name = str(patch["name"])
            self.mark_dirty()

    def set_track_flag(self, track_id: str, flag: str, value: Any) -> None:
        with self.lock:
            track = self.project.timeline.track(track_id)
            if flag in ("muted", "locked", "ducking"):
                setattr(track, flag, bool(value))
            elif flag == "blend_mode":
                track.blend_mode = str(value)
            elif flag in ("duck_threshold", "duck_ratio", "duck_attack", "duck_release"):
                setattr(track, flag, float(value))
            self.mark_dirty()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    """Routes requests against a :class:`Workspace`."""

    server_version = f"Hatcut/{__version__}"
    protocol_version = "HTTP/1.1"

    workspace: Workspace
    ui_dir: Path

    # -- plumbing --------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:
        # Quiet by default: the browser polls for render progress and the
        # resulting wall of 200s buries anything worth reading.
        if os.environ.get("HATCUT_VERBOSE"):
            super().log_message(fmt, *args)

    def _send(self, status: int, body: bytes, content_type: str,
              extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The UI is a single origin on localhost; no need to be clever.
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, exc: Exception, status: int = 400) -> None:
        self._json({"error": str(exc), "kind": type(exc).__name__}, status)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"request body is not valid JSON: {exc}") from exc
        return data if isinstance(data, dict) else {"value": data}

    @property
    def query(self) -> dict:
        parsed = urllib.parse.urlparse(self.path)
        return {k: v[0] for k, v in
                urllib.parse.parse_qs(parsed.query).items()}

    @property
    def route(self) -> str:
        return urllib.parse.urlparse(self.path).path

    # -- dispatch --------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        route = self.route
        try:
            if route.startswith("/api/"):
                self._api(method, route)
            elif method == "GET":
                self._static(route)
            else:
                self._json({"error": "not found"}, 404)
        except FileNotFoundError as exc:
            self._error(exc, 404)
        except (TimelineError, HatcutError, ValueError) as exc:
            self._error(exc, 400)
        except BrokenPipeError:
            # The browser navigated away mid-response. Not our problem.
            pass
        except Exception as exc:  # pragma: no cover - defensive
            if os.environ.get("HATCUT_VERBOSE"):
                import traceback
                traceback.print_exc()
            self._error(exc, 500)

    # -- static ----------------------------------------------------------

    def _static(self, route: str) -> None:
        relative = route.lstrip("/") or "index.html"
        target = (self.ui_dir / relative).resolve()
        try:
            target.relative_to(self.ui_dir.resolve())
        except ValueError:
            # A path that escapes the UI directory is not a request we serve.
            self._error(FileNotFoundError(f"no such file: {route}"), 404)
            return
        if not target.is_file():
            self._error(FileNotFoundError(f"no such file: {route}"), 404)
            return
        guessed = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if guessed.startswith("text/") or guessed in (
                "application/javascript", "application/json"):
            guessed += "; charset=utf-8"
        self._send(200, target.read_bytes(), guessed)

    # -- api -------------------------------------------------------------

    def _api(self, method: str, route: str) -> None:
        query = self.query
        workspace = self.workspace

        # ---- state ----
        if route == "/api/state" and method == "GET":
            self._json(workspace.snapshot())
            return

        if route == "/api/presets" and method == "GET":
            self._json(presets.catalog())
            return

        if route == "/api/encoders" and method == "GET":
            chosen = presets.encoder.choose("h264")
            self._json({
                "encoders": presets.encoder.report(),
                "version": ffmpeg.version(),
                "chosen_h264": {"name": chosen.name, "label": chosen.label,
                                "hardware": chosen.hardware},
            })
            return

        if route == "/api/project" and method == "GET":
            with workspace.lock:
                self._json(workspace.project.to_dict())
            return

        if route == "/api/project" and method in ("POST", "PUT"):
            workspace.restore_snapshot(self._body())
            self._json(workspace.snapshot())
            return

        if route == "/api/project/save" and method == "POST":
            workspace.save()
            self._json({"saved": str(workspace.project.path)})
            return

        if route == "/api/settings" and method == "POST":
            workspace.set_timeline_settings(self._body())
            self._json(workspace.snapshot())
            return

        # ---- media ----
        if route == "/api/browse" and method == "GET":
            self._json(workspace.browse(query.get("dir")))
            return

        if route == "/api/probe" and method == "GET":
            self._json(workspace.probe(query["path"]))
            return

        if route == "/api/import" and method == "POST":
            body = self._body()
            paths = body.get("paths") or ([body["path"]] if body.get("path") else [])
            if not paths:
                raise ValueError("no paths given")
            results = []
            for path in paths:
                try:
                    results.append({
                        "path": path,
                        "ok": True,
                        **workspace.import_media(
                            path,
                            start=body.get("start"),
                            in_point=float(body.get("in_point") or 0.0),
                            duration=body.get("duration"),
                            with_audio=body.get("with_audio", True),
                            speed=float(body.get("speed") or 1.0),
                        ),
                    })
                except HatcutError as exc:
                    results.append({"path": path, "ok": False, "error": str(exc)})
            self._json({"results": results, "state": workspace.snapshot()})
            return

        if route == "/api/proxy" and method == "GET":
            self._file(workspace.proxy(query["path"]), "video/mp4")
            return

        if route == "/api/thumb" and method == "GET":
            at = float(query.get("t") or 0.0)
            self._file(workspace.thumbnail(query["path"], at), "image/jpeg")
            return

        if route == "/api/peaks" and method == "GET":
            source = query.get("path")
            if not source:
                raise ValueError("missing path parameter")
            sps = int(query.get("samples_per_sec") or 50)
            data = workspace.peaks(source, samples_per_sec=sps)
            self._json({"path": source, "peaks_per_sec": sps, "peaks": data})
            return

        # ---- tracks ----
        if route == "/api/track" and method == "POST":
            body = self._body()
            track = workspace.add_track(body.get("kind", "video"),
                                        body.get("name", ""))
            self._json({"track": track, "state": workspace.snapshot()}, 201)
            return

        if route == "/api/track" and method == "DELETE":
            workspace.delete_track(query["id"])
            self._json(workspace.snapshot())
            return

        if route.startswith("/api/track/"):
            parts = route.split("/")
            if len(parts) == 5 and parts[4] == "flags" and method == "POST":
                body = self._body()
                workspace.set_track_flag(parts[3], body.get("flag", ""),
                                         body.get("value"))
                self._json(workspace.snapshot())
                return

        # ---- markers ----
        if route == "/api/marker" and method == "POST":
            body = self._body()
            marker = workspace.add_marker(
                float(body.get("time", 0.0)),
                name=str(body.get("name", "")),
                color=str(body.get("color", "#3b82f6")),
            )
            self._json({"marker": marker, "state": workspace.snapshot()}, 201)
            return

        if route == "/api/marker" and method == "DELETE":
            workspace.delete_marker(query["id"])
            self._json(workspace.snapshot())
            return

        # ---- subtitles ----
        if route == "/api/subtitles/import" and method == "POST":
            body = self._body()
            source = body.get("path") or body.get("content")
            if not source:
                raise ValueError("missing path or content for subtitle import")
            track_name = body.get("name", "Subtitles")
            res = workspace.import_subtitles(source, track_name=track_name)
            self._json({"track": res, "state": workspace.snapshot()}, 201)
            return

        if route == "/api/subtitles/export" and method == "GET":
            fmt = query.get("format", "srt")
            track_id = query.get("track_id")
            content = workspace.export_subtitles(track_id=track_id, format=fmt)
            ext = "vtt" if fmt.lower() == "vtt" else "srt"
            filename = f"subtitles.{ext}"
            data = content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        # ---- batch clips ----
        if route == "/api/clips/move" and method == "POST":
            body = self._body()
            updated = workspace.move_clips(body.get("moves") or [])
            self._json({"updated": updated, "state": workspace.snapshot()})
            return

        if route == "/api/clips/delete" and method == "POST":
            body = self._body()
            workspace.delete_clips(
                body.get("clip_ids") or [],
                ripple=bool(body.get("ripple", False)),
            )
            self._json(workspace.snapshot())
            return

        # ---- text ----
        if route == "/api/text" and method == "POST":
            body = self._body()
            style = {k: v for k, v in body.items()
                     if k in TextStyle.__annotations__ and k != "content"}
            clip = workspace.add_text(
                str(body.get("content", "Text")),
                start=body.get("start"), duration=float(body.get("duration") or 3.0),
                **style)
            self._json({"clip": clip.to_dict(), "state": workspace.snapshot()},
                       201)
            return

        # ---- clips ----
        if route.startswith("/api/clip/"):
            parts = route.split("/")
            clip_id = parts[3] if len(parts) > 3 else ""

            if len(parts) == 4 and method == "POST":
                clip = workspace.update_clip(clip_id, self._body())
                self._json({"clip": clip.to_dict(), "state": workspace.snapshot()})
                return

            if len(parts) == 4 and method == "DELETE":
                workspace.delete_clip(
                    clip_id, ripple=query.get("ripple") in ("1", "true"))
                self._json(workspace.snapshot())
                return

            if len(parts) == 5 and parts[4] == "split" and method == "POST":
                at = float(self._body().get("at") or 0.0)
                result = workspace.split_clip(clip_id, at)
                self._json({**result, "state": workspace.snapshot()})
                return

        # ---- render ----
        if route == "/api/render" and method == "POST":
            body = self._body()
            job = workspace.renders.start(
                body.get("preset", "web-1080p"),
                Path(body["output"]).expanduser() if body.get("output") else None,
                crf=body.get("crf"),
                width=body.get("width"),
                height=body.get("height"),
                fps=body.get("fps"),
                prefer_hardware=body.get("prefer_hardware", True),
                max_duration=body.get("max_duration"),
            )
            self._json(job.to_dict(), 202)
            return

        if route == "/api/render" and method == "GET":
            self._json(workspace.renders.all())
            return

        if route.startswith("/api/render/"):
            parts = route.split("/")
            job_id = parts[3]
            job = workspace.renders.get(job_id)
            if job is None:
                self._json({"error": f"no render job {job_id!r}"}, 404)
                return
            if len(parts) >= 5 and parts[4] == "download" and method == "GET":
                if not job.output or not Path(job.output).is_file():
                    self._json({"error": "rendered file does not exist"}, 404)
                    return
                path = Path(job.output).resolve()
                guessed = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                self._file_download(path, guessed)
                return
            if len(parts) >= 5 and parts[4] == "reveal" and method == "POST":
                if job.output and Path(job.output).exists():
                    self._reveal_in_os(Path(job.output))
                    self._json({"revealed": True, "path": str(job.output.resolve())})
                else:
                    self._json({"error": "file not found"}, 404)
                return
            if method == "DELETE":
                self._json({"cancelled": workspace.renders.cancel(job_id)})
                return
            self._json(job.to_dict())
            return

        if route == "/api/open-export-dir" and method == "POST":
            export_dir = workspace.export_dir.resolve()
            export_dir.mkdir(parents=True, exist_ok=True)
            self._reveal_in_os(export_dir)
            self._json({"revealed": True, "path": str(export_dir)})
            return

        self._json({"error": f"no route for {method} {route}"}, 404)

    # -- file responses with range support -------------------------------

    def _file(self, path: Path, content_type: str) -> None:
        """Serve a file, honouring ``Range``.

        Without range support a ``<video>`` element cannot seek -- it can only
        play from the start -- which makes an editor unusable.
        """
        path = Path(path)
        if not path.is_file():
            self._error(FileNotFoundError(f"no such file: {path}"), 404)
            return

        size = path.stat().st_size
        range_header = self.headers.get("Range")
        start, end = 0, size - 1
        status = 200

        if range_header and range_header.startswith("bytes="):
            spec = range_header[len("bytes="):].split(",")[0].strip()
            first, _, last = spec.partition("-")
            try:
                if first:
                    start = int(first)
                    end = int(last) if last else size - 1
                elif last:
                    # Suffix range: the final N bytes.
                    start = max(0, size - int(last))
                    end = size - 1
            except ValueError:
                self._json({"error": f"malformed Range: {range_header}"}, 416)
                return
            end = min(end, size - 1)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        if self.command == "HEAD":
            return

        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)

    def _file_download(self, path: Path, content_type: str) -> None:
        path = Path(path).resolve()
        if not path.is_file():
            self._error(FileNotFoundError(f"no such file: {path}"), 404)
            return
        filename = path.name
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as handle:
            while chunk := handle.read(256 * 1024):
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return

    def _reveal_in_os(self, path: Path) -> None:
        p = Path(path).resolve()
        try:
            if sys.platform == "win32":
                if p.is_file():
                    subprocess.Popen(["explorer.exe", f"/select,{str(p)}"])
                else:
                    subprocess.Popen(["explorer.exe", str(p)])
            elif sys.platform == "darwin":
                if p.is_file():
                    subprocess.Popen(["open", "-R", str(p)])
                else:
                    subprocess.Popen(["open", str(p)])
            else:
                folder = p.parent if p.is_file() else p
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception:
            pass


# ---------------------------------------------------------------------------
# catalogs passed to the UI
# ---------------------------------------------------------------------------

def _transition_catalog() -> list[dict]:
    from ..render import transitions

    return transitions.catalog()


def _effect_catalog() -> list[dict]:
    """Every effect with enough metadata for the UI to build its own controls."""
    from ..core.effects import EFFECTS, EffectSpec

    specs = EFFECTS.values() if hasattr(EFFECTS, "values") else EFFECTS
    return [spec.to_dict() for spec in specs
            if isinstance(spec, EffectSpec)]


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------

def _make_handler(workspace: Workspace, ui_dir: Path):
    class BoundHandler(Handler):
        pass

    BoundHandler.workspace = workspace
    BoundHandler.ui_dir = ui_dir
    return BoundHandler


def make_server(project: Project, *, host: str = "127.0.0.1", port: int = 8777,
                autosave: bool = True,
                cache_root: Path | None = None) -> tuple[ThreadingHTTPServer, Workspace]:
    """Build a server without starting it. Used by tests and by ``serve``."""
    ui_dir = Path(__file__).resolve().parent.parent / "ui"
    if not (ui_dir / "index.html").is_file():
        from ..core.errors import HatcutError as _H

        raise _H(f"the editor UI is missing from {ui_dir}")

    workspace = Workspace(project, autosave=autosave, cache_root=cache_root)
    server = ThreadingHTTPServer((host, port),
                                 _make_handler(workspace, ui_dir))
    server.daemon_threads = True
    return server, workspace


def serve(project_path: str | Path | None, *, host: str = "127.0.0.1",
          port: int = 8777, open_browser: bool = True,
          autosave: bool = True) -> int:
    """Run the editor until interrupted."""
    if project_path:
        target = Path(project_path).expanduser()
        if target.suffix != PROJECT_SUFFIX:
            target = target.with_suffix(PROJECT_SUFFIX)
        if target.exists():
            project = Project.load(target)
        else:
            project = Project.new(target.stem)
            project.save(target)
            print(f"created {target}")
    else:
        project = Project.new("Untitled")
        print("no project given -- running in memory, use Save As to keep it")

    try:
        server, _ = make_server(project, host=host, port=port, autosave=autosave)
    except OSError as exc:
        from ..core.errors import HatcutError as _H

        raise _H(
            f"could not bind {host}:{port} ({exc}). Something else is already "
            f"listening there -- try --port {port + 1}."
        ) from exc

    url = f"http://{host}:{server.server_address[1]}/"
    print(f"Hatcut editor on {url}")
    if project.path:
        print(f"  project: {project.path}")
    print("  ctrl-c to stop")

    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.shutdown()
        server.server_close()
        try:
            if project.path:
                project.save()
                print(f"saved {project.path}")
        except Exception:
            pass
    return 0
