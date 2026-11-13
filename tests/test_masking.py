"""Tests for shape masking and free masking."""

from __future__ import annotations

from pathlib import Path
import pytest

from hatcut.core.effects import EffectContext, build_all, get
from hatcut.core.project import Project
from hatcut.core.timeline import Clip
from hatcut.render import pipeline
from hatcut.core.media import probe

pytestmark = pytest.mark.usefixtures("media_dir")


class TestMaskBuilders:
    def test_rectangle_mask_default(self):
        spec = get("rectangle_mask")
        ctx = EffectContext(duration=5.0, width=1920, height=1080)
        params = spec.normalise({})
        filt = spec.builder(params, ctx)
        assert "format=rgba" in filt
        assert "geq=" in filt
        assert "if(" in filt

    def test_rectangle_mask_rounded_and_feathered(self):
        spec = get("rectangle_mask")
        ctx = EffectContext(duration=5.0, width=1920, height=1080)
        params = spec.normalise({"round": 0.2, "feathering": 15.0, "inverted": True})
        filt = spec.builder(params, ctx)
        assert "gblur=sigma=15.00:planes=8" in filt
        assert "if(" in filt

    def test_ellipse_mask_with_rotation(self):
        spec = get("ellipse_mask")
        ctx = EffectContext(duration=5.0, width=1920, height=1080)
        params = spec.normalise({"cx": 0.5, "cy": 0.5, "rx": 0.4, "ry": 0.2, "rotation": 45.0, "feathering": 10.0})
        filt = spec.builder(params, ctx)
        assert "format=rgba" in filt
        assert "hypot(" in filt
        assert "gblur=sigma=10.00:planes=8" in filt

    def test_freeform_mask_and_triangulation(self):
        spec = get("freeform_mask")
        ctx = EffectContext(duration=5.0, width=1000, height=1000)
        points = [
            {"x": 0.2, "y": 0.2},
            {"x": 0.5, "y": 0.1},
            {"x": 0.8, "y": 0.3},
            {"x": 0.7, "y": 0.8},
            {"x": 0.3, "y": 0.7},
        ]
        params = spec.normalise({"points": points, "feathering": 8.0, "inverted": False})
        filt = spec.builder(params, ctx)
        assert "format=rgba" in filt
        assert "geq=" in filt
        assert "gblur=sigma=8.00:planes=8" in filt

    def test_polygon_mask_clamping(self):
        spec = get("polygon_mask")
        params = spec.normalise({
            "points": [(-0.5, 0.2), (1.5, 0.8), {"x": 0.5, "y": 0.5}],
            "feathering": -10,
        })
        assert len(params["points"]) == 3
        assert params["points"][0]["x"] == 0.0
        assert params["points"][1]["x"] == 1.0
        assert params["feathering"] == 0.0

    def test_linear_mask_builder(self):
        spec = get("linear_mask")
        ctx = EffectContext(duration=5.0, width=1920, height=1080)
        params = spec.normalise({"x": 0.5, "y": 0.5, "angle": 45.0, "feathering": 10.0, "inverted": True})
        filt = spec.builder(params, ctx)
        assert "format=rgba" in filt
        assert "geq=" in filt
        assert "gte(" in filt
        assert "gblur=sigma=10.00:planes=8" in filt

    def test_keyframe_mask_builder(self):
        ctx = EffectContext(duration=5.0, width=1920, height=1080)
        effects = [{
            "type": "rectangle_mask",
            "params": {"x": 0.2, "y": 0.2, "w": 0.6, "h": 0.6},
            "keyframes": [
                {"time": 0.0, "params": {"x": 0.1, "y": 0.1}},
                {"time": 2.5, "params": {"x": 0.4, "y": 0.3}},
            ],
        }]
        filters = build_all(effects, ctx)
        assert len(filters) == 1
        assert "T" in filters[0]
        assert "if(lt(T," in filters[0]


