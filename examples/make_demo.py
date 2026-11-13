#!/usr/bin/env python3
"""Build a demo project out of nothing but ffmpeg's own test sources.

    python examples/make_demo.py
    python -m hatcut serve examples/demo.hatcut

Nothing is downloaded and no footage is needed: the clips are synthetic, which
keeps the repository small and means the example always runs.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from hatcut import ffmpeg  # noqa: E402
from hatcut.core.project import Project  # noqa: E402
from hatcut.core.timeline import Clip, TextStyle  # noqa: E402
from hatcut.render import pipeline  # noqa: E402

#: Deliberately mismatched, so the example exercises the part of the pipeline
#: that has to reconcile sources rather than the part that just concatenates.
SOURCES = [
    dict(name="bars.mp4", size="1280x720", rate=30, seconds=3.0,
         sample_rate=44100, channels=1, tone=440,
         pattern="smptebars"),
    dict(name="grid.mp4", size="960x540", rate=25, seconds=3.0,
         sample_rate=48000, channels=2, tone=660,
         pattern="testsrc2=size=960x540:rate=25"),
    dict(name="logo.png", size="512x512", rate=1, seconds=1.0,
         sample_rate=48000, channels=1, tone=0, pattern="rgbtestsrc"),
]


def synth(spec: dict, out_dir: Path) -> Path:
    """Render one synthetic source, or return it if it already exists."""
    target = out_dir / spec["name"]
    if target.exists():
        return target

    binary = ffmpeg.ffmpeg_path()
    pattern = spec["pattern"]
    if "=" not in pattern:
        pattern = f"{pattern}=size={spec['size']}:rate={spec['rate']}"

    if target.suffix == ".png":
        args = [binary, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", pattern,
                "-frames:v", "1", str(target)]
    else:
        args = [
            binary, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i",
            f"{pattern}:duration={spec['seconds']}",
            "-f", "lavfi", "-i",
            f"sine=frequency={spec['tone']}:sample_rate={spec['sample_rate']}"
            f":duration={spec['seconds']}",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ac", str(spec["channels"]),
            "-t", str(spec["seconds"]),
            str(target),
        ]

    subprocess.run(args, check=True)
    return target


def main() -> int:
    media = ROOT / "media"
    media.mkdir(exist_ok=True)

    print("building synthetic sources")
    built = []
    for spec in SOURCES:
        path = synth(spec, media)
        built.append(path)
        print(f"  {path.name}")

    project = Project.new("Hatcut demo", width=1280, height=720, fps=30.0)
    project.description = "Two mismatched clips, a crossfade, a still and a caption."

    print("assembling the timeline")
    project.add_media(built[0], start=0.0)

    video, audio = project.add_media(built[1], start=3.0)
    # A crossfade entering the second clip. Setting it on the picture alone
    # would desync the audio, so both halves get it.
    for clip in (video, audio):
        if clip is not None:
            clip.transition = {"type": "fade", "duration": 0.75}

    project.add_media(built[2], start=6.0, duration=4.0)

    # A caption over the still, positioned with an ffmpeg expression so the
    # text lands centred whatever size the render turns out to be.
    project.timeline.add_clip(
        Clip(
            source="",
            start=6.5,
            duration=3.0,
            text=TextStyle(
                content="made with Hatcut",
                font_size=64,
                color="white",
                x="(w-text_w)/2",
                y="(h-text_h)/2",
                box=True,
                box_color="black@0.5",
            ),
        ),
        track_kind="text",
    )

    out = ROOT / "demo.hatcut"
    project.path = out
    project.save()
    print(f"\n{project.summary()}")
    print(f"saved {out}")

    problems = project.validate()
    if problems:
        print("problems found:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    # A still is a picture with no intrinsic length, so this is also the
    # cheapest way to check the graph compiles before anyone opens the UI.
    job = pipeline.build_job(project.timeline, "preview",
                            output=ROOT / "demo-preview.mp4",
                            prefer_hardware=True)
    print(f"\npreview render would run for {job.duration:.2f}s "
          f"using {job.command[job.command.index('-c:v') + 1]}")
    for warning in job.warnings:
        print(f"  note: {warning}")

    print("\nnext:  python -m hatcut serve examples/demo.hatcut")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
