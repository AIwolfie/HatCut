"""The ``hatcut`` command line.

Every subcommand here is a thin shell over the library. Nothing is implemented
twice: if a capability is missing from the CLI it is because it is missing from
the API, and the fix belongs there.

Subcommands are ordered the way you actually use them -- probe what you have,
build a project, look at it, render it -- so ``hatcut --help`` reads as a
workflow rather than an alphabet.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__, ffmpeg, presets
from .core import media
from .core.errors import HatcutError
from .core.project import PROJECT_SUFFIX, Project

#: Media ffprobe can be asked about. Kept loose on purpose -- ffmpeg reads far
#: more than any list we could write, and refusing an extension it understands
#: would be stupid.
PROBE_HINT = "any file ffmpeg can read"


# ---------------------------------------------------------------------------
# terminal helpers
# ---------------------------------------------------------------------------

class _Bar:
    """A render progress bar that degrades gracefully.

    Writes to stderr so that piping stdout stays clean, and falls back to
    occasional plain lines when stderr is not a terminal -- a redirected log
    with 2000 carriage returns in it is worse than no progress at all.
    """

    def __init__(self, width: int = 28, enabled: bool = True) -> None:
        self.width = width
        self.enabled = enabled and sys.stderr.isatty()
        self._last_line = 0.0
        self._finished = False

    def update(self, done: float, total: float) -> None:
        if not self.enabled:
            # Non-tty: emit a line at most every 5 seconds.
            now = time.monotonic()
            if now - self._last_line < 5.0 and done < total:
                return
            self._last_line = now
            print(f"  {done:6.1f}s / {total:.1f}s", file=sys.stderr)
            return

        fraction = 0.0 if total <= 0 else max(0.0, min(1.0, done / total))
        filled = int(self.width * fraction)
        bar = "#" * filled + "-" * (self.width - filled)
        sys.stderr.write(f"\r  [{bar}] {fraction * 100:5.1f}%  {done:6.1f}s")
        sys.stderr.flush()

    def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        if self.enabled:
            sys.stderr.write("\n")
            sys.stderr.flush()


def _fail(message: str, code: int = 1) -> int:
    print(f"hatcut: {message}", file=sys.stderr)
    return code


def _human_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _human_time(seconds: float) -> str:
    minutes, secs = divmod(max(0.0, seconds), 60)
    hours, minutes = divmod(int(minutes), 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:04.1f}s"
    return f"{int(minutes)}m{secs:04.1f}s" if minutes else f"{secs:.2f}s"


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------

def cmd_probe(args: argparse.Namespace) -> int:
    """Describe media files: streams, duration, frame size, rotation."""
    exit_code = 0
    for index, name in enumerate(args.files):
        if index and not args.json:
            print()
        try:
            info = media.probe(name)
        except HatcutError as exc:
            # Report and keep going: a batch probe is more useful when one bad
            # file does not hide the other nineteen.
            print(f"{name}: {exc}", file=sys.stderr)
            exit_code = 1
            continue

        if args.json:
            print(json.dumps(info.to_dict(), indent=2))
            continue

        print(f"{info.path.name}")
        print(f"  path      {info.path}")
        if info.container:
            print(f"  format    {info.container}")
        print(f"  duration  {_human_time(info.duration)}"
              f"  ({info.duration:.3f}s)")
        if info.has_video:
            print(f"  video     {info.video_codec or '?'} "
                  f"{info.width}x{info.height} @ {info.fps:g}fps")
            if info.rotation:
                print(f"  rotation  {info.rotation:g} deg")
        if info.has_audio:
            print(f"  audio     {info.audio_codec or '?'} "
                  f"{info.sample_rate}Hz {info.channels}ch")
        if info.size_bytes:
            print(f"  size      {_human_bytes(info.size_bytes)}")
        if info.is_image:
            print("  note      still image (no intrinsic duration)")
    return exit_code


# ---------------------------------------------------------------------------
# project lifecycle
# ---------------------------------------------------------------------------

def cmd_new(args: argparse.Namespace) -> int:
    """Create an empty project."""
    project = Project.new(args.name or Path(args.project).stem,
                          width=args.width, height=args.height, fps=args.fps)
    if args.size:
        width, _, height = args.size.lower().partition("x")
        try:
            project.timeline.width = int(width)
            project.timeline.height = int(height)
        except ValueError:
            return _fail(f"--size wants WxH, got {args.size!r}")

    problems = project.timeline.validate()
    if problems:
        return _fail("; ".join(problems))

    path = project.save(args.project)
    print(f"created {path}  ({project.timeline.width}x{project.timeline.height}"
          f" @ {project.timeline.fps:g}fps)")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    """Append media to a project, linking each file's picture and sound."""
    project = Project.load(args.project)
    added = 0
    failed = 0

    for name in args.files:
        try:
            video, audio = project.add_media(
                name,
                start=None if args.append else args.start,
                in_point=args.in_point,
                duration=args.duration,
                with_audio=not args.no_audio,
                track_id=args.track,
                speed=args.speed,
            )
        except HatcutError as exc:
            print(f"hatcut: {name}: {exc}", file=sys.stderr)
            failed += 1
            continue

        added += 1
        if args.quiet:
            continue
        clip = video or audio
        assert clip is not None
        parts = [f"{Path(name).name} -> {clip.start:.2f}s +{clip.duration:.2f}s"]
        if video and audio:
            parts.append("(video+audio)")
        elif video:
            parts.append("(video)")
        else:
            parts.append("(audio)")
        if abs(args.speed - 1.0) > 1e-9:
            parts.append(f"@{args.speed:g}x")
        print("  " + " ".join(parts))

    if added:
        path = project.save()
        print(f"saved {path}  total {_human_time(project.timeline.duration)}")

    return 1 if failed and not added else 0


