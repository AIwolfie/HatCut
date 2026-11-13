"""Export presets.

A preset is a starting point, not a cage: every field can be overridden at the
command line or from the API, and the resolved result is what actually reaches
ffmpeg.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from ..core.errors import PresetError
from . import encoder


@dataclass(frozen=True)
class Preset:
    """A named output configuration."""

    name: str
    label: str
    container: str = "mp4"
    description: str = ""

    #: ``None`` means "use the codec family's default choice".
    codec: str | None = "h264"
    ''': 'h264' | 'hevc' | 'vp9' | 'av1' | None (audio-only)'''

    #: ``None`` means "keep the project's raster".
    width: int | None = None
    height: int | None = None
    fps: float | None = None

    #: 0-51, lower is better. Ignored by lossless/intra presets.
    crf: int = 20
    quality_args: tuple[str, ...] = ()

    acodec: str | None = "aac"
    audio_bitrate: str = "192k"
    audio_args: tuple[str, ...] = ()

    extra: tuple[str, ...] = ()
    max_duration: float | None = None
    """Seconds. Trims the export, which is what powers fast preview renders."""

    fit: str = "contain"
    """How to reconcile the project raster with this preset's raster.

    ``contain`` letterboxes; ``cover`` fills the frame and crops the overflow,
    which is what social presets want -- a 16:9 edit going to 9:16 should fill
    the screen, not sit in a letterbox with black bars top and bottom.
    """

    @property
    def is_image_sequence(self) -> bool:
        return self.container == "gif"

    @property
    def is_audio_only(self) -> bool:
        return self.codec is None and self.acodec is not None

    @property
    def is_intra(self) -> bool:
        """ProRes/DNxHD style intermediates: no CRF, fixed bitrate profile."""
        return self.container in ("mov",) and self.acodec in ("pcm_s16le",)

    def to_dict(self) -> dict:
        return {
            "name": self.name, "label": self.label, "container": self.container,
            "description": self.description, "codec": self.codec,
            "width": self.width, "height": self.height, "fps": self.fps,
            "crf": self.crf, "acodec": self.acodec,
            "audio_bitrate": self.audio_bitrate,
            "audio_only": self.is_audio_only,
            "gif": self.is_image_sequence,
        }


PRESETS: dict[str, Preset] = {}


def _register(preset: Preset) -> Preset:
    PRESETS[preset.name] = preset
    return preset


_register(Preset(
    "web-1080p", "Web 1080p (H.264)", "mp4",
    "The safe default. H.264 High profile, broadly compatible.",
    codec="h264", width=1920, height=1080, crf=20,
    extra=("-movflags", "+faststart", "-pix_fmt", "yuv420p", "-profile:v", "high",
           "-level", "4.2"),
))

_register(Preset(
    "web-720p", "Web 720p (H.264)", "mp4",
    "Smaller files for the web.",
    codec="h264", width=1280, height=720, crf=21,
    extra=("-movflags", "+faststart", "-pix_fmt", "yuv420p"),
))

_register(Preset(
    "web-4k", "Web 4K (H.264)", "mp4",
    "2160p. Slow to encode, no benefit to a hardware encoder.",
    codec="h264", width=3840, height=2160, crf=20,
    extra=("-movflags", "+faststart", "-pix_fmt", "yuv420p",
           "-profile:v", "high", "-level", "5.1"),
))

_register(Preset(
    "vertical-1080", "Vertical 1080x1920", "mp4",
    "TikTok / Reels / Shorts framing. Crops to fill.",
    codec="h264", width=1080, height=1920, crf=20,
    extra=("-movflags", "+faststart", "-pix_fmt", "yuv420p"),
    fit="cover",
))

_register(Preset(
    "square-1080", "Square 1080x1080", "mp4",
    "Feed posts.",
    codec="h264", width=1080, height=1080, crf=20,
    extra=("-movflags", "+faststart", "-pix_fmt", "yuv420p"),
    fit="cover",
))

_register(Preset(
    "hevc-1080p", "HEVC 1080p", "mp4",
    "Roughly half the size of H.264 at the same quality. Slower to encode.",
    codec="hevc", width=1920, height=1080, crf=23,
    extra=("-movflags", "+faststart", "-pix_fmt", "yuv420p",
           "-tag:v", "hvc1"),
))

_register(Preset(
    "webm-vp9", "WebM VP9", "webm",
    "Royalty-free. Good for open web publishing.",
    codec="vp9", width=1920, height=1080, crf=32, acodec="libopus",
    audio_bitrate="128k",
    extra=("-b:v", "0", "-row-mt", "1", "-pix_fmt", "yuv420p"),
))

_register(Preset(
    "gif", "Animated GIF", "gif",
    "Palette-quantised. No audio. Keep it short -- GIF is enormous.",
    codec=None, width=640, height=None, crf=0, acodec=None,
    fps=15, max_duration=15.0,
    extra=("-loop", "0"),
))

_register(Preset(
    "audio-m4a", "Audio only (M4A)", "m4a",
    "Drops the video stream entirely.",
    codec=None, acodec="aac", audio_bitrate="192k",
    extra=(),
))

_register(Preset(
    "audio-mp3", "Audio only (MP3)", "mp3",
    "Universal audio compatibility.",
    codec=None, acodec="libmp3lame", audio_bitrate="192k",
    extra=("-q:a", "2"),
))

_register(Preset(
    "prores", "ProRes 422 (mezzanine)", "mov",
    "Editing intermediate. Huge, but every NLE reads it.",
    codec="prores", acodec="pcm_s16le",
    extra=("-profile:v", "2", "-pix_fmt", "yuv422p10le", "-vendor", "apl0"),
))

_register(Preset(
    "preview", "Preview (fast, small)",
    "mp4",
    "480p ultrafast proxy for scrubbing a draft.",
    codec="h264", width=854, height=480, crf=30, fps=24,
    audio_bitrate="96k",
    extra=("-preset", "ultrafast", "-pix_fmt", "yuv420p",
           "-movflags", "+faststart", "-tune", "fastdecode"),
))


def get(name: str) -> Preset:
    try:
        return PRESETS[name]
    except KeyError:
        known = ", ".join(sorted(PRESETS))
        raise PresetError(f"unknown preset {name!r}. Available: {known}") from None


def catalog() -> list[dict]:
    return [p.to_dict() for p in PRESETS.values()]


def resolve(name: str, *, width: int | None = None, height: int | None = None,
            fps: float | None = None, crf: int | None = None,
            container: str | None = None, prefer_hardware: bool = True,
            ) -> tuple[Preset, encoder.EncoderChoice | None]:
    """Apply overrides and pick an encoder.

    Returns the effective preset and, for video presets, the encoder that will
    be used.
    """
    preset = get(name)

    changes: dict = {}
    if width is not None:
        changes["width"] = width
    if height is not None:
        changes["height"] = height
    if fps is not None:
        changes["fps"] = fps
    if crf is not None:
        changes["crf"] = crf
    if container is not None:
        changes["container"] = container
    if changes:
        preset = replace(preset, **changes)

    # GIF quantises to a palette, so CRF and the encoder choice are moot.
    if preset.codec is None:
        return preset, None

    if preset.codec == "prores":
        return preset, None

    if preset.codec == "vp9":
        if "libvpx-vp9" not in _encoders():
            raise PresetError("this ffmpeg build has no libvpx-vp9 encoder")
        return preset, encoder.EncoderChoice("libvpx-vp9", "libvpx-vp9", False)

    if preset.codec == "av1":
        if "libsvtav1" not in _encoders():
            raise PresetError("this ffmpeg build has no AV1 encoder")
        return preset, encoder.EncoderChoice("libsvtav1", "SVT-AV1", False)

    choice = encoder.choose(
        "hevc" if preset.codec == "hevc" else "h264",
        prefer_hardware=prefer_hardware,
    )
    return preset, choice


def _encoders() -> frozenset[str]:
    from .. import ffmpeg

    return ffmpeg.available_encoders()
