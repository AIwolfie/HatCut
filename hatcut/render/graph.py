"""Compiling a Timeline into an ffmpeg filter graph.

The shape of the problem: ffmpeg's ``xfade`` and ``concat`` both refuse inputs
whose size, frame rate, pixel format or sample layout disagree, and a real edit
is nothing but disagreeing inputs. So every clip is first normalised into the
project's raster by :mod:`hatcut.render.filters`, and only then joined.

Rendering proceeds bottom-up:

1. each video track becomes one stream, its clips joined pairwise
2. the bottom track is the opaque base; every track above is overlaid in RGBA
   so that gaps and letterbox bars stay transparent instead of punching black
   holes through the layers beneath
3. text tracks are burned on last, so captions sit above every layer
4. audio tracks are built the same way as video tracks, then mixed

Joining is pairwise rather than "all at once" specifically so a track can mix
hard cuts and crossfades. A run of zero-duration cuts becomes ``concat``; a
crossfade becomes ``xfade``; and because each step consumes exactly two streams
and emits one, the two can be interleaved freely.
"""

from __future__ import annotations

import functools
import hashlib
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..core import media
from ..core.errors import HatcutError, MediaNotFoundError
from ..core.media import MediaInfo
from ..core.timeline import Clip, Timeline, Track
from . import transitions
from .filters import RenderContext, audio_chain, chain, video_chain

#: Transitions are clamped to this fraction of the shorter neighbour so xfade
#: never asks for more overlap than either stream can supply.
TRANSITION_SAFETY = 0.98

#: Fallback font candidates, in preference order, for burned-in text.
FONT_CANDIDATES = (
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)


def to_ffmpeg_color(val: str) -> str:
    """Normalize web / CSS color string to a valid, comma-free FFmpeg color expression.

    Handles:
    - 'rgba(r, g, b, a)' -> '0xRRGGBBAA'
    - 'rgb(r, g, b)' -> '0xRRGGBB'
    - '#RRGGBBAA' -> '0xRRGGBBAA'
    - '#RRGGBB' -> '0xRRGGBB'
    - '#RGB' -> '0xRRGGBB'
    - 'black@0.5', 'white', 'red', etc. -> unchanged
    """
    if not val:
        return "white"
    val = val.strip()
    import re
    m = re.match(r"^rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([0-9.]+)\s*\)$", val, re.IGNORECASE)
    if m:
        r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
        a = float(m.group(4))
        alpha_byte = max(0, min(255, int(round(a * 255))))
        return f"0x{r:02x}{g:02x}{b:02x}{alpha_byte:02x}"
    m = re.match(r"^rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)$", val, re.IGNORECASE)
    if m:
        r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"0x{r:02x}{g:02x}{b:02x}"
    if val.startswith("#"):
        h = val[1:]
        if len(h) == 3:
            return f"0x{h[0]*2}{h[1]*2}{h[2]*2}"
        if len(h) in (6, 8):
            return f"0x{h}"
    return val


@dataclass
class RenderPlan:
    """A complete, ready-to-execute ffmpeg invocation."""

    inputs: list[list[str]] = field(default_factory=list)
    filter_complex: str = ""
    video_label: str = ""
    audio_label: str = ""
    duration: float = 0.0
    temp_files: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def map_args(self) -> list[str]:
        args: list[str] = []
        if self.video_label:
            args += ["-map", f"[{self.video_label}]"]
        if self.audio_label:
            args += ["-map", f"[{self.audio_label}]"]
        return args

    def describe(self) -> str:
        return (
            f"{len(self.inputs)} inputs, {self.duration:.2f}s, "
            f"graph {len(self.filter_complex)} chars"
            + (f", {len(self.warnings)} warning(s)" if self.warnings else "")
        )


def find_font() -> str:
    """Locate a usable TrueType font for drawtext, or raise."""
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    raise HatcutError(
        "drawtext needs a font file and none of the usual ones exist. Set "
        "font_file on the text clip, or install a system font."
    )