@pytest.mark.slow
class TestMaskRendering:
    def test_render_with_rectangle_mask(self, tmp_path: Path, project: Project, wide):
        project.path = tmp_path / "p.hatcut"
        video, _ = project.add_media(wide, start=0.0)
        video.effects.append({
            "type": "rectangle_mask",
            "params": {"x": 0.2, "y": 0.2, "w": 0.6, "h": 0.6, "round": 0.1, "feathering": 5.0},
        })

        output = tmp_path / "masked_rect.mp4"
        job = pipeline.build_job(project.timeline, "preview", output=output, prefer_hardware=False)
        pipeline.render(job)

        assert output.exists() and output.stat().st_size > 0
        info = probe(output)
        assert info.has_video

    def test_render_with_ellipse_mask(self, tmp_path: Path, project: Project, wide):
        project.path = tmp_path / "p.hatcut"
        video, _ = project.add_media(wide, start=0.0)
        video.effects.append({
            "type": "ellipse_mask",
            "params": {"cx": 0.5, "cy": 0.5, "rx": 0.3, "ry": 0.3, "rotation": 30.0, "feathering": 4.0},
        })

        output = tmp_path / "masked_ellipse.mp4"
        job = pipeline.build_job(project.timeline, "preview", output=output, prefer_hardware=False)
        pipeline.render(job)

        assert output.exists() and output.stat().st_size > 0
        info = probe(output)
        assert info.has_video

    def test_render_with_freeform_mask(self, tmp_path: Path, project: Project, wide):
        project.path = tmp_path / "p.hatcut"
        video, _ = project.add_media(wide, start=0.0)
        video.effects.append({
            "type": "freeform_mask",
            "params": {
                "points": [
                    {"x": 0.3, "y": 0.1},
                    {"x": 0.8, "y": 0.2},
                    {"x": 0.9, "y": 0.7},
                    {"x": 0.4, "y": 0.9},
                    {"x": 0.1, "y": 0.5},
                ],
                "feathering": 6.0,
                "inverted": False,
            },
        })

        output = tmp_path / "masked_freeform.mp4"
        job = pipeline.build_job(project.timeline, "preview", output=output, prefer_hardware=False)
        pipeline.render(job)

        assert output.exists() and output.stat().st_size > 0
        info = probe(output)
        assert info.has_video

    def test_render_with_linear_mask(self, tmp_path: Path, project: Project, wide):
        project.path = tmp_path / "p.hatcut"
        video, _ = project.add_media(wide, start=0.0)
        video.effects.append({
            "type": "linear_mask",
            "params": {"x": 0.5, "y": 0.5, "angle": 45.0, "feathering": 5.0, "inverted": False},
        })

        output = tmp_path / "masked_linear.mp4"
        job = pipeline.build_job(project.timeline, "preview", output=output, prefer_hardware=False)
        pipeline.render(job)

        assert output.exists() and output.stat().st_size > 0
        info = probe(output)
        assert info.has_video

    def test_render_with_keyframe_animated_mask(self, tmp_path: Path, project: Project, wide):
        project.path = tmp_path / "p.hatcut"
        video, _ = project.add_media(wide, start=0.0)
        video.effects.append({
            "type": "rectangle_mask",
            "params": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5},
            "keyframes": [
                {"time": 0.0, "params": {"x": 0.1, "y": 0.1}},
                {"time": 1.0, "params": {"x": 0.4, "y": 0.3}},
            ],
        })

        output = tmp_path / "masked_keyframe.mp4"
        job = pipeline.build_job(project.timeline, "preview", output=output, prefer_hardware=False)
        pipeline.render(job)

        assert output.exists() and output.stat().st_size > 0
        info = probe(output)
        assert info.has_video

        info = probe(output)
        assert info.has_video


    def test_render_with_freeform_mask(self, tmp_path: Path, project: Project, wide):
        project.path = tmp_path / "p.hatcut"
        video, _ = project.add_media(wide, start=0.0)
        video.effects.append({
            "type": "freeform_mask",
            "params": {
                "points": [
                    {"x": 0.3, "y": 0.1},
                    {"x": 0.8, "y": 0.2},
                    {"x": 0.9, "y": 0.7},
                    {"x": 0.4, "y": 0.9},
                    {"x": 0.1, "y": 0.5},
                ],
                "feathering": 6.0,
                "inverted": False,
            },
        })

        output = tmp_path / "masked_freeform.mp4"
        job = pipeline.build_job(project.timeline, "preview", output=output, prefer_hardware=False)
        pipeline.render(job)

        assert output.exists() and output.stat().st_size > 0
        info = probe(output)
        assert info.has_video

    def test_render_with_linear_mask(self, tmp_path: Path, project: Project, wide):
        project.path = tmp_path / "p.hatcut"
        video, _ = project.add_media(wide, start=0.0)
        video.effects.append({
            "type": "linear_mask",
            "params": {"x": 0.5, "y": 0.5, "angle": 45.0, "feathering": 5.0, "inverted": False},
        })

        output = tmp_path / "masked_linear.mp4"
        job = pipeline.build_job(project.timeline, "preview", output=output, prefer_hardware=False)
        pipeline.render(job)

        assert output.exists() and output.stat().st_size > 0
        info = probe(output)
        assert info.has_video

    def test_render_with_keyframe_animated_mask(self, tmp_path: Path, project: Project, wide):
        project.path = tmp_path / "p.hatcut"
        video, _ = project.add_media(wide, start=0.0)
        video.effects.append({
            "type": "rectangle_mask",
            "params": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5},
            "keyframes": [
                {"time": 0.0, "params": {"x": 0.1, "y": 0.1}},
                {"time": 1.0, "params": {"x": 0.4, "y": 0.3}},
            ],
        })

        output = tmp_path / "masked_keyframe.mp4"
        job = pipeline.build_job(project.timeline, "preview", output=output, prefer_hardware=False)
        pipeline.render(job)

        assert output.exists() and output.stat().st_size > 0
        info = probe(output)
        assert info.has_video

