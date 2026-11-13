"""Shared fixtures.

Every fixture here builds its own media with ffmpeg rather than pointing at
files in the repository. A test that depends on a checked-in binary breaks the
moment someone regenerates it, and it hides the fact that the interesting
inputs are the *mismatched* ones -- different sizes, frame rates and audio
layouts -- which is exactly what a timeline has to reconcile.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hatcut import ffmpeg
from hatcut.core.project import Project

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

#: Two clips that agree on nothing: different raster, frame rate, sample rate
#: and channel layout. Anything that joins them has to normalise first.
CLIP_SPECS = {
    "wide": dict(size="1280x720", rate=30, duration=2.0,
                 sample_rate=44100, channels=1, freq=440),
    "tall": dict(size="640x640", rate=25, duration=2.0,
                 sample_rate=48000, channels=2, freq=660),
}


def _run(args: list[str]) -> None:
    result = subprocess.run(args, capture_output=True, text=True,
                            creationflags=_CREATE_NO_WINDOW)
    if result.returncode != 0:
        raise RuntimeError(f"fixture generation failed: {' '.join(args)}\n{result.stderr}")


def have_ffmpeg() -> bool:
    try:
        ffmpeg.ffmpeg_path()
        return True
    except Exception:
        return False


requires_ffmpeg = pytest.mark.skipif(
    not have_ffmpeg(), reason="ffmpeg is not on PATH")


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory) -> Path:
    """Synthetic source media, built once for the whole session."""
    root = tmp_path_factory.mktemp("media")
    ffmpeg_bin = ffmpeg.ffmpeg_path()

    for name, spec in CLIP_SPECS.items():
        target = root / f"{name}.mp4"
        _run([
            ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i",
            f"testsrc2=size={spec['size']}:rate={spec['rate']}:duration={spec['duration']}",
            "-f", "lavfi", "-i",
            f"sine=frequency={spec['freq']}:sample_rate={spec['sample_rate']}:duration={spec['duration']}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-ac", str(spec["channels"]),
            "-t", str(spec["duration"]),
            str(target),
        ])

    # A still, so image handling is covered too.
    _run([
        ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=800x600:rate=1:duration=1",
        "-frames:v", "1", str(root / "still.png"),
    ])

    return root


@pytest.fixture
def wide(media_dir: Path) -> Path:
    return media_dir / "wide.mp4"


@pytest.fixture
def tall(media_dir: Path) -> Path:
    return media_dir / "tall.mp4"


@pytest.fixture
def still(media_dir: Path) -> Path:
    return media_dir / "still.png"


@pytest.fixture
def project(tmp_path: Path) -> Project:
    """An empty 1280x720 @ 30 project in a throwaway directory."""
    return Project.new("test", width=1280, height=720, fps=30.0)


@pytest.fixture
def saved_project(tmp_path: Path, project: Project) -> Project:
    project.path = tmp_path / "proj.hatcut"
    return project