def cmd_info(args: argparse.Namespace) -> int:
    """Show what is in a project."""
    project = Project.load(args.project)

    if args.json:
        print(json.dumps(project.to_dict(), indent=2))
        return 0

    timeline = project.timeline
    print(project.summary())
    if project.description:
        print(f"  {project.description}")
    print(f"  path      {project.path}")
    print(f"  duration  {_human_time(timeline.duration)}")
    print()

    for track in timeline.tracks:
        marker = " (muted)" if track.muted else ""
        print(f"  {track.name} [{track.kind}]{marker}")
        for clip in track.sorted_clips():
            label = clip.name or Path(clip.source).name or clip.source or "gap"
            bits = [f"{clip.start:7.2f}s  +{clip.duration:6.2f}s"]
            if abs(clip.speed - 1.0) > 1e-9:
                bits.append(f"{clip.speed:g}x")
            if clip.in_point > 0:
                bits.append(f"in {clip.in_point:.2f}s")
            if abs(clip.volume - 1.0) > 1e-9:
                bits.append(f"vol {clip.volume:.2f}")
            if clip.effects:
                bits.append(f"{len(clip.effects)} fx")
            if clip.text:
                snippet = clip.text.content.replace("\n", " ")[:28]
                bits.append(f"text {snippet!r}")
            if clip.transition:
                bits.append(f"->{clip.transition.get('type', '?')}")
            if not clip.enabled:
                bits.append("(disabled)")
            print(f"    {'  '.join(bits)}  {label}")
        if not track.clips:
            print("    (empty)")

    missing = project.missing_media()
    if missing:
        print()
        print(f"  missing media ({len(missing)}):")
        for name in missing:
            print(f"    {name}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Check a project for problems the renderer would trip over."""
    project = Project.load(args.project)
    problems = project.validate()
    if not problems:
        print(f"{project.path.name}: no problems found")
        return 0
    print(f"{project.path.name}: {len(problems)} problem(s)", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# capability reporting
# ---------------------------------------------------------------------------

def cmd_presets(args: argparse.Namespace) -> int:
    """List the export presets."""
    if args.json:
        print(json.dumps(presets.catalog(), indent=2))
        return 0
    for preset in presets.PRESETS.values():
        raster = (f"{preset.width}x{preset.height}"
                  if preset.width and preset.height else "project raster")
        if preset.is_audio_only:
            raster = "audio only"
        print(f"  {preset.name:<16} {preset.label}")
        print(f"  {'':<16} {preset.container}, {raster}, crf {preset.crf}"
              f"{f', {preset.fps:g}fps' if preset.fps else ''}")
        if preset.description:
            print(f"  {'':<16} {preset.description}")
        print()
    return 0


def cmd_encoders(args: argparse.Namespace) -> int:
    """Show which encoders this machine can actually use."""
    if not ffmpeg.tool_status()["ok"]:
        return _fail("ffmpeg not found -- see `hatcut doctor`")
    report = presets.encoder.report()
    for family, names in report.items():
        print(f"  {family}:")
        for name in names:
            choice = presets.encoder.EncoderChoice(name, name, True)
            kind = "hardware" if name in presets.encoder.HARDWARE else "software"
            print(f"    {name:<20} {kind}")
        if not names:
            print("    (none available)")
        print()

    chosen = presets.encoder.choose("h264")
    print(f"  h264 renders will use: {chosen.label}")
    print(f"  {ffmpeg.version()}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report whether this machine can run Hatcut, and why not if it cannot."""
    status = ffmpeg.tool_status()
    ok = True

    print(f"  python    {sys.version.split()[0]}  ({sys.platform})")
    print(f"  hatcut    {__version__}")

    for label, path in (("ffmpeg", status["ffmpeg"]), ("ffprobe", status["ffprobe"])):
        if path:
            print(f"  {label:<9} {path}")
        else:
            print(f"  {label:<9} NOT FOUND")
            ok = False

    if not ok:
        print()
        print("  ffmpeg was not found. Hatcut does not bundle it. Either:")
        print("    - put ffmpeg and ffprobe on your PATH, or")
        print("    - set HATCUT_FFMPEG and HATCUT_FFPROBE to their full paths")
        for hint in status["hints"]:
            print(f"    - {hint}")
        return 1

    print(f"  version   {ffmpeg.version()}")
    encoders = presets.encoder.report()
    print(f"  encoders  h264: {', '.join(encoders['h264']) or 'none'}")
    print(f"            hevc: {', '.join(encoders['hevc']) or 'none'}")
    if "libx264" not in encoders["h264"]:
        print("  warning   no libx264 -- software H.264 fallback is unavailable")
    print("  ready")
    return 0


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def cmd_graph(args: argparse.Namespace) -> int:
    """Print the compiled ffmpeg filtergraph without running it.

    This is the debugging tool for the renderer itself: when a render produces
    something odd, the filtergraph is the artifact to read.
    """
    project = Project.load(args.project)
    from .render.pipeline import build_job

    job = build_job(project.timeline, args.preset, args.output,
                    crf=args.crf, prefer_hardware=not args.no_hardware)

    if args.json:
        print(json.dumps(job.to_dict(), indent=2))
        return 0

    print(f"# {job.duration:.3f}s -> {job.output}")
    for warning in job.warnings:
        print(f"# warning: {warning}")
    if args.raw:
        # Verbatim argv, one token per line -- the form you can diff.
        for part in job.command:
            print(part)
        return 0

    print()
    print(job.command_line())
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    """Render a project through a preset."""
    project = Project.load(args.project)

    if project.timeline.is_empty():
        return _fail("this project has no clips to render")

    missing = project.missing_media()
    if missing and not args.force:
        for name in missing[:5]:
            print(f"hatcut: missing media: {name}", file=sys.stderr)
        if len(missing) > 5:
            print(f"hatcut: ... and {len(missing) - 5} more", file=sys.stderr)
        return _fail("refusing to render; pass --force to try anyway")

    output = Path(args.output).expanduser() if args.output else None
    if output is None:
        stem = project.path.stem if project.path else "export"
        output = Path.cwd() / f"{stem}.{presets.get(args.preset).container}"

    if output.exists() and not args.overwrite:
        return _fail(f"{output} already exists; pass --overwrite to replace it")

    from .render.pipeline import build_job, render

    try:
        job = build_job(
            project.timeline, args.preset, output,
            crf=args.crf, width=args.width, height=args.height, fps=args.fps,
            prefer_hardware=not args.no_hardware,
            max_duration=args.duration,
        )
    except HatcutError as exc:
        return _fail(str(exc))

    for warning in job.warnings:
        print(f"hatcut: warning: {warning}", file=sys.stderr)

    if args.dry_run:
        print(job.command_line())
        return 0

    if not args.quiet:
        preset = presets.get(args.preset)
        print(f"rendering {_human_time(job.duration)} of timeline "
              f"-> {job.output.name} ({preset.label})")

    bar = _Bar(enabled=not args.quiet)
    started = time.monotonic()

    def on_progress(done: float, total: float) -> None:
        bar.update(done, total)

    try:
        result = render(job, on_progress=on_progress)
    except HatcutError as exc:
        bar.finish()
        return _fail(str(exc))
    except KeyboardInterrupt:
        bar.finish()
        print("hatcut: interrupted", file=sys.stderr)
        return 130
    finally:
        bar.finish()

    elapsed = time.monotonic() - started
    size = result.stat().st_size if result.exists() else 0
    speed = job.duration / elapsed if elapsed > 0 else 0.0
    if not args.quiet:
        print(f"wrote {result}  ({_human_bytes(size)}, "
              f"{elapsed:.1f}s, {speed:.2f}x realtime)")
    else:
        print(result)
    return 0


# ---------------------------------------------------------------------------
# editor
# ---------------------------------------------------------------------------

def cmd_serve(args: argparse.Namespace) -> int:
    """Run the local web editor against a project."""
    from .server import serve

    try:
        return serve(args.project, host=args.host, port=args.port,
                     open_browser=not args.no_browser,
                     autosave=not args.no_autosave)
    except HatcutError as exc:
        return _fail(str(exc))


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hatcut",
        description="A programmable non-linear video editor built on ffmpeg.",
        epilog="Run `hatcut doctor` first if anything looks broken.",
    )
    parser.add_argument("--version", action="version",
                        version=f"hatcut {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    # -- probe ----------------------------------------------------------
    probe = subparsers.add_parser(
        "probe", help="describe media files",
        description=f"Inspect {PROBE_HINT}. Prints streams, duration and raster.")
    probe.add_argument("files", nargs="+", help="files to inspect")
    probe.add_argument("--json", action="store_true", help="machine-readable output")
    probe.set_defaults(func=cmd_probe)

    # -- new ------------------------------------------------------------
    new = subparsers.add_parser("new", help="create an empty project")
    new.add_argument("project", help=f"path to write (gets {PROJECT_SUFFIX})")
    new.add_argument("--name", help="project title (defaults to the file name)")
    new.add_argument("--width", type=int, default=1920)
    new.add_argument("--height", type=int, default=1080)
    new.add_argument("--fps", type=float, default=30.0)
    new.add_argument("--size", help="shorthand raster, e.g. 1080x1920")
    new.set_defaults(func=cmd_new)

    # -- add ------------------------------------------------------------
    add = subparsers.add_parser(
        "add", help="append media to a project",
        description="Import files, placing each one after the last. Picture and "
                    "sound land on separate tracks and stay linked.")
    add.add_argument("project")
    add.add_argument("files", nargs="+")
    add.add_argument("--start", type=float, default=None,
                     help="timeline position for the first clip (default: append)")
    add.add_argument("--append", action="store_true", default=True,
                     help="place each file after existing content (default)")
    add.add_argument("--at", dest="start", type=float,
                     help="alias for --start")
    add.add_argument("--in", dest="in_point", type=float, default=0.0,
                     metavar="SECONDS", help="source in-point")
    add.add_argument("--duration", type=float, default=None,
                     help="how much to take (default: the whole file)")
    add.add_argument("--speed", type=float, default=1.0, help="playback rate")
    add.add_argument("--no-audio", action="store_true",
                     help="import picture only")
    add.add_argument("--track", help="explicit destination track id")
    add.add_argument("-q", "--quiet", action="store_true")
    add.set_defaults(func=cmd_add)

    # -- info -----------------------------------------------------------
    info = subparsers.add_parser("info", help="show a project's contents")
    info.add_argument("project")
    info.add_argument("--json", action="store_true")
    info.set_defaults(func=cmd_info)

    # -- validate -------------------------------------------------------
    validate = subparsers.add_parser(
        "validate", help="check a project for problems")
    validate.add_argument("project")
    validate.set_defaults(func=cmd_validate)

    # -- presets --------------------------------------------------------
    preset_cmd = subparsers.add_parser("presets", help="list export presets")
    preset_cmd.add_argument("--json", action="store_true")
    preset_cmd.set_defaults(func=cmd_presets)

    # -- encoders -------------------------------------------------------
    encoders = subparsers.add_parser(
        "encoders", help="show usable video encoders")
    encoders.set_defaults(func=cmd_encoders)

    # -- doctor ---------------------------------------------------------
    doctor = subparsers.add_parser(
        "doctor", help="check that ffmpeg is wired up correctly")
    doctor.set_defaults(func=cmd_doctor)

    # -- graph ----------------------------------------------------------
    graph = subparsers.add_parser(
        "graph", help="print the compiled filtergraph",
        description="Compile the timeline and show the ffmpeg command without "
                    "running it.")
    graph.add_argument("project")
    graph.add_argument("-p", "--preset", default="web-1080p")
    graph.add_argument("-o", "--output")
    graph.add_argument("--crf", type=int)
    graph.add_argument("--no-hardware", action="store_true")
    graph.add_argument("--raw", action="store_true",
                       help="one argv token per line, for diffing")
    graph.add_argument("--json", action="store_true")
    graph.set_defaults(func=cmd_graph)

    # -- render ---------------------------------------------------------
    render = subparsers.add_parser(
        "render", help="render a project",
        description="Compile the timeline and run ffmpeg against it.")
    render.add_argument("project")
    render.add_argument("-p", "--preset", default="web-1080p",
                        help="export preset (see `hatcut presets`)")
    render.add_argument("-o", "--output", help="output file")
    render.add_argument("--crf", type=int, help="override quality (lower is better)")
    render.add_argument("--width", type=int)
    render.add_argument("--height", type=int)
    render.add_argument("--fps", type=float)
    render.add_argument("--duration", type=float,
                        help="render only the first N seconds")
    render.add_argument("--no-hardware", action="store_true",
                        help="force the software encoder")
    render.add_argument("--dry-run", action="store_true",
                        help="print the command and exit")
    render.add_argument("--overwrite", action="store_true")
    render.add_argument("--force", action="store_true",
                        help="render even if media is missing")
    render.add_argument("-q", "--quiet", action="store_true")
    render.set_defaults(func=cmd_render)

    # -- serve ----------------------------------------------------------
    serve = subparsers.add_parser(
        "serve", help="open the web editor",
        description="Serve the editor UI against a project on localhost.")
    serve.add_argument("project", nargs="?",
                       help="project to open (created if it does not exist)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8777)
    serve.add_argument("--no-browser", action="store_true",
                       help="do not open a browser window")
    serve.add_argument("--no-autosave", action="store_true",
                       help="keep edits in memory only until you save")
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return 2

    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\nhatcut: interrupted", file=sys.stderr)
        return 130
    except HatcutError as exc:
        return _fail(str(exc))
    except BrokenPipeError:
        # `hatcut presets | head` closing the pipe is not an error.
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