def escape_filter_path(path: str | Path) -> str:
    """Escape a filesystem path for use as an ffmpeg filter argument.

    Windows drive letters and any colons in the path would otherwise be read as
    filter option separators.
    """
    text = str(path).replace("\\", "/")
    return text.replace(":", "\\:").replace("'", "\\'")


def escape_drawtext(value: str) -> str:
    """Escape a string for an inline drawtext option value."""
    out = value.replace("\\", "\\\\")
    for char in (":", "'", "%", ",", "[", "]", ";"):
        out = out.replace(char, "\\" + char)
    return out


@functools.lru_cache(maxsize=1)
def default_workdir() -> Path:
    """Where generated filter inputs live when the caller names no directory.

    Not the current directory. Compiling a graph writes a ``hatcut_text_*.txt``
    for every caption -- drawtext's escaping rules are hostile enough that a
    file is the only sane way to pass text -- and a graph that is *inspected*
    rather than rendered never runs the cleanup, so anything written to the cwd
    is litter that outlives the command. The system temp directory is the right
    home: the names are content-addressed, so repeats collapse onto each other,
    and the OS reclaims whatever is left.
    """
    path = Path(tempfile.gettempdir()) / "hatcut"
    path.mkdir(parents=True, exist_ok=True)
    return path


class GraphBuilder:
    """Stateful compiler. One instance per render."""

    def __init__(self, timeline: Timeline, *, workdir: Path | None = None) -> None:
        self.timeline = timeline
        self.workdir = Path(workdir) if workdir else default_workdir()
        self.inputs: list[list[str]] = []
        self.filters: list[str] = []
        self.warnings: list[str] = []
        self.temp_files: list[Path] = []
        self._counter = 0
        self._probe_cache: dict[str, MediaInfo | None] = {}
        # Transition to inherit for a sound clip whose linked picture clip
        # carries one. Populated before any audio track is built.
        self._linked_transitions: dict[str, dict] = {}

        self.base_ctx = RenderContext(
            width=timeline.width, height=timeline.height,
            fps=timeline.fps, sample_rate=timeline.sample_rate,
        )
        # Overlay layers carry alpha so uncovered regions stay see-through.
        self.overlay_ctx = RenderContext(
            width=timeline.width, height=timeline.height,
            fps=timeline.fps, sample_rate=timeline.sample_rate,
            pix_fmt="rgba", background="black@0",
        )

    # -- plumbing --------------------------------------------------------

    def _next(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"

    def _add_input(self, args: list[str]) -> int:
        self.inputs.append(args)
        return len(self.inputs) - 1

    def _probe(self, source: str) -> MediaInfo | None:
        if source not in self._probe_cache:
            self._probe_cache[source] = media.probe_safe(source)
        return self._probe_cache[source]

    def _require_probe(self, source: str) -> MediaInfo:
        info = self._probe(source)
        if info is None:
            raise MediaNotFoundError(
                f"cannot read {source!r} -- the file is missing or not decodable"
            )
        return info

    def _warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    # -- segment construction -------------------------------------------

    def _segments(self, track: Track) -> list[tuple[Clip | None, float]]:
        """Flatten a track into clip and gap segments with their durations."""
        out: list[tuple[Clip | None, float]] = []
        cursor = 0.0
        for clip in track.sorted_clips():
            if clip.start > cursor + 1e-6:
                out.append((None, round(clip.start - cursor, 6)))
            out.append((clip, clip.duration))
            cursor = clip.end
        return out

    def _video_segment(self, clip: Clip | None, duration: float,
                       ctx: RenderContext, *, transparent_gaps: bool) -> str:
        """Emit one normalised video stream and return its label."""
        label_out = self._next("vseg")

        if clip is None:
            colour = "black@0" if transparent_gaps else ctx.background
            index = self._add_input([
                "-f", "lavfi", "-t", f"{duration:.6f}",
                "-i", f"color=c={colour}:s={ctx.width}x{ctx.height}:r={ctx.fps:g}",
            ])
            steps = [f"format={ctx.pix_fmt}", "setsar=1"]
            if transparent_gaps:
                # Force full transparency regardless of how the colour source
                # resolved the alpha channel.
                steps.insert(0, "format=rgba")
                steps.insert(1, "colorchannelmixer=aa=0")
            self.filters.append(
                chain(f"{index}:v", steps, label_out)
            )
            return label_out

        info = self._require_probe(clip.source)
        is_image = info.is_image

        if is_image:
            index = self._add_input([
                "-loop", "1", "-framerate", f"{ctx.fps:g}",
                "-i", str(clip.source),
            ])
        else:
            # Input-level seek keeps this fast on long sources; the filter
            # trim then shaves to the exact frame.
            index = self._add_input([
                "-ss", f"{clip.in_point:.6f}",
                "-i", str(clip.source),
            ])

        steps = video_chain(clip, ctx, is_image=is_image)
        self.filters.append(chain(f"{index}:v", steps, label_out))
        return label_out

    def _audio_segment(self, clip: Clip | None, duration: float,
                       ctx: RenderContext) -> str | None:
        """Emit one normalised audio stream, or None if it would be silent.

        A silence gap is emitted as a real anullsrc stream so that the video
        and audio timelines stay locked together; a *silent clip* (a source
        with no audio track) is reported as None so the track can be skipped.
        """
        if clip is None:
            label_out = self._next("aseg")
            index = self._add_input([
                "-f", "lavfi", "-t", f"{duration:.6f}",
                "-i", f"anullsrc=r={ctx.sample_rate}:cl={ctx.stereo_layout}",
            ])
            steps = [
                f"atrim=duration={duration:.6f}",
                "asetpts=PTS-STARTPTS",
                f"aresample={ctx.sample_rate}",
                f"aformat=sample_fmts=fltp:channel_layouts={ctx.stereo_layout}",
            ]
            self.filters.append(chain(f"{index}:a", steps, label_out))
            return label_out

        info = self._require_probe(clip.source)
        if not info.has_audio:
            self._warn(f"{Path(clip.source).name} has no audio track; skipped")
            return None

        label_out = self._next("aseg")
        index = self._add_input([
            "-ss", f"{clip.in_point:.6f}",
            "-i", str(clip.source),
        ])
        steps = audio_chain(clip, ctx)
        self.filters.append(chain(f"{index}:a", steps, label_out))
        return label_out

    # -- joining ---------------------------------------------------------

    def _join_video(self, segments: list[tuple[str, float]],
                    trans: list[tuple[str, float]]) -> tuple[str, float]:
        """Fold normalised video segments into one stream.

        ``trans[i]`` describes the transition entering ``segments[i]``; index 0
        is ignored because nothing precedes the first segment.
        """
        acc_label, acc_duration = segments[0]

        for i in range(1, len(segments)):
            seg_label, seg_duration = segments[i]
            xfade_name, wanted = trans[i]

            # Never ask for more overlap than either side can supply.
            limit = min(acc_duration, seg_duration) * TRANSITION_SAFETY
            duration = min(wanted, limit)

            out_label = self._next("vjoin")
            if xfade_name and duration > 0.02:
                offset = max(0.0, acc_duration - duration)
                self.filters.append(
                    f"[{acc_label}][{seg_label}]xfade=transition={xfade_name}:"
                    f"duration={duration:.6f}:offset={offset:.6f}[{out_label}]"
                )
                acc_duration = acc_duration + seg_duration - duration
            else:
                if wanted > 0:
                    self._warn(
                        f"transition of {wanted:.2f}s dropped -- the neighbouring "
                        f"clips are only {limit:.2f}s long"
                    )
                self.filters.append(
                    f"[{acc_label}][{seg_label}]concat=n=2:v=1:a=0[{out_label}]"
                )
                acc_duration += seg_duration

            acc_label = out_label

        return acc_label, acc_duration

    def _join_audio(self, segments: list[tuple[str, float]],
                    curves: list[tuple[str, float]]) -> tuple[str, float]:
        """Fold normalised audio segments into one stream."""
        acc_label, acc_duration = segments[0]

        for i in range(1, len(segments)):
            seg_label, seg_duration = segments[i]
            curve, wanted = curves[i]

            limit = min(acc_duration, seg_duration) * TRANSITION_SAFETY
            duration = min(wanted, limit)

            out_label = self._next("ajoin")
            if curve and duration > 0.02:
                self.filters.append(
                    f"[{acc_label}][{seg_label}]acrossfade=d={duration:.6f}:"
                    f"c1={curve}:c2={curve}[{out_label}]"
                )
                acc_duration = acc_duration + seg_duration - duration
            else:
                self.filters.append(
                    f"[{acc_label}][{seg_label}]concat=n=2:v=0:a=1[{out_label}]"
                )
                acc_duration += seg_duration

            acc_label = out_label

        return acc_label, acc_duration

    # -- track rendering -------------------------------------------------

    def _render_video_track(self, track: Track, *, is_base: bool,
                            min_duration: float = 0.0
                            ) -> tuple[str, float] | None:
        segments = self._segments(track)
        if not segments:
            return None

        ctx = self.base_ctx if is_base else self.overlay_ctx

        labels: list[tuple[str, float]] = []
        trans: list[tuple[str, float]] = []
        for clip, duration in segments:
            label = self._video_segment(clip, duration, ctx,
                                        transparent_gaps=not is_base)
            labels.append((label, duration))
            # A clip's transition describes the join entering it, so it must
            # stay index-aligned with its own segment.
            if clip is None or clip.transition is None:
                trans.append(("", 0.0))
            else:
                xfade_name, seconds, _ = transitions.normalise(clip.transition)
                trans.append((xfade_name, seconds))

        if len(labels) == 1:
            label, track_duration = labels[0]
        else:
            label, track_duration = self._join_video(labels, trans)

        # The base layer sets how long the whole composite runs: overlay and
        # text are only keyed over the frames that exist. If the bottom track
        # stops early, everything above it gets truncated, so extend it with
        # black to the full project length first.
        if is_base and track_duration < min_duration - 1e-6:
            filler = self._video_segment(
                None, min_duration - track_duration, self.base_ctx,
                transparent_gaps=False,
            )
            padded = self._next("vpad")
            self.filters.append(
                f"[{label}][{filler}]concat=n=2:v=1:a=0[{padded}]"
            )
            label, track_duration = padded, min_duration

        return label, track_duration

    def _render_audio_track(self, track: Track) -> tuple[str, float] | None:
        if track.muted:
            return None

        segments = self._segments(track)
        if not segments:
            return None

        labels: list[tuple[str, float]] = []
        curves: list[tuple[str, float]] = []
        for clip, duration in segments:
            # A sound clip with no transition of its own inherits the one set
            # on the picture clip it was imported with, so a dissolve does not
            # land on a hard audio cut.
            spec = None
            if clip is not None:
                spec = clip.transition or self._linked_transitions.get(clip.link_id)

            if spec is None:
                curve, seconds = "", 0.0
            else:
                _, seconds, curve = transitions.normalise(spec)

            label = self._audio_segment(clip, duration, self.base_ctx)
            if label is None:
                # Source had no audio: fall back to silence so the track stays
                # aligned rather than sliding out of sync.
                label = self._audio_segment(None, duration, self.base_ctx)
                curve, seconds = "", 0.0

            labels.append((label, duration))
            curves.append((curve, seconds))

        if len(labels) == 1:
            return labels[0]
        return self._join_audio(labels, curves)

    # -- text ------------------------------------------------------------

    def _collect_linked_transitions(self) -> None:
        """Index the transitions carried by linked picture clips."""
        for track in self.timeline.tracks_of("video"):
            for clip in track.clips:
                if clip.link_id and clip.transition:
                    self._linked_transitions[clip.link_id] = clip.transition

    def _render_text(self, track: Track, base_label: str,
                     base_duration: float) -> str:
        """Burn a text track's clips onto the running composite."""
        font = ""
        current = base_label

        for clip in track.sorted_clips():
            if not clip.enabled or clip.text is None:
                continue
            style = clip.text
            if not style.content.strip():
                continue

            if not font:
                font = style.font_file or find_font()

            # A text file sidesteps drawtext's escaping rules entirely, which
            # matters the moment the caption contains a colon, a percent sign
            # or any non-Latin script.
            digest = hashlib.sha1(
                style.content.encode("utf-8")
            ).hexdigest()[:12]
            text_file = self.workdir / f"hatcut_text_{digest}.txt"
            text_file.write_text(style.content, encoding="utf-8")
            self.temp_files.append(text_file)

            out_label = self._next("vtxt")
            options = [
                f"fontfile='{escape_filter_path(font)}'",
                f"textfile='{escape_filter_path(text_file)}'",
                # Without this, drawtext interprets % as a strftime directive
                # and dies on any caption containing a percent sign.
                "expansion=none",
                f"fontsize={int(style.font_size)}",
                f"fontcolor={to_ffmpeg_color(style.color)}",
                f"x={style.x}",
                f"y={style.y}",
                f"enable='between(t,{clip.start:.6f},{clip.end:.6f})'",
            ]
            if style.box:
                options += [
                    "box=1",
                    f"boxcolor={to_ffmpeg_color(style.box_color)}",
                    f"boxborderw={int(style.box_border)}",
                ]

            self.filters.append(
                f"[{current}]drawtext={':'.join(options)}[{out_label}]"
            )
            current = out_label

        return current

    # -- entry point -----------------------------------------------------

    def build(self) -> RenderPlan:
        timeline = self.timeline
        if timeline.is_empty():
            raise HatcutError("nothing to render -- the timeline has no clips")

        problems = timeline.validate()
        if problems:
            raise HatcutError(
                "timeline is not renderable:\n  - " + "\n  - ".join(problems)
            )

        duration = timeline.duration

        # ---- video -----------------------------------------------------
        video_tracks = timeline.tracks_of("video")
        composite = ""
        composite_duration = 0.0

        for index, track in enumerate(video_tracks):
            rendered = self._render_video_track(
                track, is_base=(index == 0), min_duration=duration,
            )
            if rendered is None:
                continue
            label, track_duration = rendered

            if not composite:
                composite = label
                composite_duration = track_duration
                continue

            out_label = self._next("vcomp")
            track_blend = getattr(track, "blend_mode", "normal")
            if track_blend and track_blend != "normal":
                # Ensure input formats are compatible with blend
                self.filters.append(
                    f"[{composite}][{label}]blend=all_mode={track_blend}:all_opacity=1.0[{out_label}]"
                )
            else:
                self.filters.append(
                    f"[{composite}][{label}]overlay=0:0:"
                    f"eof_action=pass:shortest=0[{out_label}]"
                )
            composite = out_label
            composite_duration = max(composite_duration, track_duration)

        if not composite:
            # Audio-only project: synthesise a black bed so the graph still has
            # a video stream to map.
            index = self._add_input([
                "-f", "lavfi", "-t", f"{duration:.6f}",
                "-i", f"color=c=black:s={timeline.width}x{timeline.height}"
                     f":r={timeline.fps:g}",
            ])
            composite = self._next("vbase")
            self.filters.append(
                chain(f"{index}:v", ["format=yuv420p", "setsar=1"], composite)
            )

        # Everything above the base layer worked in RGBA; flatten once, here.
        flat = self._next("vflat")
        self.filters.append(f"[{composite}]format=yuv420p[{flat}]")
        composite = flat

        # ---- text ------------------------------------------------------
        for track in timeline.tracks_of("text"):
            composite = self._render_text(track, composite, duration)

        # Trim to the true project length: overlays can run long when an upper
        # track's last clip extends past the base.
        video_out = self._next("vout")
        self.filters.append(
            f"[{composite}]trim=duration={duration:.6f},setpts=PTS-STARTPTS"
            f"[{video_out}]"
        )

        # ---- audio -----------------------------------------------------
        self._collect_linked_transitions()

        audio_labels: list[str] = []
        ducked_tracks: list[tuple[str, Track]] = []
        voice_tracks: list[str] = []

        for track in timeline.tracks_of("audio"):
            rendered = self._render_audio_track(track)
            if rendered is not None:
                lbl = rendered[0]
                audio_labels.append(lbl)
                if getattr(track, "ducking", False):
                    ducked_tracks.append((lbl, track))
                else:
                    voice_tracks.append(lbl)

        audio_out = ""
        if audio_labels:
            # apad extends with silence, then atrim cuts to the exact project
            # length. Without it, an edit whose sound stops before its picture
            # produces an MP4 whose streams disagree on duration.
            tail = f"apad,atrim=duration={duration:.6f},asetpts=PTS-STARTPTS"

            if ducked_tracks and voice_tracks:
                # Duck background tracks against dialogue/voice tracks:
                if len(voice_tracks) == 1:
                    voice_mix = voice_tracks[0]
                else:
                    voice_mix = self._next("avmix")
                    joined_v = "".join(f"[{vl}]" for vl in voice_tracks)
                    self.filters.append(
                        f"{joined_v}amix=inputs={len(voice_tracks)}:"
                        f"duration=longest:dropout_transition=0:normalize=0[{voice_mix}]"
                    )

                ducked_outs = []
                for d_lbl, d_trk in ducked_tracks:
                    d_out = self._next("aduck")
                    th = getattr(d_trk, "duck_threshold", 0.1)
                    rt = getattr(d_trk, "duck_ratio", 4.0)
                    atk = getattr(d_trk, "duck_attack", 50.0)
                    rel = getattr(d_trk, "duck_release", 300.0)
                    self.filters.append(
                        f"[{d_lbl}][{voice_mix}]sidechaincompress=threshold={th:.4f}:"
                        f"ratio={rt:.2f}:attack={atk:.1f}:release={rel:.1f}[{d_out}]"
                    )
                    ducked_outs.append(d_out)

                final_ins = ducked_outs + [voice_mix]
                mixed = self._next("amix")
                all_j = "".join(f"[{fl}]" for fl in final_ins)
                self.filters.append(
                    f"{all_j}amix=inputs={len(final_ins)}:"
                    f"duration=longest:dropout_transition=0:normalize=0[{mixed}]"
                )
                audio_out = self._next("aout")
                self.filters.append(f"[{mixed}]{tail}[{audio_out}]")
            elif len(audio_labels) == 1:
                audio_out = self._next("aout")
                self.filters.append(f"[{audio_labels[0]}]{tail}[{audio_out}]")
            else:
                mixed = self._next("amix")
                joined = "".join(f"[{label}]" for label in audio_labels)
                self.filters.append(
                    f"{joined}amix=inputs={len(audio_labels)}:"
                    f"duration=longest:dropout_transition=0:normalize=0[{mixed}]"
                )
                audio_out = self._next("aout")
                self.filters.append(f"[{mixed}]{tail}[{audio_out}]")
        else:
            self._warn("no audio tracks -- the export will be silent")
            index = self._add_input([
                "-f", "lavfi", "-t", f"{duration:.6f}",
                "-i", f"anullsrc=r={timeline.sample_rate}:cl=stereo",
            ])
            audio_out = self._next("aout")
            self.filters.append(
                chain(f"{index}:a", [
                    f"atrim=duration={duration:.6f}",
                    "asetpts=PTS-STARTPTS",
                    f"aresample={timeline.sample_rate}",
                    "aformat=sample_fmts=fltp:channel_layouts=stereo",
                ], audio_out)
            )

        return RenderPlan(
            inputs=self.inputs,
            filter_complex=";".join(self.filters),
            video_label=video_out,
            audio_label=audio_out,
            duration=duration,
            temp_files=self.temp_files,
            warnings=self.warnings,
        )


def build_plan(timeline: Timeline, *, workdir: Path | None = None) -> RenderPlan:
    """Compile a timeline into a render plan."""
    return GraphBuilder(timeline, workdir=workdir).build()
