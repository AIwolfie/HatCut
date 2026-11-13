"""Encoder selection and dialect translation.

The interesting cases here are all about *vocabulary*: the same flag name
means something different, or nothing at all, depending on which encoder is
reading it. Getting that wrong is a hard ffmpeg failure, not a slow render.
"""

from __future__ import annotations

import pytest

from hatcut.export import encoder, presets
from hatcut.export.encoder import EncoderChoice


def choice(name: str) -> EncoderChoice:
    return EncoderChoice(name, name, name in encoder.HARDWARE)


class TestQualityArgs:
    def test_nvenc_gets_its_own_dialect(self):
        args = choice("h264_nvenc").quality_args(23)
        assert "-cq" in args
        assert args[args.index("-cq") + 1] == "23"
        # NVENC has no CRF; -b:v 0 is what keeps -cq in charge of the rate.
        assert args[args.index("-b:v") + 1] == "0"
        assert "-crf" not in args

    def test_qsv_uses_global_quality(self):
        args = choice("h264_qsv").quality_args(24)
        assert "-global_quality" in args
        assert "-crf" not in args

    def test_amf_uses_qp(self):
        args = choice("h264_amf").quality_args(24)
        assert "-qp_i" in args and "-qp_p" in args

    def test_videotoolbox_inverts_the_scale(self):
        # VideoToolbox -q:v runs 1..100 where higher is better, the opposite
        # of CRF, so the mapping has to flip.
        args = choice("h264_videotoolbox").quality_args(20)
        assert int(args[args.index("-q:v") + 1]) == 80

    def test_software_uses_crf(self):
        args = choice("libx264").quality_args(20)
        assert args[args.index("-crf") + 1] == "20"

    def test_no_encoder_emits_a_libx264_speed_tier_except_libx264(self):
        for name in sorted(encoder.HARDWARE):
            args = choice(name).quality_args(23)
            if "-preset" in args:
                value = args[args.index("-preset") + 1]
                assert value.startswith("p") or name.endswith("_qsv"), (
                    f"{name} was handed the x264 preset {value!r}")


class TestExtraFiltering:
    def test_x264_vocabulary_is_stripped_for_hardware(self):
        extra = ("-preset", "ultrafast", "-pix_fmt", "yuv420p",
                 "-movflags", "+faststart", "-tune", "fastdecode")
        kept = encoder.filter_extra(extra, choice("h264_nvenc"))
        assert kept == ["-pix_fmt", "yuv420p", "-movflags", "+faststart"]

    def test_software_keeps_everything(self):
        extra = ("-preset", "ultrafast", "-pix_fmt", "yuv420p",
                 "-movflags", "+faststart", "-tune", "fastdecode")
        assert encoder.filter_extra(extra, choice("libx264")) == list(extra)

    def test_neutral_flags_survive_a_hardware_encoder(self):
        extra = ("-movflags", "+faststart", "-pix_fmt", "yuv420p",
                 "-profile:v", "high", "-level", "4.2")
        assert encoder.filter_extra(extra, choice("h264_amf")) == list(extra)

    def test_a_trailing_flag_with_no_value_is_dropped_cleanly(self):
        # Malformed, but it must not take the next token with it or raise.
        assert encoder.filter_extra(("-preset",), choice("h264_nvenc")) == []

    def test_no_choice_means_no_filtering(self):
        extra = ("-preset", "ultrafast")
        assert encoder.filter_extra(extra, None) == ["-preset", "ultrafast"]

    def test_the_preview_preset_is_safe_on_every_encoder(self):
        """The regression this file exists for.

        `preview` carries `-preset ultrafast -tune fastdecode`, which NVENC
        rejects outright ("Unable to parse preset option value ultrafast").
        """
        extra = presets.get("preview").extra
        assert "-preset" in extra, "fixture drift: preview no longer sets a preset"
        for name in sorted(encoder.HARDWARE):
            kept = encoder.filter_extra(extra, choice(name))
            assert "-preset" not in kept
            assert "-tune" not in kept


class TestSelection:
    def test_software_only_when_asked(self):
        picked = encoder.choose("h264", prefer_hardware=False)
        assert not picked.hardware

    def test_hardware_when_available_and_preferred(self):
        picked = encoder.choose("h264", prefer_hardware=True)
        if any(c.hardware for c in encoder.available(encoder.H264_CANDIDATES)):
            assert picked.hardware
        else:
            assert not picked.hardware    # a CPU-only machine still renders

    def test_both_families_report_something(self):
        report = encoder.report()
        assert report["h264"]
        assert report["hevc"]

    def test_unknown_codec_falls_back_to_h264(self):
        assert encoder.choose("mystery", prefer_hardware=False).name == "libx264"
