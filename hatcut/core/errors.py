"""Hatcut error types.

Every failure the library raises on purpose derives from HatcutError so callers
can catch one type at the boundary instead of guessing at ffmpeg's exit codes.
"""

from __future__ import annotations


class HatcutError(Exception):
    """Base class for all Hatcut errors."""


class ToolMissingError(HatcutError):
    """ffmpeg or ffprobe could not be located."""


class ProbeError(HatcutError):
    """ffprobe ran but the media could not be read."""


class MediaNotFoundError(HatcutError):
    """A clip references a source file that is not on disk."""


class ProjectError(HatcutError):
    """The project file is malformed, or an operation would corrupt it."""


class TimelineError(HatcutError):
    """An edit is not valid against the current timeline state."""


class RenderError(HatcutError):
    """ffmpeg was invoked and failed."""

    def __init__(self, message: str, command: list[str] | None = None,
                 stderr: str | None = None) -> None:
        super().__init__(message)
        self.command = command or []
        self.stderr = stderr or ""

    def command_line(self) -> str:
        """The ffmpeg invocation, shell-quoted, for pasting into a terminal."""
        import shlex

        return " ".join(shlex.quote(part) for part in self.command)


class RenderCancelled(HatcutError):
    """A render was stopped on purpose.

    Separate from :class:`RenderError` because it is not a failure -- the UI
    needs to tell "you cancelled this" apart from "ffmpeg blew up", and a
    caller that catches RenderError should not have to string-match on the
    message to find out which happened.
    """


class PresetError(HatcutError):
    """An export preset name or override is not valid."""
