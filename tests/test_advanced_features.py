"""Tests for the advanced editing, creative motion, audio ducking, and subtitle features."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from hatcut.core.effects import EffectContext, build_all, get as get_effect
from hatcut.core.project import Project
from hatcut.core.subtitles import (
    cues_to_clips,
    export_srt,
    export_vtt,
    parse_subtitles,
    parse_timestamp,
)
from hatcut.core.timeline import Clip, Marker, TextStyle, Timeline, Track, Transform
from hatcut.render.filters import RenderContext, transform_filters
from hatcut.render.graph import build_plan
from hatcut.server.app import Workspace


# ---------------------------------------------------------------------------
# 1. Timeline Markers
# ---------------------------------------------------------------------------

def test_timeline_markers_lifecycle():
    timeline = Timeline()
    assert len(timeline.markers) == 0

    m1 = timeline.add_marker(3.5, name="Drop", color="#ef4444")
    m2 = timeline.add_marker(1.2, name="Intro", color="#3b82f6")
    m3 = timeline.add_marker(8.0, name="Outro", color="#10b981")

    # Sorted by timestamp
    assert [m.time for m in timeline.markers] == [1.2, 3.5, 8.0]
    assert timeline.find_marker(m1.id).name == "Drop"

    # Serialization roundtrip
    data = timeline.to_dict()
    assert "markers" in data
    assert len(data["markers"]) == 3

    restored = Timeline.from_dict(data)
    assert len(restored.markers) == 3
    assert restored.markers[0].name == "Intro"
    assert restored.markers[1].time == 3.5

    # Removal
    restored.remove_marker(m2.id)
    assert len(restored.markers) == 2
    assert restored.markers[0].name == "Drop"


# ---------------------------------------------------------------------------
# 2. Subtitles: SRT & VTT Parsing & Export
# ---------------------------------------------------------------------------

def test_subtitle_parsing_and_export():
    srt_content = """1
00:00:01,250 --> 00:00:04,500
Welcome to Hatcut video editor!

2
00:00:05,000 --> 00:00:09,100
Fully client-rendered and zero dependencies.
"""
    cues = parse_subtitles(srt_content)
    assert len(cues) == 2
    assert cues[0]["start"] == 1.25
    assert cues[0]["end"] == 4.5
    assert abs(cues[0]["duration"] - 3.25) < 1e-4
    assert cues[0]["text"] == "Welcome to Hatcut video editor!"
    assert cues[1]["start"] == 5.0

    # Convert to clips
    clips = cues_to_clips(cues)
    assert len(clips) == 2
    assert clips[0].start == 1.25
    assert clips[0].text.content == "Welcome to Hatcut video editor!"

    # Export SRT
    exported_srt = export_srt(clips)
    assert "00:00:01,250 --> 00:00:04,500" in exported_srt
    assert "Welcome to Hatcut video editor!" in exported_srt

    # Export VTT
    exported_vtt = export_vtt(clips)
    assert exported_vtt.startswith("WEBVTT")
    assert "00:00:01.250 --> 00:00:04.500" in exported_vtt

    # WebVTT input parsing
    vtt_content = """WEBVTT

