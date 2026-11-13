"""Transition catalogue.

Video transitions map onto ffmpeg's ``xfade`` filter; audio transitions onto
``acrossfade``. The names here are the ones stored in project files, curated
down from xfade's much longer list to the set that is actually distinguishable
on screen.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.errors import HatcutError


@dataclass(frozen=True)
class Transition:
    name: str
    label: str
    xfade: str
    audio: str = "tri"
    category: str = "dissolve"

    def to_dict(self) -> dict:
        return {
            "name": self.name, "label": self.label,
            "category": self.category, "audio": self.audio,
        }


TRANSITIONS: dict[str, Transition] = {}


def _register(name: str, label: str, xfade: str, audio: str = "tri",
              category: str = "dissolve") -> None:
    TRANSITIONS[name] = Transition(name, label, xfade, audio, category)


_register("none", "None (hard cut)", "fade", "tri", "none")
_register("fade", "Cross dissolve", "fade", "tri", "dissolve")
_register("fadeblack", "Fade through black", "fadeblack", "tri", "dissolve")
_register("fadewhite", "Fade through white", "fadewhite", "tri", "dissolve")
_register("fadegrays", "Fade through grey", "fadegrays", "tri", "dissolve")
_register("dissolve", "Dissolve", "dissolve", "log", "dissolve")

_register("wipeleft", "Wipe left", "wipeleft", "tri", "wipe")
_register("wiperight", "Wipe right", "wiperight", "tri", "wipe")
_register("wipeup", "Wipe up", "wipeup", "tri", "wipe")
_register("wipedown", "Wipe down", "wipedown", "tri", "wipe")
_register("wipetl", "Wipe corner (TL)", "wipetl", "tri", "wipe")
_register("wipetr", "Wipe corner (TR)", "wipetr", "tri", "wipe")
_register("wipebl", "Wipe corner (BL)", "wipebl", "tri", "wipe")
_register("wipebr", "Wipe corner (BR)", "wipebr", "tri", "wipe")

_register("slideleft", "Slide left", "slideleft", "tri", "slide")
_register("slideright", "Slide right", "slideright", "tri", "slide")
_register("slideup", "Slide up", "slideup", "tri", "slide")
_register("slidedown", "Slide down", "slidedown", "tri", "slide")

_register("smoothleft", "Smooth left", "smoothleft", "tri", "slide")
_register("smoothright", "Smooth right", "smoothright", "tri", "slide")
_register("smoothup", "Smooth up", "smoothup", "tri", "slide")
_register("smoothdown", "Smooth down", "smoothdown", "tri", "slide")

_register("circlecrop", "Circle crop", "circlecrop", "tri", "shape")
_register("rectcrop", "Rectangle crop", "rectcrop", "tri", "shape")
_register("circleopen", "Circle open", "circleopen", "tri", "shape")
_register("circleclose", "Circle close", "circleclose", "tri", "shape")
_register("radial", "Radial", "radial", "tri", "shape")
_register("vertopen", "Vertical open", "vertopen", "tri", "shape")
_register("vertclose", "Vertical close", "vertclose", "tri", "shape")
_register("horzopen", "Horizontal open", "horzopen", "tri", "shape")
_register("horzclose", "Horizontal close", "horzclose", "tri", "shape")

_register("pixelize", "Pixelise", "pixelize", "tri", "stylise")
_register("hblur", "Blur dissolve", "hblur", "tri", "stylise")
_register("distance", "Distance", "distance", "tri", "stylise")

_register("diagtl", "Diagonal (TL)", "diagtl", "tri", "wipe")
_register("diagtr", "Diagonal (TR)", "diagtr", "tri", "wipe")
_register("diagbl", "Diagonal (BL)", "diagbl", "tri", "wipe")
_register("diagbr", "Diagonal (BR)", "diagbr", "tri", "wipe")

_register("zoomin", "Zoom in", "zoomin", "tri", "motion")
_register("squeezeh", "Squeeze horizontal", "squeezeh", "tri", "motion")
_register("squeezev", "Squeeze vertical", "squeezev", "tri", "motion")

_register("hlslice", "Horizontal slice", "hlslice", "tri", "motion")
_register("hrslice", "Horizontal slice (rev)", "hrslice", "tri", "motion")
_register("vuslice", "Vertical slice", "vuslice", "tri", "motion")
_register("vdslice", "Vertical slice (rev)", "vdslice", "tri", "motion")

_register("hlwind", "Horizontal wind", "hlwind", "tri", "motion")
_register("hrwind", "Horizontal wind (rev)", "hrwind", "tri", "motion")
_register("vuwind", "Vertical wind", "vuwind", "tri", "motion")
_register("vdwind", "Vertical wind (rev)", "vdwind", "tri", "motion")

_register("coverleft", "Cover left", "coverleft", "tri", "motion")
_register("coverright", "Cover right", "coverright", "tri", "motion")
_register("coverup", "Cover up", "coverup", "tri", "motion")
_register("coverdown", "Cover down", "coverdown", "tri", "motion")

_register("revealleft", "Reveal left", "revealleft", "tri", "motion")
_register("revealright", "Reveal right", "revealright", "tri", "motion")
_register("revealup", "Reveal up", "revealup", "tri", "motion")
_register("revealdown", "Reveal down", "revealdown", "tri", "motion")

#: Audio crossfade curves accepted by acrossfade.
AUDIO_CURVES = frozenset({
    "tri", "qsin", "esin", "hsin", "log", "par", "qua", "cub",
    "squ", "cbr", "exp", "lin", "sin", "nofade",
})


def get(name: str) -> Transition:
    try:
        return TRANSITIONS[name]
    except KeyError:
        known = ", ".join(sorted(TRANSITIONS))
        raise HatcutError(f"unknown transition {name!r}. Known: {known}") from None


def catalog() -> list[dict]:
    return [t.to_dict() for t in TRANSITIONS.values()]


def normalise(spec: dict | None) -> tuple[str, float, str]:
    """Validate a clip's transition block.

    Returns ``(xfade_name, duration, audio_curve)``. A hard cut -- no block at
    all, or an explicit ``none`` -- comes back as ``("", 0.0, "")``.

    xfade requires a strictly positive duration shorter than both inputs; the
    graph compiler clamps further once it knows the neighbouring clip lengths.
    """
    if not spec:
        return "", 0.0, ""

    name = spec.get("type") or spec.get("name") or "none"
    duration = float(spec.get("duration", 0.5))

    if name == "none" or duration <= 0:
        return "", 0.0, ""

    transition = get(name)
    audio = spec.get("audio") or transition.audio
    if audio not in AUDIO_CURVES:
        audio = transition.audio

    return transition.xfade, duration, audio
