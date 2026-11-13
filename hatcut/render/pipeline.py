"""Assembling and executing the ffmpeg command line."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from .. import ffmpeg
from ..core.errors import RenderCancelled, RenderError
from ..core.timeline import Timeline
from ..export import encoder as encoder_mod
from ..export import presets as preset_mod
from ..export.encoder import EncoderChoice
from . import graph as graph_mod

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


@dataclass
class RenderJob:
    """A fully-built ffmpeg invocation plus everything needed to run it."""

    command: list[str] = field(default_factory=list)
    duration: float = 0.0
    output: Path | None = None
    warnings: list[str] = field(default_factory=list)
    temp_files: list[Path] = field(default_factory=list)

    def command_line(self) -> str:
        import shlex

        return " ".join(shlex.quote(part) for part in self.command)

    def to_dict(self) -> dict:
        return {
            "command": self.command_line(),
            "duration": self.duration,
            "output": str(self.output) if self.output else None,
            "warnings": self.warnings,
        }


def _scale_stage(plan: graph_mod.RenderPlan, preset: preset_mod.Preset,
                 timeline: Timeline) -> tuple[str, str]:
    """Append a final rescale stage when the preset raster differs.

    Returns the (possibly new) label for the video stream and any extra filter
    segments to append.
    """
    same_size = (preset.width in (None, timeline.width)
                 and preset.height in (None, timeline.height))
    if same_size:
        return plan.video_label, ""

    width = preset.width or timeline.width
    height = preset.height or timeline.height
    if width % 2 or height % 2:
        # yuv420p requires even dimensions; nudge rather than fail at the end
        # of a long render.
        width -= width % 2
        height -= height % 2

    label_out = "vscaled"
    if preset.fit == "cover":
        segment = (
            f"[{plan.video_label}]scale={width}:{height}"
            f":force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,format=yuv420p[{label_out}]"
        )
    else:
        segment = (
            f"[{plan.video_label}]scale={width}:{height}"
            f":force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,format=yuv420p[{label_out}]"
        )
    return label_out, segment


def _gif_stage(label_in: str, preset: preset_mod.Preset) -> tuple[str, str]:
    """Two-pass palette generation, which is what makes a GIF not look awful."""
    label_out = "vgif"
    segment = (
        f"[{label_in}]split[a][b];"
        f"[a]palettegen=max_colors=256:stats_mode=diff[p];"
        f"[b][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle"
        f"[{label_out}]"
    )
    return label_out, segment


def build_job(timeline: Timeline, preset_name: str, output: str | Path | None = None,
              *, workdir: Path | None = None, width: int | None = None,
              height: int | None = None, fps: float | None = None,
              crf: int | None = None, prefer_hardware: bool = True,
              max_duration: float | None = None) -> RenderJob:
    """Compile a timeline and a preset into a runnable job."""
    workdir = Path(workdir) if workdir else graph_mod.default_workdir()
    preset, choice = preset_mod.resolve(
        preset_name, width=width, height=height, fps=fps, crf=crf,
        prefer_hardware=prefer_hardware,
    )

    plan = graph_mod.build_plan(timeline, workdir=workdir)

    segments = [plan.filter_complex]
    video_label = plan.video_label

    video_label, scale_segment = _scale_stage(plan, preset, timeline)
    if scale_segment:
        segments.append(scale_segment)

    if preset.is_image_sequence:
        video_label, gif_segment = _gif_stage(video_label, preset)
        segments.append(gif_segment)

    filter_complex = ";".join(segments)

    # ---- input arguments ----------------------------------------------
    command: list[str] = [
        ffmpeg.ffmpeg_path(),
        "-hide_banner", "-nostdin", "-y",
        "-loglevel", "error",
        "-progress", "pipe:1", "-nostats",
    ]
    for input_args in plan.inputs:
        command.extend(input_args)

    command += ["-filter_complex", filter_complex]

    if preset.is_audio_only:
        # Drop the video stream entirely rather than encoding and discarding it.
        if plan.audio_label:
            command += ["-map", f"[{plan.audio_label}]"]
        command += ["-vn"]
    else:
        command += ["-map", f"[{video_label}]"]
        if plan.audio_label:
            command += ["-map", f"[{plan.audio_label}]"]
        else:
            command += ["-an"]

    # ---- codec arguments ----------------------------------------------
    if preset.is_audio_only:
        command += ["-c:a", preset.acodec or "aac",
                    "-b:a", preset.audio_bitrate]
        command += list(preset.audio_args)
    elif preset.is_image_sequence:
        pass  # the palette stage owns the pixel format
    elif preset.codec == "prores":
        command += ["-c:v", "prores_ks", "-c:a", "pcm_s16le"]
    else:
        assert choice is not None
        command += ["-c:v", choice.name]
        if preset.quality_args:
            command += list(preset.quality_args)
        elif preset.codec in ("vp9", "av1"):
            command += ["-crf", str(preset.crf)]
        else:
            command += choice.quality_args(preset.crf)

        if preset.acodec:
            command += ["-c:a", preset.acodec, "-b:a", preset.audio_bitrate]
            command += list(preset.audio_args)
        else:
            command += ["-an"]

    if preset.fps:
        command += ["-r", f"{preset.fps:g}"]

    # Preset extras are filtered against the encoder actually chosen: a
    # hardware encoder cannot parse libx264's speed-tier vocabulary, and
    # passing it one is a hard failure rather than a silent downgrade.
    command += encoder_mod.filter_extra(preset.extra, choice)

    # ---- duration and output -------------------------------------------
    limit = plan.duration
    if max_duration is not None:
        limit = min(limit, max_duration)
    elif preset.max_duration is not None:
        limit = min(limit, preset.max_duration)
    command += ["-t", f"{limit:.6f}"]

    if output is None:
        suffix = preset.container
        output = workdir / f"hatcut_export.{suffix}"
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    command += ["-f", preset.container] if preset.container in ("mp4", "webm", "gif") else []
    command.append(str(output))

    return RenderJob(
        command=command,
        duration=limit,
        output=output,
        warnings=list(plan.warnings),
        temp_files=list(plan.temp_files),
    )


def _cleanup(paths: list[Path]) -> None:
    for path in paths:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass


def render(job: RenderJob, *, on_progress: Callable[[float, float], None] | None = None,
           keep_temp: bool = False,
           cancel: threading.Event | None = None) -> Path:
    """Run a job to completion. Returns the output path.

    Raises :class:`RenderError` with ffmpeg's stderr attached on failure --
    without that, a failed render is a wall of silence. Raises
    :class:`RenderCancelled` if ``cancel`` is set while the render is running.

    Cancellation is cooperative rather than a thread kill: ffmpeg gets
    ``terminate`` and the loop stops reading. A half-written output file is
    removed, because leaving a truncated mp4 behind for the user to find later
    is worse than leaving nothing.
    """
    start = time.monotonic()

    process = subprocess.Popen(
        job.command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_CREATE_NO_WINDOW,
    )

    errors: list[str] = []
    cancelled = False
    aborted = False
    try:
        assert process.stdout is not None
        for line in process.stdout:
            if cancel is not None and cancel.is_set():
                cancelled = True
                process.terminate()
                break
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key == "out_time_us" and on_progress:
                try:
                    seconds = int(value) / 1_000_000
                except ValueError:
                    continue
                on_progress(seconds, job.duration)
            elif key in ("error", "Error"):
                errors.append(value)
    except BaseException:
        # A caller-side fault -- a progress callback that raises, a
        # KeyboardInterrupt -- must not leave ffmpeg running. Without this the
        # process outlives the call, keeps burning every core, and writes an
        # output file the caller has already stopped waiting for.
        aborted = True
        process.terminate()
        raise
    finally:
        stderr = process.stderr.read() if process.stderr else ""
        code = process.wait()
        if not keep_temp:
            _cleanup(job.temp_files)

    if aborted and job.output is not None:
        # Same reasoning as cancellation: a half-written file that looks like
        # a finished export is worse than no file.
        try:
            Path(job.output).unlink(missing_ok=True)
        except OSError:
            pass

    if cancelled:
        # ffmpeg wrote a partial file. Remove it rather than pass it off as
        # an export.
        try:
            if job.output is not None:
                Path(job.output).unlink(missing_ok=True)
        except OSError:
            pass
        raise RenderCancelled(
            f"render cancelled after {time.monotonic() - start:.1f}s"
        )

    if code != 0:
        detail = stderr.strip() or "\n".join(errors) or "no output"
        raise RenderError(
            f"ffmpeg exited with code {code} after "
            f"{time.monotonic() - start:.1f}s:\n{detail}",
            command=job.command,
            stderr=stderr,
        )

    if on_progress:
        on_progress(job.duration, job.duration)

    return job.output  # type: ignore[return-value]


def render_timeline(timeline: Timeline, preset_name: str,
                    output: str | Path | None = None, **kwargs) -> Path:
    """One-shot convenience: build a job and run it."""
    on_progress = kwargs.pop("on_progress", None)
    job = build_job(timeline, preset_name, output, **kwargs)
    return render(job, on_progress=on_progress)


def ffmpeg_available() -> bool:
    try:
        ffmpeg.ffmpeg_path()
        ffmpeg.ffprobe_path()
    except Exception:
        return False
    return True