1
00:01.000 --> 00:03.500
Short timestamp VTT cue
"""
    vtt_cues = parse_subtitles(vtt_content)
    assert len(vtt_cues) == 1
    assert vtt_cues[0]["start"] == 1.0
    assert vtt_cues[0]["duration"] == 2.5
    assert vtt_cues[0]["text"] == "Short timestamp VTT cue"


# ---------------------------------------------------------------------------
# 3. Creative Effects: Chroma Key, 3D LUT, Preset Looks, Pitch
# ---------------------------------------------------------------------------

def test_chroma_key_effect():
    spec = get_effect("chroma_key")
    ctx = EffectContext(duration=5.0)

    # Standard green screen
    params = spec.normalise({"color": "#00FF00", "similarity": 0.35, "blend": 0.12, "spill": 0.5})
    filter_str = spec.builder(params, ctx)
    assert "colorkey=color=0x00FF00:similarity=0.3500:blend=0.1200" in filter_str
    assert "despill=type=green:mix=0.50" in filter_str

    # Blue screen
    params_blue = spec.normalise({"color": "#0000FF", "similarity": 0.4, "blend": 0.1, "spill": 0.6})
    filter_blue = spec.builder(params_blue, ctx)
    assert "colorkey=color=0x0000FF" in filter_blue
    assert "despill=type=blue:mix=0.60" in filter_blue


def test_lut3d_effect():
    spec = get_effect("lut3d")
    ctx = EffectContext(duration=5.0)

    # Built-in sample LUT
    params = spec.normalise({"file": "cinematic_warm.cube", "interp": "tetrahedral"})
    filter_str = spec.builder(params, ctx)
    assert "lut3d=file=" in filter_str
    assert "cinematic_warm.cube" in filter_str
    assert "interp=tetrahedral" in filter_str


def test_preset_color_looks():
    spec = get_effect("preset_look")
    ctx = EffectContext(duration=5.0)

    looks = ("film_warm", "clean_cold", "cyberpunk", "vintage", "noir", "teal_and_orange", "bleach_bypass")
    for look in looks:
        params = spec.normalise({"look": look, "intensity": 0.8})
        filter_str = spec.builder(params, ctx)
        assert filter_str and filter_str != "null"


def test_pitch_effect():
    spec = get_effect("pitch")
    ctx = EffectContext(duration=5.0, has_audio=True)

    params = spec.normalise({"semitones": 3.0, "preserve_tempo": True})
    filter_str = spec.builder(params, ctx)
    assert "rubberband=pitch=" in filter_str or "asetrate=" in filter_str


# ---------------------------------------------------------------------------
# 4. Transform Keyframing (Ken Burns / Pan & Zoom / Motion)
# ---------------------------------------------------------------------------

def test_transform_keyframing():
    clip = Clip(
        start=0.0,
        duration=4.0,
        transform_keyframes=[
            {"time": 0.0, "scale": 1.0, "x": 0, "y": 0, "rotate": 0.0, "opacity": 1.0},
            {"time": 4.0, "scale": 1.5, "x": 100, "y": 50, "rotate": 15.0, "opacity": 0.5},
        ]
    )
    ctx = RenderContext(width=1920, height=1080, fps=30.0)
    filters = transform_filters(clip, ctx)

    # Should contain zoompan for scale/pan animation
    assert any("zoompan=" in f for f in filters)
    # Should contain rotate with dynamic time expression
    assert any("rotate=" in f for f in filters)
    # Should contain geq for animated alpha
    assert any("geq=" in f for f in filters)


# ---------------------------------------------------------------------------
# 5. Overlay Blend Modes in Render Plan
# ---------------------------------------------------------------------------

def test_overlay_blend_modes_in_plan(project: Project, wide):
    timeline = project.timeline
    t1 = timeline.ensure_track("video")
    t2 = timeline.add_track("video", "V2")
    t2.blend_mode = "screen"

    c1 = Clip(start=0.0, duration=2.0, source=str(wide))
    c2 = Clip(start=0.0, duration=2.0, source=str(wide))
    t1.clips.append(c1)
    t2.clips.append(c2)

    plan = timeline.plan()
    assert "blend=all_mode=screen" in plan.filter_complex


# ---------------------------------------------------------------------------
# 6. Audio Ducking (Sidechain Compression) in Render Plan
# ---------------------------------------------------------------------------

def test_audio_ducking_in_plan(project: Project, wide):
    timeline = project.timeline
    v_track = timeline.ensure_track("video")
    v_track.clips.append(Clip(start=0.0, duration=4.0, source=str(wide)))

    a_voice = timeline.ensure_track("audio")
    a_music = timeline.add_track("audio", "Music")
    a_music.ducking = True
    a_music.duck_threshold = 0.08
    a_music.duck_ratio = 5.0

    a_voice.clips.append(Clip(start=1.0, duration=2.0, source=str(wide)))
    a_music.clips.append(Clip(start=0.0, duration=4.0, source=str(wide)))

    plan = timeline.plan()
    assert "sidechaincompress=" in plan.filter_complex
    assert "threshold=0.0800" in plan.filter_complex
    assert "ratio=5.00" in plan.filter_complex


# ---------------------------------------------------------------------------
# 7. Workspace Batch Clips & Subtitles API
# ---------------------------------------------------------------------------

def test_workspace_batch_clips_and_subtitles():
    project = Project.new("Batch Test")
    ws = Workspace(project, autosave=False)

    # Batch add and move clips
    t = project.timeline.ensure_track("video")
    c1 = Clip(start=0.0, duration=2.0, source="test1.mp4")
    c2 = Clip(start=3.0, duration=2.0, source="test2.mp4")
    t.clips.extend([c1, c2])

    ws.move_clips([
        {"id": c1.id, "start": 1.0},
        {"id": c2.id, "start": 5.0},
    ])
    assert c1.start == 1.0
    assert c2.start == 5.0

    # Subtitle import into workspace
    srt = """1
00:00:00,500 --> 00:00:02,500
Hello World

2
00:00:03,000 --> 00:00:05,000
Hatcut Subtitles
"""
    track = ws.import_subtitles(srt, track_name="Test Subs")
    assert track["kind"] == "text"
    assert len(track["clips"]) == 2

    # Subtitle export from workspace
    exported = ws.export_subtitles(format="srt")
    assert "00:00:00,500 --> 00:00:02,500" in exported
    assert "Hello World" in exported

    # Batch delete
    ws.delete_clips([c1.id, c2.id])
    assert len(t.clips) == 0
