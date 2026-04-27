"""showtape — multi-pane in-container demo recorder.

Declarative YAML → narrated MP4. Per step, 1–4 panes (browser via
Playwright, terminal via VHS) composed by FFmpeg, with TTS narration
from Piper.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("showtape")
except PackageNotFoundError:  # not installed (e.g. running from a source tree)
    __version__ = "0+unknown"
