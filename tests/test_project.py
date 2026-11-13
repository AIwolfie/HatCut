"""Project file format and media registration.

A project file is the one artifact a user is expected to keep for years, so
the round-trip is tested harder than the editing operations: anything that
fails to survive a save/load cycle is data loss.
"""

from __future__ import annotations

import json

import pytest

from hatcut.core.errors import ProjectError
from hatcut.core.project import Project


class TestRoundTrip:
    def test_an_empty_project_survives_json(self, project: Project):
        restored = Project.from_dict(json.loads(json.dumps(project.to_dict())))
        assert restored.name == project.name
        assert restored.timeline.width == project.timeline.width
        # A new project has its default V1/A1 lanes; "empty" means no clips,
        # not no tracks.
        assert all(t.clips == [] for t in restored.timeline.tracks)

    def test_settings_survive(self, project: Project):
        project.timeline.fps = 23.976
        project.timeline.sample_rate = 44100
        restored = Project.from_dict(json.loads(json.dumps(project.to_dict())))
        assert restored.timeline.fps == pytest.approx(23.976)
        assert restored.timeline.sample_rate == 44100

    def test_the_description_survives(self, project: Project):
        project.description = "a test project"
        restored = Project.from_dict(json.loads(json.dumps(project.to_dict())))
        assert restored.description == "a test project"

    def test_unknown_keys_do_not_break_loading(self, project: Project):
        # A file written by a newer version must not explode an older one.
        data = project.to_dict()
        data["something_from_the_future"] = {"nested": True}
        assert Project.from_dict(data) is not None

    def test_the_file_does_not_record_its_own_location(self, project: Project):
        # An absolute path baked into the project breaks the moment the file
        # is moved or opened on another machine, so `path` is not serialised
        # at all -- `from_dict` takes it as a separate argument.
        assert "path" not in project.to_dict()

    def test_loading_supplies_the_path_out_of_band(self, project: Project):
        from pathlib import Path
        restored = Project.from_dict(project.to_dict(),
                                     path=Path("C:/somewhere/p.hatcut"))
        assert restored.path == Path("C:/somewhere/p.hatcut")


class TestMediaRegistration:
    @pytest.mark.usefixtures("media_dir")
    def test_registering_media_populates_the_cache(self, project: Project, wide):
        project.register_media(wide)
        assert str(wide) in project.media_cache
        assert project.media_cache[str(wide)]["width"] == 1280

    @pytest.mark.usefixtures("media_dir")
    def test_add_media_places_a_linked_pair(self, project: Project, wide):
        video, audio = project.add_media(wide, start=0.0)
        assert video is not None and audio is not None
        # One link id is what keeps a later retime from desyncing the two.
        assert video.link_id == audio.link_id

    @pytest.mark.usefixtures("media_dir")
    def test_add_media_without_audio_places_only_picture(self, project: Project,
                                                         wide):
        video, audio = project.add_media(wide, start=0.0, with_audio=False)
        assert video is not None
        assert audio is None

    @pytest.mark.usefixtures("media_dir")
    def test_a_still_registers_as_an_image(self, project: Project, still):
        project.register_media(still)
        assert project.media_cache[str(still)]["is_image"] is True

    def test_a_missing_file_is_reported_not_crashed(self, project: Project):
        with pytest.raises(Exception):
            project.register_media("definitely_not_here.mp4")

    @pytest.mark.usefixtures("media_dir")
    def test_a_present_file_is_not_reported_missing(self, project: Project, wide):
        project.add_media(wide, start=0.0)
        assert project.missing_media() == []

    @pytest.mark.usefixtures("media_dir")
    def test_deleting_the_source_makes_it_report_missing(
            self, project: Project, wide, tmp_path):
        # A copy, so the session-scoped fixture media survives for other tests.
        import shutil
        doomed = tmp_path / "doomed.mp4"
        shutil.copy(wide, doomed)
        project.add_media(doomed, start=0.0)
        assert project.missing_media() == []

        doomed.unlink()
        # The project still references the path; that has to be reported, not
        # discovered at render time.
        assert str(doomed) in project.missing_media()


class TestPersistence:
    @pytest.mark.usefixtures("media_dir")
    def test_save_then_load_preserves_the_edit(self, tmp_path, wide):
        project = Project.new("persisted", width=1280, height=720, fps=30.0)
        project.path = tmp_path / "p.hatcut"
        project.add_media(wide, start=0.0)
        project.save()

        loaded = Project.load(project.path)
        assert loaded.name == "persisted"
        assert loaded.timeline.duration == pytest.approx(2.0, abs=0.2)
        assert len(loaded.timeline.tracks) == 2

    @pytest.mark.usefixtures("media_dir")
    def test_relative_paths_allow_moving_project_folder(self, tmp_path, wide):
        import shutil
        from pathlib import Path
        proj_dir = tmp_path / "project_a"
        proj_dir.mkdir()
        media_subdir = proj_dir / "media"
        media_subdir.mkdir()
        copied_wide = media_subdir / "video.mp4"
        shutil.copy(wide, copied_wide)

        project = Project.new("portable", width=1280, height=720, fps=30.0)
        project.path = proj_dir / "portable.hatcut"
        project.add_media(copied_wide, start=0.0)
        project.save()

        # Verify on-disk JSON uses relative path
        raw_json = json.loads((proj_dir / "portable.hatcut").read_text(encoding="utf-8"))
        clip_source = raw_json["tracks"][0]["clips"][0]["source"]
        assert clip_source == "media/video.mp4"

        # Now move the entire folder to another location
        moved_dir = tmp_path / "project_b"
        shutil.move(proj_dir, moved_dir)

        # Load from new location
        loaded = Project.load(moved_dir / "portable.hatcut")
        assert loaded.missing_media() == []
        clip = loaded.timeline.tracks[0].clips[0]
        assert Path(clip.source).exists()
        assert Path(clip.source).resolve() == (moved_dir / "media" / "video.mp4").resolve()

    def test_save_is_atomic_and_leaves_no_temp_files(self, tmp_path, project):
        project.path = tmp_path / "p.hatcut"
        project.save()
        leftovers = [p.name for p in tmp_path.iterdir() if p.name != "p.hatcut"]
        assert leftovers == []

    def test_save_without_a_path_is_refused(self, project: Project):
        with pytest.raises(ProjectError):
            project.save()

    def test_loading_a_nonexistent_file_raises_a_project_error(self, tmp_path):
        # ProjectError rather than FileNotFoundError: callers should not have
        # to know which layer noticed, and the CLI reports both the same way.
        with pytest.raises(ProjectError, match="no such project"):
            Project.load(tmp_path / "nope.hatcut")

    def test_loading_malformed_json_raises_a_project_error(self, tmp_path):
        bad = tmp_path / "bad.hatcut"
        bad.write_text("{ this is not json", encoding="utf-8")
        with pytest.raises(ProjectError):
            Project.load(bad)


class TestSummary:
    @pytest.mark.usefixtures("media_dir")
    def test_summary_describes_the_edit_in_one_line(self, project: Project, wide):
        project.add_media(wide, start=0.0)
        summary = project.summary()
        # It is a display string, not a data structure -- the JSON form is
        # `to_dict()`, and the two serve different callers.
        assert summary.startswith("test: ")
        assert "2 tracks" in summary
        assert "2 clips" in summary
        assert "1280x720" in summary
        assert "\n" not in summary
