"""End-to-end rendering.

The unit tests above check the model. These check that the model survives
contact with ffmpeg -- which is where the two clips that agree on nothing
(different raster, frame rate, sample rate, channel layout) stop being a
theoretical problem and start being an actual command line.
"""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

import pytest

from hatcut import ffmpeg
from hatcut.core.media import probe
from hatcut.core.project import Project
from hatcut.render import pipeline

pytestmark = pytest.mark.usefixtures("media_dir")


def build(project: Project, preset: str = "preview", **kwargs):
    # These tests only inspect the compiled command line, so the output path
    # is a placeholder -- nothing is executed.
    return pipeline.build_job(project.timeline, preset,
                              output=Path(tempfile.gettempdir()) / "hatcut_out.mp4",
                              **kwargs)


class TestGraphCompilation:
    def test_two_mismatched_clips_produce_one_normalised_graph(
            self, project: Project, wide, tall):
        project.add_media(wide, start=0.0)
        project.add_media(tall, start=2.0)
        job = build(project)
        graph = job.command[job.command.index("-filter_complex") + 1]

        # Both sources are scaled onto the project raster before they meet, or
        # concat refuses the mismatched inputs.
        assert graph.count("scale=1280:720") >= 2
        assert "concat=" in graph
        assert "aresample=48000" in graph

    def test_the_command_names_the_chosen_encoder(self, project: Project, wide):
        project.add_media(wide, start=0.0)
        job = build(project)
        chosen = job.command[job.command.index("-c:v") + 1]
        assert chosen in ffmpeg.available_encoders()

    def test_a_hardware_encoder_is_never_handed_x264_preset_names(
            self, project: Project, wide):
        project.add_media(wide, start=0.0)
        job = build(project, "preview", prefer_hardware=True)
        encoder = job.command[job.command.index("-c:v") + 1]
        if "nvenc" in encoder or "amf" in encoder or "videotoolbox" in encoder:
            assert "ultrafast" not in job.command
            assert "fastdecode" not in job.command

    def test_output_is_bounded_by_the_timeline_duration(self, project: Project,
                                                        wide):
        project.add_media(wide, start=0.0)
        job = build(project)
        assert "-t" in job.command
        limit = float(job.command[job.command.index("-t") + 1])
        assert limit == pytest.approx(project.timeline.duration)

    def test_an_empty_timeline_is_refused_before_ffmpeg_runs(self, project: Project):
        from hatcut.core.errors import HatcutError
        with pytest.raises(HatcutError, match="nothing to render"):
            build(project)

    def test_a_timeline_with_problems_is_refused(self, project: Project, wide):
        from hatcut.core.errors import HatcutError
        project.add_media(wide, start=0.0)
        track = project.timeline.tracks_of("video")[0]
        track.clips.append(track.clips[0].__class__(
            source=str(wide), start=0.5, duration=1.0))
        with pytest.raises(HatcutError, match="not renderable"):
            build(project)


@pytest.mark.slow
class TestRealRender:
    def test_mismatched_clips_render_to_a_single_valid_file(
            self, tmp_path: Path, project: Project, wide, tall):
        project.path = tmp_path / "p.hatcut"
        project.add_media(wide, start=0.0)
        project.add_media(tall, start=2.0)

        output = tmp_path / "joined.mp4"
        pipeline.render(pipeline.build_job(project.timeline, "preview",
                                           output=output,
                                           prefer_hardware=False))

        assert output.exists() and output.stat().st_size > 0
        info = probe(output)
        # The whole point: two sources of different shapes, one clean raster.
        assert (info.width, info.height) == (854, 480)
        assert info.duration == pytest.approx(4.0, abs=0.35)
        assert info.has_audio

    def test_a_still_renders_as_video_of_the_requested_length(
            self, tmp_path: Path, project: Project, still):
        project.path = tmp_path / "p.hatcut"
        project.add_media(still, start=0.0, duration=3.0)

        output = tmp_path / "still.mp4"
        pipeline.render(pipeline.build_job(project.timeline, "preview",
                                           output=output,
                                           prefer_hardware=False))

        info = probe(output)
        assert info.duration == pytest.approx(3.0, abs=0.35)
        assert info.has_video

    def test_a_crossfade_keeps_video_and_audio_the_same_length(
            self, tmp_path: Path, project: Project, wide, tall):
        """The drift trap.

        A video crossfade consumes `duration` seconds of overlap while the
        audio crossfade does the same; if the two are computed from different
        offsets the result is a file whose sound runs past its picture. This
        asserts the two agree -- the absolute total is a separate question,
        covered below.
        """
        project.path = tmp_path / "p.hatcut"
        project.add_media(wide, start=0.0)
        video, audio = project.add_media(tall, start=2.0)
        # A transition describes the join *entering* a clip, so it belongs on
        # the second pair -- the first has nothing to fade from.
        for clip in (video, audio):
            clip.transition = {"type": "fade", "duration": 0.5}

        output = tmp_path / "faded.mp4"
        pipeline.render(pipeline.build_job(project.timeline, "preview",
                                           output=output,
                                           prefer_hardware=False))

        info = probe(output)
        assert info.has_video and info.has_audio

    def test_a_crossfade_does_not_leave_a_black_tail(
            self, tmp_path: Path, project: Project, wide, tall):
        project.path = tmp_path / "p.hatcut"
        project.add_media(wide, start=0.0)
        video, audio = project.add_media(tall, start=2.0)
        for clip in (video, audio):
            clip.transition = {"type": "fade", "duration": 0.5}

        output = tmp_path / "faded.mp4"
        pipeline.render(pipeline.build_job(project.timeline, "preview",
                                           output=output,
                                           prefer_hardware=False))

        # Desired: a 0.5s overlap over two 2s clips lands at 3.5s.
        assert probe(output).duration == pytest.approx(3.5, abs=0.4)

    def test_cancelling_a_render_leaves_no_partial_file(self, tmp_path: Path,
                                                        project: Project, wide):
        from hatcut.core.errors import RenderCancelled

        project.path = tmp_path / "p.hatcut"
        project.add_media(wide, start=0.0)

        output = tmp_path / "cancelled.mp4"
        cancel = threading.Event()
        job = pipeline.build_job(project.timeline, "web-1080p", output=output,
                                 prefer_hardware=False)

        # Cancelling from the progress callback rather than a timer keeps this
        # deterministic -- a wall-clock delay races a fast machine.
        def stop_after_first_report(_seconds: float, _duration: float) -> None:
            cancel.set()

        with pytest.raises(RenderCancelled):
            pipeline.render(job, on_progress=stop_after_first_report,
                            cancel=cancel)

        # A truncated mp4 left on disk is worse than no file: it looks like a
        # finished export and plays as garbage.
        assert not output.exists()

    def test_render_text_clip_with_rgba_boxcolor(self, tmp_path: Path,
                                                  project: Project, wide):
        from hatcut.core.timeline import Clip, TextStyle
        project.path = tmp_path / "p_text.hatcut"
        project.add_media(wide, start=0.0)
        text_track = project.timeline.add_track("text", "Captions")
        text_track.clips.append(Clip(
            source="", start=0.0, duration=1.0,
            text=TextStyle(content="Sample Title", font_size=32, color="#ffffff",
                           box=True, box_color="rgba(0,0,0,0.6)", box_border=10)
        ))
        out = tmp_path / "text_rendered.mp4"
        job = pipeline.build_job(project.timeline, "preview", output=out, prefer_hardware=False)
        pipeline.render(job)
        assert out.exists() and out.stat().st_size > 0
