"""The effect registry.

Effects are data, not code paths: a clip stores ``{"type": "blur", "params":
{"sigma": 8}}`` and the renderer looks the type up here to get an ffmpeg filter
string. That keeps project files portable and lets the web UI build its controls
straight from :data:`EFFECTS` without a hand-written form for every filter.

Every builder receives an :class:`EffectContext` so time-dependent effects
(fades, in particular) can express themselves in terms of the clip's real
duration rather than a hard-coded constant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from .errors import HatcutError

ParamType = Literal["float", "int", "bool", "color", "choice", "points"]


@dataclass(frozen=True)
class Param:
    """A single tunable on an effect, with enough metadata to build a UI."""

    name: str
    label: str
    type: ParamType = "float"
    default: Any = 0.0
    minimum: float | None = None
    maximum: float | None = None
    step: float = 0.05
    choices: tuple[str, ...] = ()
    help: str = ""

    def clamp(self, value: Any) -> Any:
        if self.type == "bool":
            return bool(value)
        if self.type == "choice":
            return value if value in self.choices else self.default
        if self.type == "color":
            return str(value)
        if self.type == "points":
            if isinstance(value, (list, tuple)):
                out = []
                for pt in value:
                    if isinstance(pt, dict):
                        try:
                            x = float(pt.get("x", 0.0))
                            y = float(pt.get("y", 0.0))
                            out.append({"x": max(0.0, min(1.0, x)), "y": max(0.0, min(1.0, y))})
                        except (TypeError, ValueError):
                            continue
                    elif isinstance(pt, (list, tuple)) and len(pt) >= 2:
                        try:
                            x = float(pt[0])
                            y = float(pt[1])
                            out.append({"x": max(0.0, min(1.0, x)), "y": max(0.0, min(1.0, y))})
                        except (TypeError, ValueError):
                            continue
                return out
            return list(self.default) if isinstance(self.default, (list, tuple)) else []
        try:
            number = float(value)
        except (TypeError, ValueError):
            return self.default
        if self.minimum is not None:
            number = max(self.minimum, number)
        if self.maximum is not None:
            number = min(self.maximum, number)
        return int(round(number)) if self.type == "int" else number

    def to_dict(self) -> dict:
        default_val = list(self.default) if isinstance(self.default, (list, tuple)) else self.default
        return {
            "name": self.name, "label": self.label, "type": self.type,
            "default": default_val, "min": self.minimum, "max": self.maximum,
            "step": self.step, "choices": list(self.choices), "help": self.help,
        }


@dataclass(frozen=True)
class EffectContext:
    """What a builder needs to know about the clip it is attached to."""

    duration: float
    fps: float = 30.0
    width: int = 1920
    height: int = 1080
    has_audio: bool = False


Builder = Callable[[dict[str, Any], EffectContext], str]


@dataclass(frozen=True)
class EffectSpec:
    name: str
    label: str
    builder: Builder
    params: tuple[Param, ...] = ()
    category: str = "color"
    help: str = ""

    def defaults(self) -> dict[str, Any]:
        return {p.name: p.default for p in self.params}

    def normalise(self, params: dict[str, Any] | None) -> dict[str, Any]:
        """Fill in defaults and clamp everything to its declared range."""
        supplied = params or {}
        return {
            p.name: p.clamp(supplied.get(p.name, p.default))
            for p in self.params
        }

    def to_dict(self) -> dict:
        return {
            "name": self.name, "label": self.label, "category": self.category,
            "help": self.help, "params": [p.to_dict() for p in self.params],
        }


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------

def _build_eq(params: dict, ctx: EffectContext) -> str:
    parts = []
    for key in ("brightness", "contrast", "saturation", "gamma"):
        value = params.get(key)
        if value is None:
            continue
        if key == "brightness" and abs(value) < 1e-9:
            continue
        if key in ("contrast", "saturation", "gamma") and abs(value - 1.0) < 1e-9:
            continue
        parts.append(f"{key}={value:.6g}")
    return "eq=" + ":".join(parts) if parts else "null"


def _build_brightness(params, ctx):
    return _build_eq({"brightness": params["amount"]}, ctx)


def _build_contrast(params, ctx):
    return _build_eq({"contrast": params["amount"]}, ctx)


def _build_saturation(params, ctx):
    return _build_eq({"saturation": params["amount"]}, ctx)


def _build_gamma(params, ctx):
    return _build_eq({"gamma": params["amount"]}, ctx)


def _build_blur(params, ctx):
    return f"gblur=sigma={params['sigma']:.4g}:steps={int(params['steps'])}"


def _build_sharpen(params, ctx):
    amount = params["amount"]
    return f"unsharp=5:5:{amount:.4g}:5:5:0.0"


def _build_hue(params, ctx):
    return f"hue=h={params['degrees']:.4g}:s={params['saturation']:.4g}"


def _build_grayscale(params, ctx):
    return "hue=s=0"


def _build_sepia(params, ctx):
    intensity = params["intensity"]
    # Blend the sepia matrix toward identity by intensity.
    t = _clamp(intensity, 0.0, 1.0)
    r = t
    return (
        "colorchannelmixer="
        f"{0.393 * r + (1 - r):.5f}:{0.769 * r:.5f}:{0.189 * r:.5f}:0:"
        f"{0.349 * r:.5f}:{0.686 * r + (1 - r):.5f}:{0.168 * r:.5f}:0:"
        f"{0.272 * r:.5f}:{0.534 * r:.5f}:{0.131 * r + (1 - r):.5f}"
    )


def _build_vignette(params, ctx):
    return f"vignette=angle={params['angle']:.5g}"


def _build_vignette_soft(params, ctx):
    return "vignette"


def _build_invert(params, ctx):
    return "negate"


def _build_opacity(params, ctx):
    alpha = _clamp(params["alpha"], 0.0, 1.0)
    return f"format=rgba,colorchannelmixer=aa={alpha:.4g},format=yuv420p"


def _build_fade_in(params, ctx):
    duration = min(float(params["duration"]), max(ctx.duration, 0.001))
    return f"fade=t=in:st=0:d={duration:.4g}"


def _build_fade_out(params, ctx):
    requested = float(params["duration"])
    duration = min(requested, max(ctx.duration, 0.001))
    start = max(0.0, ctx.duration - duration)
    return f"fade=t=out:st={start:.4g}:d={duration:.4g}"


def _build_audio_fade_in(params, ctx):
    duration = min(float(params["duration"]), max(ctx.duration, 0.001))
    return f"afade=t=in:st=0:d={duration:.4g}"


def _build_audio_fade_out(params, ctx):
    requested = float(params["duration"])
    duration = min(requested, max(ctx.duration, 0.001))
    start = max(0.0, ctx.duration - duration)
    return f"afade=t=out:st={start:.4g}:d={duration:.4g}"


def _build_gain(params, ctx):
    return f"volume={params['db']:.4g}dB"


def _build_pitch(params, ctx):
    return f"rubberband=pitch={params['ratio']:.4g}"


def _build_speed_audio(params, ctx):
    return f"atempo={_clamp(params['factor'], 0.5, 100.0):.4g}"


def _build_mirror(params, ctx):
    return f"hflip" if params["axis"] == "horizontal" else "vflip"


def _build_rotate(params, ctx):
    degrees = float(params["degrees"])
    radians = degrees * 3.141592653589793 / 180.0
    return f"rotate={radians:.6g}:fillcolor=black@0"


def _build_crop(params, ctx):
    return (
        f"crop=iw*{params['width']:.4g}:ih*{params['height']:.4g}:"
        f"iw*{params['x']:.4g}:ih*{params['y']:.4g}"
    )


import math


# Mask builders.
# --------------

def _triangulate_polygon(pts: list[tuple[float, float]]) -> list[tuple[tuple[float, float], tuple[float, float], tuple[float, float]]]:
    """Triangulate a 2D simple polygon into triangles using ear-clipping."""
    if len(pts) < 3:
        return []
    cleaned = [pts[0]]
    for pt in pts[1:]:
        if abs(pt[0] - cleaned[-1][0]) > 1e-5 or abs(pt[1] - cleaned[-1][1]) > 1e-5:
            cleaned.append(pt)
    if len(cleaned) > 2 and (abs(cleaned[0][0] - cleaned[-1][0]) < 1e-5 and abs(cleaned[0][1] - cleaned[-1][1]) < 1e-5):
        cleaned.pop()
    n = len(cleaned)
    if n < 3:
        return []

    area = sum(cleaned[i][0] * cleaned[(i + 1) % n][1] - cleaned[(i + 1) % n][0] * cleaned[i][1] for i in range(n))
    if area < 0:
        cleaned = cleaned[::-1]

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    def in_triangle(p, a, b, c):
        cp1 = cross(a, b, p)
        cp2 = cross(b, c, p)
        cp3 = cross(c, a, p)
        return (cp1 >= -1e-6 and cp2 >= -1e-6 and cp3 >= -1e-6)

    indices = list(range(n))
    triangles = []
    max_iters = 3 * n
    i = 0
    while len(indices) > 2 and max_iters > 0:
        max_iters -= 1
        n_idx = len(indices)
        prev_idx = indices[(i - 1) % n_idx]
        curr_idx = indices[i % n_idx]
        next_idx = indices[(i + 1) % n_idx]
        a = cleaned[prev_idx]
        b = cleaned[curr_idx]
        c = cleaned[next_idx]
        if cross(a, b, c) > 1e-6:
            is_ear = True
            for k in range(n_idx):
                idx = indices[k]
                if idx not in (prev_idx, curr_idx, next_idx):
                    if in_triangle(cleaned[idx], a, b, c):
                        is_ear = False
                        break
            if is_ear:
                triangles.append((a, b, c))
                indices.pop(i % n_idx)
                continue
        i = (i + 1) % n_idx

    if len(indices) > 2:
        root = cleaned[indices[0]]
        for k in range(1, len(indices) - 1):
            triangles.append((root, cleaned[indices[k]], cleaned[indices[k + 1]]))
    return triangles


def _build_freeform_mask(params, ctx):
    """Free-form polygon mask with keyframeable point positions."""
    raw_points = params.get("points") or params.get("points_default") or []
    if not isinstance(raw_points, (list, tuple)) or len(raw_points) < 3:
        return "null"

    points: list[tuple[float, float]] = []
    for p in raw_points:
        if isinstance(p, dict):
            points.append((float(p.get("x", 0.0)) * ctx.width, float(p.get("y", 0.0)) * ctx.height))
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            points.append((float(p[0]) * ctx.width, float(p[1]) * ctx.height))

    if len(points) < 3:
        return "null"

    triangles = _triangulate_polygon(points)
    if not triangles:
        return "null"

    feather = _clamp(float(params.get("feathering", 0.0)), 0.0, 200.0)
    inverted = bool(params.get("inverted", False))

    tri_exprs = []
    for a, b, c in triangles:
        e1 = f"gte((X-{a[0]:.2f})*({b[1]-a[1]:.2f})-(Y-{a[1]:.2f})*({b[0]-a[0]:.2f}),0)"
        e2 = f"gte((X-{b[0]:.2f})*({c[1]-b[1]:.2f})-(Y-{b[1]:.2f})*({c[0]-b[0]:.2f}),0)"
        e3 = f"gte((X-{c[0]:.2f})*({a[1]-c[1]:.2f})-(Y-{c[1]:.2f})*({a[0]-c[0]:.2f}),0)"
        tri_exprs.append(f"({e1}*{e2}*{e3})")

    inside = f"gt({'+'.join(tri_exprs)},0)"
    alpha_expr = f"if({inside},0,255)" if inverted else f"if({inside},255,0)"

    steps = [
        "format=rgba",
        f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{alpha_expr}'",
    ]
    if feather > 0.1:
        steps.append(f"gblur=sigma={feather:.2f}:planes=8")
    return ",".join(steps)


def _build_polygon_mask(params, ctx):
    """A fixed polygon mask with editable vertices and feathered edge."""
    return _build_freeform_mask(params, ctx)


def _build_time_expr(keyframes: list[dict] | None, param_name: str, default_val: float) -> str:
    """Build a piece-wise linear interpolation expression over time T in FFmpeg geq.

    If keyframes is None or has <= 1 keyframes with param_name, returns the constant string.
    """
    if not keyframes or not isinstance(keyframes, list):
        return f"{default_val:.4f}"

    kfs: list[tuple[float, float]] = []
    for kf in keyframes:
        if isinstance(kf, dict) and "params" in kf and isinstance(kf["params"], dict):
            if param_name in kf["params"]:
                try:
                    t = max(0.0, float(kf.get("time", 0.0)))
                    v = float(kf["params"][param_name])
                    kfs.append((t, v))
                except (TypeError, ValueError):
                    continue

    if not kfs:
        return f"{default_val:.4f}"
    kfs.sort(key=lambda x: x[0])

    if len(kfs) == 1:
        return f"{kfs[0][1]:.4f}"

    # Multiple keyframes: nested if-conditions over T
    expr = f"{kfs[-1][1]:.4f}"
    for i in range(len(kfs) - 2, -1, -1):
        t0, v0 = kfs[i]
        t1, v1 = kfs[i + 1]
        dt = max(1e-4, t1 - t0)
        lerp = f"({v0:.4f}+({v1 - v0:.4f})*(T-{t0:.3f})/{dt:.4f})"
        expr = f"if(lt(T,{t1:.3f}),if(lte(T,{t0:.3f}),{v0:.4f},{lerp}),{expr})"
    return expr


def _build_linear_mask(params: dict, ctx: EffectContext) -> str:
    """Linear / split mask with position, angle, feathering, and optional keyframes."""
    keyframes = params.get("_keyframes") or params.get("keyframes")
    has_kfs = bool(keyframes and isinstance(keyframes, list) and len(keyframes) > 1)

    x_default = float(params.get("x", 0.5))
    y_default = float(params.get("y", 0.5))
    angle_default = float(params.get("angle", 0.0))
    feather = _clamp(float(params.get("feathering", 0.0)), 0.0, 200.0)
    inverted = bool(params.get("inverted", False))

    rad = angle_default * math.pi / 180.0
    nx = math.cos(rad)
    ny = math.sin(rad)

    if not has_kfs:
        x0 = x_default * ctx.width
        y0 = y_default * ctx.height
        inside = f"gte({nx:.5f}*(X-{x0:.2f})+{ny:.5f}*(Y-{y0:.2f}),0)"
    else:
        x_expr = f"(({_build_time_expr(keyframes, 'x', x_default)})*{ctx.width})"
        y_expr = f"(({_build_time_expr(keyframes, 'y', y_default)})*{ctx.height})"
        inside = f"gte({nx:.5f}*(X-({x_expr}))+{ny:.5f}*(Y-({y_expr})),0)"

    alpha_expr = f"if({inside},0,255)" if inverted else f"if({inside},255,0)"
    steps = [
        "format=rgba",
        f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{alpha_expr}'",
    ]
    if feather > 0.1:
        steps.append(f"gblur=sigma={feather:.2f}:planes=8")
    return ",".join(steps)


def _build_ellipse_mask(params, ctx):
    """Ellipse (rounded-oval) mask, keyframeable centre / radii / rotation."""
    keyframes = params.get("_keyframes") or params.get("keyframes")
    has_kfs = bool(keyframes and isinstance(keyframes, list) and len(keyframes) > 1)

    cx_default = float(params.get("cx", 0.5))
    cy_default = float(params.get("cy", 0.5))
    rx_default = max(0.01, float(params.get("rx", 0.3)))
    ry_default = max(0.01, float(params.get("ry", 0.3)))
    rot = float(params.get("rotation", 0.0))
    feather = _clamp(float(params.get("feathering", 0.0)), 0.0, 200.0)
    inverted = bool(params.get("inverted", False))

    if not has_kfs:
        cx = cx_default * ctx.width
        cy = cy_default * ctx.height
        rx = rx_default * ctx.width
        ry = ry_default * ctx.height
        if abs(rot) < 1e-4:
            q_expr = f"hypot((X-{cx:.2f})/{rx:.2f},(Y-{cy:.2f})/{ry:.2f})"
        else:
            rad = rot * math.pi / 180.0
            cos_th = math.cos(rad)
            sin_th = math.sin(rad)
            q_expr = (
                f"hypot(({cos_th:.5f}*(X-{cx:.2f})+{sin_th:.5f}*(Y-{cy:.2f}))/{rx:.2f},"
                f"({-sin_th:.5f}*(X-{cx:.2f})+{cos_th:.5f}*(Y-{cy:.2f}))/{ry:.2f})"
            )
    else:
        cx_expr = f"(({_build_time_expr(keyframes, 'cx', cx_default)})*{ctx.width})"
        cy_expr = f"(({_build_time_expr(keyframes, 'cy', cy_default)})*{ctx.height})"
        rx_expr = f"(({_build_time_expr(keyframes, 'rx', rx_default)})*{ctx.width})"
        ry_expr = f"(({_build_time_expr(keyframes, 'ry', ry_default)})*{ctx.height})"
        if abs(rot) < 1e-4:
            q_expr = f"hypot((X-({cx_expr}))/({rx_expr}),(Y-({cy_expr}))/({ry_expr}))"
        else:
            rad = rot * math.pi / 180.0
            cos_th = math.cos(rad)
            sin_th = math.sin(rad)
            q_expr = (
                f"hypot(({cos_th:.5f}*(X-({cx_expr}))+{sin_th:.5f}*(Y-({cy_expr})))/({rx_expr}),"
                f"({-sin_th:.5f}*(X-({cx_expr}))+{cos_th:.5f}*(Y-({cy_expr})))/({ry_expr}))"
            )

    alpha_expr = f"if(lte({q_expr},1),0,255)" if inverted else f"if(lte({q_expr},1),255,0)"
    steps = [
        "format=rgba",
        f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{alpha_expr}'",
    ]
    if feather > 0.1:
        steps.append(f"gblur=sigma={feather:.2f}:planes=8")
    return ",".join(steps)


def _build_rectangle_mask(params, ctx):
    """Rectangle / rounded-corner mask, keyframeable position/size/roundness."""
    keyframes = params.get("_keyframes") or params.get("keyframes")
    has_kfs = bool(keyframes and isinstance(keyframes, list) and len(keyframes) > 1)

    x_default = max(0.0, float(params.get("x", 0.2)))
    y_default = max(0.0, float(params.get("y", 0.2)))
    w_default = max(0.01, float(params.get("w", 0.6)))
    h_default = max(0.01, float(params.get("h", 0.6)))
    round_val = _clamp(float(params.get("round", 0.0)), 0.0, 1.0) * min(w_default * ctx.width, h_default * ctx.height) * 0.5
    feather = _clamp(float(params.get("feathering", 0.0)), 0.0, 200.0)
    inverted = bool(params.get("inverted", False))

    if not has_kfs:
        x0 = x_default * ctx.width
        y0 = y_default * ctx.height
        w = w_default * ctx.width
        h = h_default * ctx.height
        x1 = x0 + w
        y1 = y0 + h
        if round_val <= 0.5:
            inside = f"between(X,{x0:.2f},{x1:.2f})*between(Y,{y0:.2f},{y1:.2f})"
        else:
            r = round_val
            inside = (
                f"gt(between(X,{x0+r:.2f},{x1-r:.2f})*between(Y,{y0:.2f},{y1:.2f})+"
                f"between(X,{x0:.2f},{x1:.2f})*between(Y,{y0+r:.2f},{y1-r:.2f})+"
                f"lte(hypot(X-{x0+r:.2f},Y-{y0+r:.2f}),{r:.2f})*lte(X,{x0+r:.2f})*lte(Y,{y0+r:.2f})+"
                f"lte(hypot(X-{x1-r:.2f},Y-{y0+r:.2f}),{r:.2f})*gte(X,{x1-r:.2f})*lte(Y,{y0+r:.2f})+"
                f"lte(hypot(X-{x0+r:.2f},Y-{y1-r:.2f}),{r:.2f})*lte(X,{x0+r:.2f})*gte(Y,{y1-r:.2f})+"
                f"lte(hypot(X-{x1-r:.2f},Y-{y1-r:.2f}),{r:.2f})*gte(X,{x1-r:.2f})*gte(Y,{y1-r:.2f}),0)"
            )
    else:
        x_expr = f"(({_build_time_expr(keyframes, 'x', x_default)})*{ctx.width})"
        y_expr = f"(({_build_time_expr(keyframes, 'y', y_default)})*{ctx.height})"
        w_expr = f"(({_build_time_expr(keyframes, 'w', w_default)})*{ctx.width})"
        h_expr = f"(({_build_time_expr(keyframes, 'h', h_default)})*{ctx.height})"
        x1_expr = f"(({x_expr})+({w_expr}))"
        y1_expr = f"(({y_expr})+({h_expr}))"
        inside = f"between(X,{x_expr},{x1_expr})*between(Y,{y_expr},{y1_expr})"

    alpha_expr = f"if({inside},0,255)" if inverted else f"if({inside},255,0)"
    steps = [
        "format=rgba",
        f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{alpha_expr}'",
    ]
    if feather > 0.1:
        steps.append(f"gblur=sigma={feather:.2f}:planes=8")
    return ",".join(steps)



# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

def _register(spec: EffectSpec) -> EffectSpec:
    EFFECTS[spec.name] = spec
    return spec

EFFECTS: dict[str, EffectSpec] = {}

_register(EffectSpec(
    "brightness", "Brightness", _build_brightness,
    (Param("amount", "Amount", "float", 0.0, -1.0, 1.0, 0.02,
           help="-1 is black, 0 is unchanged, 1 is white"),),
    "color",
))

_register(EffectSpec(
    "contrast", "Contrast", _build_contrast,
    (Param("amount", "Amount", "float", 1.0, 0.0, 4.0, 0.05,
           help="1.0 is unchanged"),),
    "color",
))

_register(EffectSpec(
    "saturation", "Saturation", _build_saturation,
    (Param("amount", "Amount", "float", 1.0, 0.0, 4.0, 0.05,
           help="0 is greyscale, 1.0 is unchanged"),),
    "color",
))

_register(EffectSpec(
    "gamma", "Gamma", _build_gamma,
    (Param("amount", "Amount", "float", 1.0, 0.1, 4.0, 0.05),),
    "color",
))

_register(EffectSpec(
    "hue", "Hue shift", _build_hue,
    (Param("degrees", "Hue", "float", 0.0, -180.0, 180.0, 1.0),
     Param("saturation", "Saturation", "float", 1.0, 0.0, 3.0, 0.05)),
    "color",
))

_register(EffectSpec(
    "grayscale", "Greyscale", _build_grayscale, (), "color",
))

_register(EffectSpec(
    "sepia", "Sepia", _build_sepia,
    (Param("intensity", "Intensity", "float", 0.8, 0.0, 1.0, 0.05),),
    "color",
))

_register(EffectSpec(
    "invert", "Invert", _build_invert, (), "color",
))

_register(EffectSpec(
    "blur", "Blur", _build_blur,
    (Param("sigma", "Radius", "float", 6.0, 0.1, 60.0, 0.5),
     Param("steps", "Quality", "int", 2, 1, 6, 1,
           help="More steps is smoother and slower")),
    "blur",
))

_register(EffectSpec(
    "sharpen", "Sharpen", _build_sharpen,
    (Param("amount", "Amount", "float", 1.0, 0.0, 5.0, 0.05),),
    "blur",
))

_register(EffectSpec(
    "vignette", "Vignette", _build_vignette,
    (Param("angle", "Strength", "float", 0.628, 0.0, 1.5708, 0.02),),
    "stylise",
))

_register(EffectSpec(
    "vignette_soft", "Vignette (soft)", _build_vignette_soft, (), "stylise",
))

_register(EffectSpec(
    "mirror", "Mirror", _build_mirror,
    (Param("axis", "Axis", "choice", "horizontal", choices=("horizontal", "vertical")),),
    "stylise",
))

_register(EffectSpec(
    "rotate", "Rotate", _build_rotate,
    (Param("degrees", "Degrees", "float", 0.0, -180.0, 180.0, 1.0),),
    "stylise",
))

_register(EffectSpec(
    "crop", "Crop (fractional)", _build_crop,
    (Param("width", "Width", "float", 1.0, 0.05, 1.0, 0.01),
     Param("height", "Height", "float", 1.0, 0.05, 1.0, 0.01),
     Param("x", "X", "float", 0.0, 0.0, 1.0, 0.01),
     Param("y", "Y", "float", 0.0, 0.0, 1.0, 0.01)),
    "stylise",
))

_register(EffectSpec(
    "opacity", "Opacity", _build_opacity,
    (Param("alpha", "Alpha", "float", 1.0, 0.0, 1.0, 0.05),),
    "stylise",
))

_register(EffectSpec(
    "fade_in", "Fade in", _build_fade_in,
    (Param("duration", "Duration", "float", 0.5, 0.0, 10.0, 0.1, help="seconds"),),
    "transition",
))

_register(EffectSpec(
    "fade_out", "Fade out", _build_fade_out,
    (Param("duration", "Duration", "float", 0.5, 0.0, 10.0, 0.1, help="seconds"),),
    "transition",
))

_register(EffectSpec(
    "audio_fade_in", "Audio fade in", _build_audio_fade_in,
    (Param("duration", "Duration", "float", 0.4, 0.0, 10.0, 0.1),),
    "audio",
))

_register(EffectSpec(
    "audio_fade_out", "Audio fade out", _build_audio_fade_out,
    (Param("duration", "Duration", "float", 0.4, 0.0, 10.0, 0.1),),
    "audio",
))

_register(EffectSpec(
    "gain", "Gain", _build_gain,
    (Param("db", "Decibels", "float", 0.0, -40.0, 20.0, 0.5),),
    "audio",
))

_register(EffectSpec(
    "speed", "Speed", _build_speed_audio,
    (Param("factor", "Factor", "float", 1.0, 0.5, 100.0, 0.05),),
    "audio",
))

#: Effects that only make sense on a track with audio.
AUDIO_EFFECTS = frozenset({"audio_fade_in", "audio_fade_out", "gain", "speed"})

#: Effects that must run after the frame is composited to final size.
POST_EFFECTS = frozenset({"fade_in", "fade_out", "freeform_mask", "polygon_mask",
                          "ellipse_mask", "rectangle_mask", "linear_mask", "split_mask"})

_register(EffectSpec(
    "freeform_mask", "Mask (freeform)", _build_freeform_mask,
    (
        Param("points", "Points", "points",
              [{"x": 0.3, "y": 0.2}, {"x": 0.7, "y": 0.2}, {"x": 0.8, "y": 0.7},
               {"x": 0.5, "y": 0.85}, {"x": 0.2, "y": 0.7}],
              help="keyframeable [{x,y},{x,y}] in project space, 0-1"),
        Param("feathering", "Feather", "float", 0.0, 0.0, 100.0, 1.0,
              help="soft edge width in pixels"),
        Param("inverted", "Inverted", "bool", False),
    ),
    "mask",
))

_register(EffectSpec(
    "polygon_mask", "Mask (polygon)", _build_polygon_mask,
    (
        Param("points", "Points", "points",
              [{"x": 0.25, "y": 0.25}, {"x": 0.75, "y": 0.25},
               {"x": 0.75, "y": 0.75}, {"x": 0.25, "y": 0.75}],
              help="keyframeable [{x,y},{x,y}] vertices, 0-1 project space"),
        Param("feathering", "Feather", "float", 0.0, 0.0, 100.0, 1.0,
              help="soft edge width in pixels"),
        Param("inverted", "Inverted", "bool", False),
    ),
    "mask",
))

_register(EffectSpec(
    "ellipse_mask", "Mask (ellipse)", _build_ellipse_mask,
    (
        Param("cx", "Center X", "float", 0.5, 0.0, 1.0, 0.01),
        Param("cy", "Center Y", "float", 0.5, 0.0, 1.0, 0.01),
        Param("rx", "Radius X", "float", 0.3, 0.01, 1.0, 0.01),
        Param("ry", "Radius Y", "float", 0.3, 0.01, 1.0, 0.01),
        Param("rotation", "Rotation", "float", 0.0, -180.0, 180.0, 1.0),
        Param("feathering", "Feather", "float", 0.0, 0.0, 100.0, 1.0),
        Param("inverted", "Inverted", "bool", False),
    ),
    "mask",
))

_register(EffectSpec(
    "rectangle_mask", "Mask (rectangle)", _build_rectangle_mask,
    (
        Param("x", "Left", "float", 0.2, 0.0, 1.0, 0.01),
        Param("y", "Top", "float", 0.2, 0.0, 1.0, 0.01),
        Param("w", "Width", "float", 0.6, 0.01, 1.0, 0.01),
        Param("h", "Height", "float", 0.6, 0.01, 1.0, 0.01),
        Param("round", "Roundness", "float", 0.0, 0.0, 1.0, 0.01),
        Param("feathering", "Feather", "float", 0.0, 0.0, 100.0, 1.0),
        Param("inverted", "Inverted", "bool", False),
    ),
    "mask",
))

_register(EffectSpec(
    "linear_mask", "Mask (linear split)", _build_linear_mask,
    (
        Param("x", "Center X", "float", 0.5, 0.0, 1.0, 0.01),
        Param("y", "Center Y", "float", 0.5, 0.0, 1.0, 0.01),
        Param("angle", "Angle", "float", 0.0, -180.0, 180.0, 1.0),
        Param("feathering", "Feather", "float", 0.0, 0.0, 100.0, 1.0),
        Param("inverted", "Inverted", "bool", False),
    ),
    "mask",
))

_register(EffectSpec(
    "split_mask", "Mask (split)", _build_linear_mask,
    (
        Param("x", "Center X", "float", 0.5, 0.0, 1.0, 0.01),
        Param("y", "Center Y", "float", 0.5, 0.0, 1.0, 0.01),
        Param("angle", "Angle", "float", 0.0, -180.0, 180.0, 1.0),
        Param("feathering", "Feather", "float", 0.0, 0.0, 100.0, 1.0),
        Param("inverted", "Inverted", "bool", False),
    ),
    "mask",
))


# ---------------------------------------------------------------------------
# chroma key / green screen
# ---------------------------------------------------------------------------

def _build_chroma_key(params: dict[str, Any], ctx: EffectContext) -> str:
    raw_color = str(params.get("color", "#00FF00")).strip()
    if raw_color.startswith("#"):
        hex_val = "0x" + raw_color[1:]
    elif raw_color.startswith("0x") or raw_color.startswith("0X"):
        hex_val = raw_color
    else:
        hex_val = "0x00FF00"
    sim = float(params.get("similarity", 0.3))
    bld = float(params.get("blend", 0.1))
    spill = float(params.get("spill", 0.0))

    filter_str = f"format=rgba,colorkey=color={hex_val}:similarity={sim:.4f}:blend={bld:.4f}"
    if spill > 0.01:
        try:
            num = int(hex_val[2:], 16)
            g = (num >> 8) & 0xFF
            b = num & 0xFF
            spill_type = "green" if g >= b else "blue"
        except ValueError:
            spill_type = "green"
        filter_str += f",despill=type={spill_type}:mix={min(1.0, spill):.2f}"
    return filter_str


_register(EffectSpec(
    "chroma_key", "Chroma Key (Green Screen)", _build_chroma_key,
    (
        Param("color", "Key Color", "color", "#00FF00"),
        Param("similarity", "Tolerance", "float", 0.3, 0.01, 1.0, 0.01),
        Param("blend", "Softness", "float", 0.1, 0.0, 1.0, 0.01),
        Param("spill", "Spill Suppression", "float", 0.0, 0.0, 1.0, 0.05),
    ),
    "keying",
    help="Remove solid green, blue or custom colored backdrop.",
))


# ---------------------------------------------------------------------------
# 3D LUT (.cube)
# ---------------------------------------------------------------------------

def _build_lut3d(params: dict[str, Any], ctx: EffectContext) -> str:
    from pathlib import Path
    file_path = str(params.get("file", "")).strip()
    if not file_path:
        return "null"
    p = Path(file_path)
    if not p.is_file():
        builtin = Path(__file__).parent.parent / "assets" / "luts" / p.name
        if builtin.is_file():
            p = builtin
        elif not p.exists():
            return "null"
    interp = params.get("interp", "tetrahedral")
    esc_path = str(p.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    return f"lut3d=file='{esc_path}':interp={interp}"


_register(EffectSpec(
    "lut3d", "3D LUT (.cube)", _build_lut3d,
    (
        Param("file", "LUT File", "color", "cinematic_warm.cube"),
        Param("interp", "Interpolation", "choice", "tetrahedral",
              choices=("nearest", "trilinear", "tetrahedral")),
    ),
    "color",
    help="Apply 3D color lookup table (.cube) for film looks.",
))


# ---------------------------------------------------------------------------
# preset color looks
# ---------------------------------------------------------------------------

def _build_preset_look(params: dict[str, Any], ctx: EffectContext) -> str:
    look = str(params.get("look", "film_warm"))
    intensity = max(0.0, min(1.0, float(params.get("intensity", 1.0))))
    if intensity <= 0.001:
        return "null"

    if look == "film_warm":
        return (
            f"colorbalance=rs={0.08*intensity:.3f}:gs={0.02*intensity:.3f}:bs={-0.08*intensity:.3f}:"
            f"rm={0.08*intensity:.3f}:gm={0.02*intensity:.3f}:bm={-0.06*intensity:.3f}:"
            f"rh={0.06*intensity:.3f}:gh={0.02*intensity:.3f}:bh={-0.08*intensity:.3f},"
            f"eq=contrast={1.0+0.08*intensity:.3f}:saturation={1.0+0.12*intensity:.3f}"
        )
    elif look == "clean_cold":
        return (
            f"colorbalance=rs={-0.08*intensity:.3f}:gs={-0.02*intensity:.3f}:bs={0.08*intensity:.3f}:"
            f"rm={-0.06*intensity:.3f}:gm=0:bm={0.08*intensity:.3f}:"
            f"rh={-0.04*intensity:.3f}:gh=0:bh={0.06*intensity:.3f},"
            f"eq=contrast={1.0+0.1*intensity:.3f}:saturation={1.0-0.05*intensity:.3f}"
        )
    elif look == "cyberpunk":
        return (
            f"colorbalance=rs={-0.15*intensity:.3f}:gs={0.05*intensity:.3f}:bs={0.2*intensity:.3f}:"
            f"rm={0.15*intensity:.3f}:gm={-0.05*intensity:.3f}:bm={0.15*intensity:.3f}:"
            f"rh={0.2*intensity:.3f}:gh={-0.1*intensity:.3f}:bh={0.15*intensity:.3f},"
            f"eq=contrast={1.0+0.25*intensity:.3f}:saturation={1.0+0.35*intensity:.3f}"
        )
    elif look == "vintage":
        return (
            f"colorbalance=rs={0.1*intensity:.3f}:gs={0.08*intensity:.3f}:bs={-0.15*intensity:.3f}:"
            f"rm={0.08*intensity:.3f}:gm={0.06*intensity:.3f}:bm={-0.1*intensity:.3f}:"
            f"rh={0.05*intensity:.3f}:gh={0.04*intensity:.3f}:bh={-0.08*intensity:.3f},"
            f"eq=contrast={1.0-0.08*intensity:.3f}:saturation={1.0-0.18*intensity:.3f}:brightness={0.02*intensity:.3f},"
            f"vignette=PI/4*{intensity:.2f}"
        )
    elif look == "noir":
        sat = max(0.0, 1.0 - intensity)
        return f"hue=s={sat:.2f},eq=contrast={1.0+0.45*intensity:.3f}:brightness={-0.05*intensity:.3f}"
    elif look == "teal_and_orange":
        return (
            f"colorbalance=rs={-0.12*intensity:.3f}:gs=0:bs={0.15*intensity:.3f}:"
            f"rm={0.05*intensity:.3f}:gm={-0.02*intensity:.3f}:bm={-0.05*intensity:.3f}:"
            f"rh={0.18*intensity:.3f}:gh={0.08*intensity:.3f}:bh={-0.15*intensity:.3f},"
            f"eq=contrast={1.0+0.18*intensity:.3f}:saturation={1.0+0.2*intensity:.3f}"
        )
    elif look == "bleach_bypass":
        return f"eq=contrast={1.0+0.3*intensity:.3f}:saturation={max(0.0, 1.0-0.5*intensity):.3f}"
    return "null"


_register(EffectSpec(
    "preset_look", "Preset Color Look", _build_preset_look,
    (
        Param("look", "Color Look", "choice", "film_warm",
              choices=("film_warm", "clean_cold", "cyberpunk", "vintage", "noir", "teal_and_orange", "bleach_bypass")),
        Param("intensity", "Intensity", "float", 1.0, 0.0, 1.0, 0.05),
    ),
    "color",
    help="One-click cinematic color grading looks.",
))


# ---------------------------------------------------------------------------
# audio pitch correction & shift
# ---------------------------------------------------------------------------

def _build_pitch(params: dict[str, Any], ctx: EffectContext) -> str:
    semitones = float(params.get("semitones", 0.0))
    if abs(semitones) < 1e-4:
        return "null"
    factor = 2.0 ** (semitones / 12.0)
    preserve_tempo = bool(params.get("preserve_tempo", True))
    sr = 48000
    # Prefer rubberband if supported, else asetrate + resample + atempo
    filter_str = f"rubberband=pitch={factor:.6f}"
    return filter_str


_register(EffectSpec(
    "pitch", "Pitch Shift", _build_pitch,
    (
        Param("semitones", "Semitones", "float", 0.0, -24.0, 24.0, 1.0),
        Param("preserve_tempo", "Preserve Tempo", "bool", True),
    ),
    "audio",
    help="Shift audio pitch higher or lower while preserving playback speed.",
))


# ---------------------------------------------------------------------------
# overlay blend mode
# ---------------------------------------------------------------------------

def _build_blend_mode(params: dict[str, Any], ctx: EffectContext) -> str:
    return "null"


_register(EffectSpec(
    "blend_mode", "Blend Mode", _build_blend_mode,
    (
        Param("mode", "Mode", "choice", "screen",
              choices=("screen", "multiply", "overlay", "addition", "difference", "darken", "lighten", "hardlight", "softlight")),
        Param("opacity", "Opacity", "float", 1.0, 0.0, 1.0, 0.05),
    ),
    "composite",
    help="Overlay blending mode for composites and texture layers.",
))


def get(name: str) -> EffectSpec:
    try:
        return EFFECTS[name]
    except KeyError:
        known = ", ".join(sorted(EFFECTS))
        raise HatcutError(f"unknown effect {name!r}. Known effects: {known}") from None


def build_all(effects: list[dict], ctx: EffectContext) -> list[str]:
    """Turn a clip's effect list into ffmpeg filter strings, in order."""
    filters: list[str] = []
    for entry in effects or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("type") or entry.get("name")
        if not name:
            continue
        spec = get(name)
        params = spec.normalise(entry.get("params"))
        if "keyframes" in entry and isinstance(entry["keyframes"], list):
            params["_keyframes"] = entry["keyframes"]
        try:
            filters.append(spec.builder(params, ctx))
        except (KeyError, TypeError) as exc:
            raise HatcutError(f"effect {name!r} has bad parameters: {exc}") from exc
    return [f for f in filters if f and f != "null"]


def catalog() -> list[dict]:
    """The full effect list, shaped for a UI to render controls from."""
    return [spec.to_dict() for spec in sorted(EFFECTS.values(),
                                              key=lambda s: (s.category, s.name))]
