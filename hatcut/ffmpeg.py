"""Locating and interrogating the ffmpeg toolchain.

Hatcut shells out to ffmpeg rather than linking it. That keeps the project pure
Python and lets people use whatever build they already have -- including the
hardware encoders their particular build ships with.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
from pathlib import Path

from .core.errors import ToolMissingError

#: Environment variables checked before PATH, so a project can pin a specific
#: build without touching the machine's PATH.
FFMPEG_ENV = "HATCUT_FFMPEG"
FFPROBE_ENV = "HATCUT_FFPROBE"

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _run(args: list[str], timeout: float | None = 30.0) -> subprocess.CompletedProcess:
    """Run a subprocess without flashing a console window on Windows."""
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=_CREATE_NO_WINDOW,
    )


def _resolve(env_var: str, binary: str) -> str:
    override = os.environ.get(env_var)
    if override:
        path = Path(override).expanduser()
        if path.is_file():
            return str(path)
        raise ToolMissingError(
            f"{env_var} is set to {override!r} but that is not a file"
        )

    found = shutil.which(binary)
    if found:
        return found

    # Common Windows install locations, checked because ffmpeg is frequently
    # unpacked somewhere that never made it onto PATH.
    if os.name == "nt":
        for root in (r"C:\ffmpeg", r"C:\Program Files\ffmpeg"):
            for candidate in Path(root).rglob(f"{binary}.exe"):
                return str(candidate)

    raise ToolMissingError(
        f"could not find {binary!r}. Install ffmpeg and put it on PATH, or set "
        f"{env_var} to the full path of the binary."
    )


@functools.lru_cache(maxsize=1)
def ffmpeg_path() -> str:
    return _resolve(FFMPEG_ENV, "ffmpeg")


@functools.lru_cache(maxsize=1)
def ffprobe_path() -> str:
    return _resolve(FFPROBE_ENV, "ffprobe")


@functools.lru_cache(maxsize=1)
def version() -> str:
    """First line of `ffmpeg -version`, e.g. 'ffmpeg version N-124401-...'."""
    result = _run([ffmpeg_path(), "-hide_banner", "-version"])
    return result.stdout.splitlines()[0] if result.stdout else "unknown"


@functools.lru_cache(maxsize=1)
def available_encoders() -> frozenset[str]:
    """Names of every encoder this ffmpeg build exposes."""
    result = _run([ffmpeg_path(), "-hide_banner", "-encoders"])
    names: set[str] = set()
    for line in result.stdout.splitlines():
        # Rows look like: " V....D libx264   H.264 / AVC ..."
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            names.add(parts[1])
    return frozenset(names)


@functools.lru_cache(maxsize=1)
def available_filters() -> frozenset[str]:
    """Names of every filter this ffmpeg build exposes."""
    result = _run([ffmpeg_path(), "-hide_banner", "-filters"])
    names: set[str] = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        # Rows look like: " ..C xfade  V->V  Cross fade one video with another."
        if len(parts) >= 2 and len(parts[0]) == 3 and parts[0][0] in "TSC.":
            names.add(parts[1])
    return frozenset(names)


def has_encoder(name: str) -> bool:
    return name in available_encoders()


def has_filter(name: str) -> bool:
    return name in available_filters()


def run_json(args: list[str], timeout: float | None = 60.0) -> dict:
    """Run a command that is expected to emit JSON on stdout."""
    result = _run(args, timeout=timeout)
    if result.returncode != 0:
        raise ToolMissingError(result.stderr.strip() or "command failed")
    return json.loads(result.stdout)


def tool_status() -> dict:
    """Where the toolchain is, without raising.

    ``resolve`` raises on a missing binary, which is the right behaviour for a
    render and the wrong behaviour for a diagnostic. This is the diagnostic:
    it reports both paths and, when something is missing, the places that were
    searched, so ``hatcut doctor`` can tell the user what to fix.
    """
    status: dict = {"ok": True, "ffmpeg": None, "ffprobe": None, "hints": []}

    for key, env_var, binary in (
        ("ffmpeg", FFMPEG_ENV, "ffmpeg"),
        ("ffprobe", FFPROBE_ENV, "ffprobe"),
    ):
        override = os.environ.get(env_var)
        try:
            status[key] = _resolve(env_var, binary)
        except ToolMissingError:
            status["ok"] = False
            if override:
                status["hints"].append(
                    f"{env_var} is set to {override!r}, which is not a file"
                )

    if not status["ok"] and os.name == "nt":
        for root in (r"C:\ffmpeg", r"C:\Program Files\ffmpeg"):
            if not Path(root).exists():
                status["hints"].append(f"no ffmpeg directory at {root}")
            else:
                status["hints"].append(
                    f"{root} exists but no matching binary was found inside it"
                )

    return status


def reset_caches() -> None:
    """Forget cached tool paths and capability probes (used by tests)."""
    ffmpeg_path.cache_clear()
    ffprobe_path.cache_clear()
    version.cache_clear()
    available_encoders.cache_clear()
    available_filters.cache_clear()
