"""Hatcut -- a programmable non-linear video editor.

The library is the product; the CLI and the web UI are both thin clients over
it. Everything you can do by clicking, you can do from Python::

    from hatcut import Project, presets

    project = Project.new("My film", width=1920, height=1080, fps=30)
    project.add_media("shot1.mp4")
    project.add_media("shot2.mp4")

    # Cross-fade the join between them.
    _, second = project.timeline.tracks_of("video")[0].clips[-1], None
    video_track = project.timeline.tracks_of("video")[0]
    video_track.sorted_clips()[1].transition = {"type": "fade", "duration": 0.8}

    project.save("film.hatcut")
    project.timeline.render("web-1080p", "film.mp4")

"""

from __future__ import annotations

from .core import effects, media
from .core.errors import (
    HatcutError,
    MediaNotFoundError,
    PresetError,
    ProbeError,
    ProjectError,
    RenderCancelled,
    RenderError,
    TimelineError,
    ToolMissingError,
)
from .core.project import FORMAT_VERSION, PROJECT_SUFFIX, Project
from .core.timeline import Clip, TextStyle, Timeline, Track, Transform
from .export import encoder, presets
from .render import graph, pipeline, transitions

__version__ = "0.1.0"

__all__ = [
    "Clip",
    "FORMAT_VERSION",
    "HatcutError",
    "MediaNotFoundError",
    "PROJECT_SUFFIX",
    "PresetError",
    "ProbeError",
    "Project",
    "ProjectError",
    "RenderCancelled",
    "RenderError",
    "TextStyle",
    "Timeline",
    "TimelineError",
    "ToolMissingError",
    "Track",
    "Transform",
    "__version__",
    "effects",
    "encoder",
    "graph",
    "media",
    "pipeline",
    "presets",
    "transitions",
]
