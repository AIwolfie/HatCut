"""Turning one clip into one ffmpeg filter chain.

Every clip is normalised to exactly the project's frame size, frame rate,
pixel format, SAR and audio layout *before* it is joined to its neighbours.
That is what lets ``xfade`` and ``concat`` accept streams from wildly different
sources without complaint -- they refuse mismatched inputs, and mismatched
inputs are the normal case in real edits.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.effects import build_all as build_effects
from ..core.effects import EffectContext
from ..core.timeline import Clip

#: atempo only accepts 0.5-2.0, so anything past that is reached by chaining.
ATEMPO_MIN = 0.5
ATEMPO_MAX = 2.0


@dataclass(frozen=True)
class RenderContext:
    """Project-wide render settings, passed down to every chain."""

    width: int = 1920
    height: int = 1080
    fps: float = 30.0
    sample_rate: int = 48000
    #: Pixel format for the working graph. yuv420p is what every consumer
    #: player and hardware encoder expects.
    pix_fmt: str = "yuv420p"
    #: Colour used for letterbox bars and gaps.
    background: str = "black"

    @property
    def stereo_layout(self) -> str:
        return "stereo"


def atempo_factors(speed: float) -> list[float]:
    """Decompose a playback rate into a chain of in-range atempo values."""
    if abs(speed - 1.0) < 1e-9:
        return []

    factors: list[float] = []
    remaining = float(speed)
    while remaining > ATEMPO_MAX + 1e-9:
        factors.append(ATEMPO_MAX)
        remaining /= ATEMPO_MAX
    while remaining < ATEMPO_MIN - 1e-9:
        factors.append(ATEMPO_MIN)
        remaining /= ATEMPO_MIN
    if abs(remaining - 1.0) > 1e-9:
        factors.append(round(remaining, 9))
    return factors


def fit_filter(fit: str, width: int, height: int,
               background: str = "black") -> list[str]:
    """Scale a frame into the project raster according to a fit mode."""
    if fit == "stretch":
        return [f"scale={width}:{height}"]

    if fit == "cover":
        # Fill the frame, cropping the overflow.
        return [
            f"scale={width}:{height}:force_original_aspect_ratio=increase",
            f"crop={width}:{height}",
        ]

    # contain: letterbox the whole frame inside the raster
    return [
        f"scale={width}:{height}:force_original_aspect_ratio=decrease",
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={background}",
    ]


def _build_kf_expr(keyframes: list[dict], key: str, default: float, time_var: str = "t") -> str:
    """Build a piecewise linear interpolation expression for FFmpeg."""
    valid = []
    for kf in keyframes:
        if isinstance(kf, dict) and "time" in kf:
            try:
                t_val = float(kf["time"])
                val = float(kf.get(key, default))
                valid.append((t_val, val))
            except (ValueError, TypeError):
                continue
    if not valid:
        return f"{default:.6g}"
    valid.sort(key=lambda item: item[0])
    if len(valid) == 1:
        return f"{valid[0][1]:.6g}"

    expr = f"{valid[-1][1]:.6g}"
    for i in range(len(valid) - 2, -1, -1):
        t0, v0 = valid[i]
        t1, v1 = valid[i + 1]
        dt = max(1e-4, t1 - t0)
        slope = (v1 - v0) / dt
        interp = f"({v0:.6g}+{slope:.6g}*({time_var}-{t0:.6g}))"
        expr = f"if(lte({time_var},{t1:.6g}),{interp},{expr})"
    t_first, v_first = valid[0]
    return f"if(lt({time_var},{t_first:.6g}),{v_first:.6g},{expr})"


def transform_filters(clip: Clip, ctx: RenderContext) -> list[str]:
    """Geometry beyond the basic fit: scale multiplier, rotation, offset, and keyframing.

    When keyframes are present on the clip, dynamic piecewise expressions are compiled
    into zoompan, rotate, and geq. Without keyframes, static geometry is applied.
    """
    t = clip.transform
    kfs = getattr(clip, "transform_keyframes", None) or []
    steps: list[str] = []

    transparent = ctx.background if "@" in ctx.background else f"{ctx.background}@0"

    # Dynamic keyframe animation:
    if kfs and len(kfs) >= 2:
        has_scale_or_pos = any(
            abs(float(k.get("scale", 1.0)) - 1.0) > 1e-4 or
            abs(float(k.get("x", 0.0))) > 1e-4 or
            abs(float(k.get("y", 0.0))) > 1e-4
            for k in kfs
        )
        if has_scale_or_pos:
            z_expr = _build_kf_expr(kfs, "scale", t.scale, time_var="in_time")
            x_expr = _build_kf_expr(kfs, "x", t.x, time_var="in_time")
            y_expr = _build_kf_expr(kfs, "y", t.y, time_var="in_time")
            steps.append(
                f"zoompan=z='{z_expr}':x='(iw-iw/zoom)/2-({x_expr})':y='(ih-ih/zoom)/2-({y_expr})':"
                f"d=1:s={ctx.width}x{ctx.height}:fps={ctx.fps}"
            )

        has_rot = any(abs(float(k.get("rotate", 0.0))) > 1e-4 for k in kfs)
        if has_rot:
            rot_deg = _build_kf_expr(kfs, "rotate", t.rotate, time_var="t")
            steps.append(
                f"rotate='({rot_deg})*PI/180':ow='hypot(iw,ih)':oh='hypot(iw,ih)':c={transparent}"
            )
            steps.append(
                f"crop=w='min(iw,{ctx.width})':h='min(ih,{ctx.height})':"
                f"x='(iw-min(iw,{ctx.width}))/2':y='(ih-min(ih,{ctx.height}))/2'"
            )
            steps.append(
                f"pad={ctx.width}:{ctx.height}:(ow-iw)/2:(oh-ih)/2:color={transparent}"
            )
        elif abs(t.rotate) > 1e-9:
            radians = t.rotate * 3.141592653589793 / 180.0
            steps.append(
                f"rotate={radians:.6g}:ow=rotw({radians:.6g}):"
                f"oh=roth({radians:.6g}):c={transparent}"
            )
            steps.append(
                f"crop=w='min(iw,{ctx.width})':h='min(ih,{ctx.height})':"
                f"x='(iw-min(iw,{ctx.width}))/2':y='(ih-min(ih,{ctx.height}))/2'"
            )
            steps.append(
                f"pad={ctx.width}:{ctx.height}:(ow-iw)/2:(oh-ih)/2:color={transparent}"
            )

        has_op = any(float(k.get("opacity", 1.0)) < 0.999 for k in kfs)
        if has_op:
            op_expr = _build_kf_expr(kfs, "opacity", t.opacity, time_var="T")
            steps.append(
                f"format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
                f"a='255*max(0,min(1,{op_expr}))',format={ctx.pix_fmt}"
            )
        elif t.opacity < 1.0:
            steps.append(
                f"format=rgba,colorchannelmixer=aa={max(0.0, t.opacity):.4g},"
                f"format={ctx.pix_fmt}"
            )

        return steps

    # Static geometry:
    if abs(t.scale - 1.0) > 1e-9 and t.scale > 0:
        steps.append(f"scale=iw*{t.scale:.6g}:ih*{t.scale:.6g}")

    if abs(t.rotate) > 1e-9:
        radians = t.rotate * 3.141592653589793 / 180.0
        steps.append(
            f"rotate={radians:.6g}:ow=rotw({radians:.6g}):"
            f"oh=roth({radians:.6g}):c={transparent}"
        )

    if steps:
        steps.append(
            f"crop=w='min(iw,{ctx.width})':h='min(ih,{ctx.height})':"
            f"x='(iw-min(iw,{ctx.width}))/2':y='(ih-min(ih,{ctx.height}))/2'"
        )
        x_off = t.x
        y_off = t.y
        steps.append(
            f"pad={ctx.width}:{ctx.height}:"
            f"max(0\\,(ow-iw)/2+{x_off}):max(0\\,(oh-ih)/2+{y_off}):"
            f"color={transparent}"
        )

    if t.opacity < 1.0:
        steps.append(
            f"format=rgba,colorchannelmixer=aa={max(0.0, t.opacity):.4g},"
            f"format={ctx.pix_fmt}"
        )

    return steps


def video_chain(clip: Clip, ctx: RenderContext, *, is_image: bool = False) -> list[str]:
    """The full video filter chain for one clip.

    Order matters: trim, then retime, then geometry, then look, then fades.
    Fades go last so they run over the final composited frame rather than being
    stretched by a speed change.
    """
    steps: list[str] = []

    # Trim in the filter graph. The graph is built on top of input-level
    # fast-seek, so this only has to shave the sub-second remainder.
    if is_image:
        # A still has no timeline of its own; hold it for the clip duration.
        steps.append(f"trim=duration={clip.duration:.6f}")
    else:
        steps.append(
            f"trim=start={clip.in_point:.6f}:"
            f"duration={clip.source_span:.6f}"
        )
    steps.append("setpts=PTS-STARTPTS")

    if not is_image and abs(clip.speed - 1.0) > 1e-9:
        steps.append(f"setpts=PTS/{clip.speed:.6g}")

    steps.extend(fit_filter(clip.transform.fit, ctx.width, ctx.height,
                            ctx.background))
    steps.append("setsar=1")
    steps.append(f"fps={ctx.fps:g}")
    steps.append(f"format={ctx.pix_fmt}")

    steps.extend(transform_filters(clip, ctx))

    effect_ctx = EffectContext(
        duration=clip.duration, fps=ctx.fps,
        width=ctx.width, height=ctx.height, has_audio=False,
    )
    steps.extend(build_effects(clip.effects, effect_ctx))

    # Re-pin geometry last. The transform stage uses crop, which silently
    # rewrites the sample aspect ratio to preserve display geometry, and both
    # concat and overlay refuse streams whose SAR disagrees.
    steps.append("setsar=1")
    steps.append(f"format={ctx.pix_fmt}")
    return steps


def audio_chain(clip: Clip, ctx: RenderContext, *, silent: bool = False) -> list[str]:
    """The full audio filter chain for one clip.

    ``silent`` generates silence instead of reading a source, which is how a
    gap on an audio track stays aligned with the video above it.
    """
    steps: list[str] = []

    if silent:
        # anullsrc is infinite; cut it to the clip's timeline length.
        steps.append(f"atrim=duration={clip.duration:.6f}")
    else:
        steps.append(
            f"atrim=start={clip.in_point:.6f}:"
            f"duration={clip.source_span:.6f}"
        )
    steps.append("asetpts=PTS-STARTPTS")

    if not silent and abs(clip.speed - 1.0) > 1e-9:
        for factor in atempo_factors(clip.speed):
            steps.append(f"atempo={factor:.9g}")

    steps.append(f"aresample={ctx.sample_rate}")
    steps.append(f"aformat=sample_fmts=fltp:channel_layouts={ctx.stereo_layout}")

    effect_ctx = EffectContext(
        duration=clip.duration, fps=ctx.fps,
        width=ctx.width, height=ctx.height, has_audio=True,
    )
    steps.extend(build_effects(clip.effects, effect_ctx))

    if abs(clip.volume - 1.0) > 1e-9:
        steps.append(f"volume={max(0.0, clip.volume):.6g}")

    return steps


def chain(label_in: str, steps: list[str], label_out: str) -> str:
    """Join a label and a list of filters into one filter_complex segment."""
    # Collapse runs of identical filters: normalisation and the effect pass can
    # both ask for the same format conversion, and repeating it is noise.
    deduped: list[str] = []
    for step in steps:
        if deduped and deduped[-1] == step:
            continue
        deduped.append(step)

    if not deduped:
        return f"[{label_in}]null[{label_out}]"
    return f"[{label_in}]{','.join(deduped)}[{label_out}]"
