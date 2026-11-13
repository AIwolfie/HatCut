"""Tests for server workspace methods, peaks extraction, and snapshot restoration."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from hatcut.core.project import Project
from hatcut.server.app import Workspace

pytestmark = pytest.mark.usefixtures("media_dir")


class TestServerWorkspace:
    def test_peaks_extraction_and_caching(self, tmp_path: Path, wide):
        project = Project.new("test_peaks")
        workspace = Workspace(project, cache_root=tmp_path / "cache")

        peaks = workspace.peaks(str(wide), samples_per_sec=20)
        assert isinstance(peaks, list)
        assert len(peaks) > 0
        assert all(0.0 <= p <= 1.0 for p in peaks)

        # Second call hits cache
        cached_peaks = workspace.peaks(str(wide), samples_per_sec=20)
        assert cached_peaks == peaks

    def test_peaks_on_still_image_returns_empty(self, tmp_path: Path, still):
        project = Project.new("test_still_peaks")
        workspace = Workspace(project, cache_root=tmp_path / "cache")

        peaks = workspace.peaks(str(still))
        assert peaks == []

    def test_restore_snapshot(self, tmp_path: Path, wide):
        project = Project.new("orig")
        workspace = Workspace(project, cache_root=tmp_path / "cache")
        workspace.import_media(str(wide))

        snapshot_before = workspace.snapshot()
        assert len(snapshot_before["timeline"]["tracks"][0]["clips"]) == 1

        # Delete clip
        clip_id = snapshot_before["timeline"]["tracks"][0]["clips"][0]["id"]
        workspace.delete_clip(clip_id)
        assert len(workspace.snapshot()["timeline"]["tracks"][0]["clips"]) == 0

        # Restore from snapshot
        workspace.restore_snapshot(snapshot_before)
        snapshot_after = workspace.snapshot()
        assert len(snapshot_after["timeline"]["tracks"][0]["clips"]) == 1
        assert snapshot_after["timeline"]["tracks"][0]["clips"][0]["id"] == clip_id

    def test_snapshot_contains_catalogs_and_presets(self, tmp_path: Path):
        project = Project.new("catalogs_test")
        workspace = Workspace(project, cache_root=tmp_path / "cache")
        snap = workspace.snapshot()
        assert "presets" in snap and len(snap["presets"]) > 0
        assert any(p["name"] == "web-1080p" for p in snap["presets"])
        assert "transitions" in snap and len(snap["transitions"]) > 0
        assert "effects" in snap and len(snap["effects"]) > 0
        assert "encoders" in snap

    def test_render_job_serialization(self, tmp_path: Path):
        from hatcut.server.app import RenderJobState
        out_file = tmp_path / "test_out.mp4"
        out_file.write_bytes(b"\x00" * 1024)
        job = RenderJobState(id="job_123", preset="web-1080p", output=out_file, status="done", duration=5.0)
        d = job.to_dict()
        assert d["id"] == "job_123"
        assert d["download_url"] == "/api/render/job_123/download"
        assert d["output_absolute"] == str(out_file.resolve())
        assert d["size_bytes"] == 1024
