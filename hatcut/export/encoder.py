"""Choosing a video encoder at runtime.

Hardware encoders are several times faster than libx264 but are not present on
every machine and vary by GPU vendor, so the choice has to be made by asking
ffmpeg what it actually has rather than by assuming.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import ffmpeg

#: (name, vendor, quality-flag, quality-scale) in preference order per family.
H264_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("h264_nvenc", "NVIDIA NVENC"),
    ("h264_qsv", "Intel Quick Sync"),
    ("h264_amf", "AMD AMF"),
    ("h264_videotoolbox", "Apple VideoToolbox"),
    ("libx264", "libx264 (software)"),
)

HEVC_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("hevc_nvenc", "NVIDIA NVENC"),
    ("hevc_qsv", "Intel Quick Sync"),
    ("hevc_amf", "AMD AMF"),
    ("hevc_videotoolbox", "Apple VideoToolbox"),
    ("libx265", "libx265 (software)"),
)

#: Encoders that ignore libx264-style `-preset`/`-crf` and need their own flags.
HARDWARE = frozenset({
    "h264_nvenc", "hevc_nvenc",
    "h264_qsv", "hevc_qsv",
    "h264_amf", "hevc_amf",
    "h264_videotoolbox", "hevc_videotoolbox",
})


@dataclass(frozen=True)
class EncoderChoice:
    name: str
    label: str
    hardware: bool

    def quality_args(self, crf: int) -> list[str]:
        """Translate a CRF-ish quality target into this encoder's dialect."""
        if self.name.endswith("_nvenc"):
            # NVENC has no CRF; -cq is the nearest equivalent, and constqp
            # keeps the bitrate from drifting on static footage.
            return [
                "-rc", "vbr", "-cq", str(crf), "-b:v", "0",
                "-preset", "p5", "-tune", "hq",
            ]
        if self.name.endswith("_qsv"):
            return ["-global_quality", str(crf), "-look_ahead", "1"]
        if self.name.endswith("_amf"):
            return ["-rc", "cqp", "-qp_i", str(crf), "-qp_p", str(crf),
                    "-quality", "balanced"]
        if self.name.endswith("_videotoolbox"):
            return ["-q:v", str(max(1, min(100, 100 - crf)))]
        return ["-crf", str(crf), "-preset", "medium"]


def available(candidates: tuple[tuple[str, str], ...]) -> list[EncoderChoice]:
    present = ffmpeg.available_encoders()
    return [
        EncoderChoice(name, label, name in HARDWARE)
        for name, label in candidates
        if name in present
    ]


def choose(codec: str = "h264", *, prefer_hardware: bool = True) -> EncoderChoice:
    """Pick the best available encoder for a codec family.

    Falls back to software rather than failing, so a render still completes on
    a machine with no usable GPU encoder.
    """
    candidates = HEVC_CANDIDATES if codec in ("hevc", "h265") else H264_CANDIDATES
    options = available(candidates)
    if not options:
        from ..core.errors import PresetError

        raise PresetError(
            f"this ffmpeg build has no {'HEVC' if codec in ('hevc', 'h265') else 'H.264'} "
            f"encoder. Tried: {', '.join(n for n, _ in candidates)}"
        )

    if prefer_hardware:
        for choice in options:
            if choice.hardware:
                return choice
    else:
        for choice in options:
            if not choice.hardware:
                return choice

    return options[-1]


def report() -> dict[str, list[str]]:
    """Every usable encoder per family, for `hatcut encoders`."""
    return {
        "h264": [c.name for c in available(H264_CANDIDATES)],
        "hevc": [c.name for c in available(HEVC_CANDIDATES)],
    }


#: Flags whose *values* are a private vocabulary rather than a shared scale.
#: ``-preset ultrafast`` is a libx264 speed tier and ``-tune fastdecode`` an
#: x264 tuning; NVENC, QSV and AMF reject both outright -- NVENC wants ``p1``
#: through ``p7`` and knows only ``hq``/``ll``/``ull``/``lossless`` as tunings.
#: These are dropped for hardware encoders rather than translated, because
#: there is no faithful mapping between a speed tier and a ``pN`` preset, and
#: :meth:`EncoderChoice.quality_args` has already supplied the encoder's own
#: speed/quality dial.
VOCABULARY_FLAGS = frozenset({"-preset", "-tune"})


def filter_extra(extra: tuple[str, ...] | list[str],
                 choice: "EncoderChoice | None") -> list[str]:
    """Drop encoder-private flags that the chosen encoder cannot parse.

    Presets carry a few codec-specific extra arguments. Those that name a
    *value* every encoder understands (``-pix_fmt``, ``-movflags``,
    ``-profile:v``) pass through untouched; those that name a libx264 speed
    tier or tuning do not survive contact with a hardware encoder.
    """
    if choice is None or not choice.hardware:
        return list(extra)
    kept: list[str] = []
    skipping = False
    for token in extra:
        if skipping:
            skipping = False
            continue
        if token in VOCABULARY_FLAGS:
            skipping = True
            continue
        kept.append(token)
    return kept
