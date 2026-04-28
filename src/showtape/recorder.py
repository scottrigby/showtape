"""Core rendering pipeline: YAML demo spec → narrated multi-pane MP4.

Public surface for callers:
    render(yaml_path, out=None, work_dir=None, voice_model=None) -> Path

Reads a YAML demo spec, generates per-step assets (Piper narration WAV, one
video per pane via Playwright or VHS), composites the panes into a layout
(1–4 panes, with named 3-pane variants), and concatenates the per-step
clips into a single MP4.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path

import yaml
from piper.voice import PiperVoice
from playwright.sync_api import sync_playwright

FPS = 30
DEFAULT_VOICE_MODEL_NAME = "en_US-libritts_r-medium"

# Per-action duration estimates (ms). Used to size the step before running it.
# If a real action takes longer, the recording extends past the estimate; if
# shorter, the pane idles to fill — either way, all panes end at the same time.
BROWSER_ACTION_ESTIMATES = {
    "goto": 3000, "fill": 300, "click": 300, "wait_for": 2000,
    "press": 100, "scroll": 500, "type": 1500,
}

LAYOUTS_3 = ("3-left", "3-right", "3-top", "3-bottom")

# Cookie / localStorage persistence keyed by browser session id.
_session_storage: dict[str, dict] = {}
# Scroll position keyed by browser session id (pixels from top).
_session_scroll: dict[str, int] = {}
# Last navigated URL keyed by browser session id.
_session_url: dict[str, str] = {}

# Named cross-session copy buffers.
_session_buffers: dict[str, str] = {}


# ---------- Voice model resolution ----------

def voice_model_search_paths():
    """Lookup chain for Piper voice models (most specific to most general)."""
    return [
        Path.cwd() / "voices",
        Path("/usr/local/share/showtape/voices"),
        Path.home() / ".cache" / "showtape" / "voices",
    ]


def _find_installed_voice_models() -> list[Path]:
    """Return all .onnx files found across the voice model search paths."""
    found = {}  # name → path, dedup by filename
    for base in voice_model_search_paths():
        if base.is_dir():
            for p in base.glob("*.onnx"):
                found.setdefault(p.name, p)
    return list(found.values())


def resolve_voice_model(voice_model: str | None) -> Path:
    """Resolve a voice model spec to an absolute path.

    Accepts:
      - None         → auto-detect if exactly one model is installed;
                       otherwise fall back to DEFAULT_VOICE_MODEL_NAME
      - bare name    → looked up in search paths
      - relative path → resolved against cwd
      - absolute path → used as-is
    """
    if voice_model is None:
        installed = _find_installed_voice_models()
        if len(installed) == 1:
            return installed[0]
        voice_model = DEFAULT_VOICE_MODEL_NAME
    p = Path(voice_model)
    if p.is_absolute() and p.exists():
        return p
    if p.is_absolute():
        raise FileNotFoundError(f"voice model not found at {p}")
    # Treat as bare name first; fall back to relative path.
    name = p.name if p.suffix == ".onnx" else f"{voice_model}.onnx"
    for base in voice_model_search_paths():
        candidate = base / name
        if candidate.exists():
            return candidate
    # Last resort: maybe it's a relative path the caller meant literally.
    if p.exists():
        return p.resolve()
    searched = "\n  ".join(str(b) for b in voice_model_search_paths())
    installed = _find_installed_voice_models()
    hint = (f"Installed models: {', '.join(p.stem for p in installed)}"
            if installed else
            f"Run `showtape fetch-voice {DEFAULT_VOICE_MODEL_NAME}` to install.")
    raise FileNotFoundError(
        f"voice model {voice_model!r} not found. Searched:\n  {searched}\n{hint}"
    )


# ---------- TTS ----------

def apply_pronunciations(text: str, pronunciations: dict | None) -> str:
    """Substitute words in `text` with per-project respellings before synthesis.

    Match is whole-word (regex \\b) and case-insensitive; the *replacement*
    is taken verbatim from the map. Longer keys win when one is a prefix of
    another (handled by sorting). Empty/None map → text returned unchanged.

    Example:
        apply_pronunciations("Deploy showtape today", {"showtape": "show tape"})
        → "Deploy show tape today"
    """
    if not pronunciations:
        return text
    items = sorted(pronunciations.items(), key=lambda kv: -len(kv[0]))
    lookup = {k.lower(): v for k, v in items}
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(k) for k, _ in items) + r")\b",
        re.IGNORECASE,
    )
    return pattern.sub(lambda m: lookup[m.group(0).lower()], text)


# Trailing silence appended to every synth WAV. Piper writes audio right up
# to the last sample; without padding, concat boundaries occasionally clip
# the tail of the last syllable, especially on plosives. 200 ms is enough
# to be safe and not enough to be perceptible as a gap.
NARRATION_TAIL_PAD_MS = 200


def synth(voice, text, speaker_id, out_path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(voice.config.sample_rate)
        for chunk in voice.synthesize(text):
            wav.writeframes(chunk.audio_int16_bytes)
        # Append silence so the audio's last non-zero sample isn't right
        # at the file boundary.
        silence_samples = int(voice.config.sample_rate * NARRATION_TAIL_PAD_MS / 1000)
        wav.writeframes(b"\x00\x00" * silence_samples)


def wav_duration_ms(path):
    with wave.open(str(path), "rb") as f:
        return int(f.getnframes() * 1000 / f.getframerate())


def silent_wav(path, duration_ms, sample_rate=22050):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"anullsrc=r={sample_rate}:cl=mono",
        "-t", f"{max(0.1, duration_ms / 1000)}",
        str(path),
    ], check=True, capture_output=True)


# ---------- Layout ----------

def pane_dimensions(n, output_w, output_h, layout):
    """Return [(w, h), ...] for each pane index given pane count + layout."""
    if n == 1:
        return [(output_w, output_h)]
    if n == 2:
        return [(output_w // 2, output_h)] * 2
    if n == 3:
        big_horizontal = layout in ("3-top", "3-bottom")
        big = (output_w, output_h // 2) if big_horizontal else (output_w // 2, output_h)
        small = (output_w // 2, output_h // 2)
        return [big, small, small]
    if n == 4:
        return [(output_w // 2, output_h // 2)] * 4
    raise ValueError(f"unsupported pane count: {n}")


def filter_graph(n, layout, fps, w, h):
    """FFmpeg filter graph stitching N pane videos into a single [v] stream.

    Pane videos are recorded at their final dimensions (see pane_dimensions),
    so we only need to normalise fps + sample aspect ratio, not rescale.
    A 1px black separator is drawn between every pair of adjacent panes —
    cheap, unobtrusive, and prevents two same-coloured panes from bleeding
    into one another.
    """
    norm = lambda i, label: f"[{i}:v]fps={fps},setsar=1[{label}]"
    vline = lambda x: f"drawbox=x={x}:y=0:w=1:h={h}:color=black:t=fill"
    hline = lambda y: f"drawbox=x=0:y={y}:w={w}:h=1:color=black:t=fill"
    vline_seg = lambda x, y0, y1: f"drawbox=x={x}:y={y0}:w=1:h={y1 - y0}:color=black:t=fill"
    hline_seg = lambda y, x0, x1: f"drawbox=x={x0}:y={y}:w={x1 - x0}:h=1:color=black:t=fill"
    mid_x, mid_y = w // 2, h // 2

    if n == 1:
        return norm(0, "v")

    if n == 2:
        stack = ";".join([norm(0, "L"), norm(1, "R"), "[L][R]hstack=inputs=2[stacked]"])
        seps = [vline(mid_x)]
    elif n == 3:
        norms = ";".join([norm(0, "A"), norm(1, "B"), norm(2, "C")])
        if layout == "3-left":
            stack = f"{norms};[B][C]vstack=inputs=2[BC];[A][BC]hstack=inputs=2[stacked]"
            seps = [vline(mid_x), hline_seg(mid_y, mid_x, w)]
        elif layout == "3-right":
            stack = f"{norms};[B][C]vstack=inputs=2[BC];[BC][A]hstack=inputs=2[stacked]"
            seps = [vline(mid_x), hline_seg(mid_y, 0, mid_x)]
        elif layout == "3-top":
            stack = f"{norms};[B][C]hstack=inputs=2[BC];[A][BC]vstack=inputs=2[stacked]"
            seps = [hline(mid_y), vline_seg(mid_x, mid_y, h)]
        elif layout == "3-bottom":
            stack = f"{norms};[B][C]hstack=inputs=2[BC];[BC][A]vstack=inputs=2[stacked]"
            seps = [hline(mid_y), vline_seg(mid_x, 0, mid_y)]
        else:
            raise ValueError(f"unknown 3-pane layout: {layout}")
    elif n == 4:
        stack = ";".join([
            norm(0, "TL"), norm(1, "TR"), norm(2, "BL"), norm(3, "BR"),
            "[TL][TR]hstack=inputs=2[T]",
            "[BL][BR]hstack=inputs=2[B]",
            "[T][B]vstack=inputs=2[stacked]",
        ])
        seps = [vline(mid_x), hline(mid_y)]
    else:
        raise ValueError(f"unsupported pane count: {n}")

    return f"{stack};[stacked]{','.join(seps)}[v]"


# ---------- Browser pane ----------

def estimate_browser_ms(actions):
    total = sum(BROWSER_ACTION_ESTIMATES.get(next(iter(a)), 300)
                for a in (actions or []) if isinstance(a, dict))
    return total + 500


def run_browser_action(page, action):
    if not isinstance(action, dict) or len(action) != 1:
        raise ValueError(f"browser action must be a single-key mapping: {action!r}")
    key, val = next(iter(action.items()))
    if key == "goto":
        page.goto(val, wait_until="domcontentloaded", timeout=20000)
    elif key == "capture":
        # Extract text from a DOM element or JS expression into a named buffer.
        # val: { selector: "css", to: "name" }  — innerText of matching element
        #   OR { eval: "js expr", to: "name" }  — result of page.evaluate()
        buf = val["to"]
        if "eval" in val:
            _session_buffers[buf] = str(page.evaluate(val["eval"]) or "").strip()
        else:
            el = page.query_selector(val["selector"])
            _session_buffers[buf] = el.inner_text().strip() if el else ""
    elif key == "fill":
        selector = val["selector"]
        # Support paste_from: to fill from a cross-pane buffer.
        value = _session_buffers.get(val["paste_from"]) if "paste_from" in val else val["value"]
        page.fill(selector, value or "", timeout=10000)
    elif key == "click":
        sel = val if isinstance(val, str) else val["selector"]
        page.click(sel, timeout=10000)
    elif key == "wait_for":
        if isinstance(val, str):
            page.wait_for_selector(val, timeout=20000)
        elif "selector" in val:
            page.wait_for_selector(val["selector"], timeout=val.get("timeout_ms", 20000))
        elif "ms" in val:
            page.wait_for_timeout(val["ms"])
    elif key == "press":
        if isinstance(val, str):
            page.keyboard.press(val)
        else:
            page.press(val["selector"], val["key"])
    elif key == "scroll":
        page.evaluate(f"window.scrollBy({val.get('x', 0)}, {val.get('y', 0)})")
    elif key == "type":
        if isinstance(val, str):
            page.keyboard.type(val, delay=50)
        else:
            page.type(val["selector"], val["value"], delay=val.get("delay_ms", 50))
    else:
        raise ValueError(f"unknown browser action: {key}")


def _run_browser_session(pane, dims, target_ms=None, record=True, video_dir=None):
    """Shared browser session logic for both recording and advance-only modes.

    When record=True: records to video_dir and returns the WebM path.
    When record=False: runs actions only (for `record: false` steps) and returns None.
    Persists cookies, localStorage, scroll position, and last URL across calls
    for the same session id.
    """
    session = pane.get("session", "default")
    actions = pane.get("actions", [])
    storage = _session_storage.get(session)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx_kwargs = dict(viewport={"width": dims[0], "height": dims[1]})
        if record:
            ctx_kwargs["record_video_dir"] = str(video_dir)
            ctx_kwargs["record_video_size"] = {"width": dims[0], "height": dims[1]}
        if storage is not None:
            ctx_kwargs["storage_state"] = storage
        ctx = browser.new_context(**ctx_kwargs)
        page = ctx.new_page()

        # Restore scroll position from previous step in this session.
        # Happens after the first goto fires (page must exist first).
        scroll_restored = False

        def maybe_restore_scroll():
            nonlocal scroll_restored
            if not scroll_restored and session in _session_scroll:
                try:
                    page.evaluate(f"window.scrollTo(0, {_session_scroll[session]})")
                except Exception:
                    pass
                scroll_restored = True

        start = time.monotonic()
        try:
            for action in actions:
                run_browser_action(page, action)
                # Restore scroll after the first goto so it applies to the loaded page.
                if "goto" in action:
                    maybe_restore_scroll()
        except Exception as e:
            print(f"  ! browser action error: {e}", file=sys.stderr)

        if record and target_ms is not None:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            remaining = target_ms - elapsed_ms
            if remaining > 0:
                page.wait_for_timeout(remaining)

        # Save scroll position and storage state for next step.
        try:
            _session_scroll[session] = page.evaluate("window.scrollY") or 0
        except Exception:
            pass
        try:
            _session_storage[session] = ctx.storage_state()
        except Exception as e:
            print(f"  ! could not save session storage: {e}", file=sys.stderr)
        ctx.close()
        browser.close()

    if not record:
        return None

    webms = sorted(video_dir.glob("*.webm"))
    if not webms:
        raise RuntimeError(f"playwright produced no webm in {video_dir}")
    return webms[-1]


def record_browser_pane(pane, target_ms, dims, video_dir, warmup_ms=0):
    video_dir.mkdir(parents=True, exist_ok=True)
    record_ms = target_ms + warmup_ms
    webm = _run_browser_session(pane, dims, target_ms=record_ms, record=True, video_dir=video_dir)
    if warmup_ms <= 0:
        return webm
    # Trim the leading warmup frames (page-load white canvas) from the WebM.
    trimmed = video_dir / "trimmed.webm"
    subprocess.run([
        "ffmpeg", "-y", "-ss", f"{warmup_ms / 1000}",
        "-i", str(webm), "-c", "copy", str(trimmed),
    ], check=True, capture_output=True)
    return trimmed


def advance_browser_pane(pane, dims):
    """Run browser pane actions without recording — advances session state only."""
    _run_browser_session(pane, dims, record=False)


# ---------- Terminal pane ----------

# `Set TypingSpeed` in VHS is global within a tape, so when `paste:` flips it
# to instant and back, the "back" line must reference the same default that
# the tape header writes. One constant, two referencers — keeps them honest.
DEFAULT_TYPING_SPEED_MS = 50

# Per-paste-chunk overhead estimate. A paste chunk emits Type (instant) +
# Enter (~100ms) + a 300ms breath before the next chunk. Doesn't include
# the actual command's runtime — that's what `sleep_ms:` after the paste
# is for.
PASTE_CHUNK_MS = 400


def _paste_chunks(text: str) -> list[str]:
    """Split a `paste:` value into one shell command per chunk.

    YAML `|` block style preserves newlines and usually leaves a trailing
    `\\n`; rstrip drops it. Empty lines (from blank lines in the source
    YAML) are filtered out so we don't inject phantom Enters.

    Backslash-newline continuations stay intact: each chunk ends with the
    literal `\\` and the next chunk begins where the source line did.
    Bash reassembles them as one command when typed in sequence.
    """
    return [c for c in text.rstrip().split("\n") if c.strip()]


def estimate_terminal_ms(actions):
    total = 0
    for a in actions or []:
        if not isinstance(a, dict):
            continue
        if "type" in a:
            total += len(a["type"]) * DEFAULT_TYPING_SPEED_MS
        if "paste" in a:
            total += len(_paste_chunks(a["paste"])) * PASTE_CHUNK_MS
        if a.get("enter"):
            total += 100
        if "sleep_ms" in a:
            total += int(a["sleep_ms"])
        if "capture" in a:
            total += 100   # near-instant tmux capture-pane call
        if "paste_from" in a:
            total += PASTE_CHUNK_MS  # near-instant like paste:
    return total + 500


# Common Unicode characters that the shell readline mis-interprets when sent
# as multi-byte UTF-8. We auto-replace these in `type:`/`paste:` action
# strings so demo authors don't have to remember the rule. (AI-written
# content especially loves smart quotes and em dashes.) Narration text is
# unaffected — that goes through Piper, not the shell.
_UNICODE_TO_ASCII = {
    "—": "-",      # — em dash → hyphen-minus
    "–": "-",      # – en dash → hyphen-minus
    "‘": "'",      # ' left single → straight apostrophe
    "’": "'",      # ' right single → straight apostrophe
    "“": '"',      # " left double → straight double quote
    "”": '"',      # " right double → straight double quote
    "…": "...",    # … ellipsis → three dots
    " ": " ",      # non-breaking space → regular space
    "­": "",       # soft hyphen → strip
    "•": "*",      # • bullet → asterisk
}


def _to_shell_safe_ascii(s: str) -> str:
    """Convert known-problematic Unicode characters to ASCII equivalents.

    Sent through the type:/paste: → VHS → ttyd → bash readline pipeline,
    multi-byte UTF-8 sequences sometimes get partially interpreted as
    command-line edit operations (transposing words, killing the line).
    Pre-substituting the common offenders makes demos copy-pastable from
    AI-generated content without a foot-gun.
    """
    for src, dst in _UNICODE_TO_ASCII.items():
        s = s.replace(src, dst)
    return s


def _emit_terminal_actions(actions):
    """Translate a sequence of YAML terminal actions into VHS tape lines.

    Returns (lines, used_ms). Doesn't include tape header (Output, Set Width,
    Set TypingSpeed, etc.) or trailing Sleep padding — caller wraps it. This
    is the reusable bit that's shared between per-step tapes (compile_tape)
    and non-session per-step tapes (compile_tape).
    """
    lines = []
    used_ms = 0
    for a in actions or []:
        if "type" in a:
            text = _to_shell_safe_ascii(a["type"])
            lines.append(vhs_type_line(text))
            used_ms += len(text) * DEFAULT_TYPING_SPEED_MS
        if "paste" in a:
            text = _to_shell_safe_ascii(a["paste"])
            chunks = _paste_chunks(text)
            lines.append("Set TypingSpeed 1ms")
            for k, chunk in enumerate(chunks):
                lines.append(vhs_type_line(chunk))
                lines.append("Enter")
                if k < len(chunks) - 1:
                    lines.append("Sleep 300ms")
            lines.append(f"Set TypingSpeed {DEFAULT_TYPING_SPEED_MS}ms")
            used_ms += len(chunks) * PASTE_CHUNK_MS
        if a.get("enter"):
            lines.append("Enter")
            used_ms += 100
        if "sleep_ms" in a:
            lines.append(f'Sleep {int(a["sleep_ms"])}ms')
            used_ms += int(a["sleep_ms"])
    return lines, used_ms


def vhs_type_line(s: str) -> str:
    """Render a `Type ...` line for the given string.

    VHS's Type takes a quoted string but doesn't accept backslash escapes
    — to include a `"` you have to wrap the whole string in single quotes
    (and vice versa). Strings that contain BOTH quote styles can't be
    Typed in a single call; raise so the user gets a clear error rather
    than a cryptic VHS parser failure.
    """
    has_double = '"' in s
    has_single = "'" in s
    if has_double and has_single:
        raise ValueError(
            f"VHS Type can't render a string containing both ' and \". "
            f"Either rephrase or split into separate type/paste actions: {s!r}"
        )
    if has_double:
        return f"Type '{s}'"
    return f'Type "{s}"'


def _tape_header(output_mp4, dims, font_size):
    return [
        f'Output "{output_mp4}"',
        f"Set Width {dims[0]}",
        f"Set Height {dims[1]}",
        f"Set FontSize {font_size}",
        f"Set TypingSpeed {DEFAULT_TYPING_SPEED_MS}ms",
        'Set Theme "Dracula"',
        "Set Padding 30",
        "Set Shell bash",
    ]


def compile_tape(actions, target_ms, output_mp4, dims, font_size):
    """Tape for a single per-step terminal pane (no session continuity)."""
    lines = _tape_header(output_mp4, dims, font_size)
    action_lines, used_ms = _emit_terminal_actions(actions)
    lines.extend(action_lines)
    remaining = target_ms - used_ms
    if remaining > 0:
        lines.append(f"Sleep {remaining}ms")
    return "\n".join(lines) + "\n"


def record_terminal_pane(pane, target_ms, dims, work_dir, key, font_size):
    work_dir.mkdir(parents=True, exist_ok=True)
    tape_path = work_dir / f"{key}.tape"
    out_path = (work_dir / f"{key}.mp4").resolve()
    tape_path.write_text(compile_tape(pane.get("actions", []), target_ms, out_path, dims, font_size))
    subprocess.run(["vhs", str(tape_path)], check=True)
    return out_path


# ---------- Terminal sessions (continuity across steps) ----------
#
# A terminal pane with `session: <id>` shares one shell (a tmux server-side
# session) across every step that uses the same id. Scrollback persists
# between steps because the tmux session stays alive for the entire render.
#
# Recording is per-step: each step that includes a session pane attaches a
# fresh VHS client to the live tmux session, records exactly that step's
# duration, and exits. No batch recording, no time-based slicing, no scale
# factors — each step's MP4 is exactly what happened during that step.
# Commands run once (safe for write-ops); different steps can use different
# viewport dims for the same session.
#
# Default font size for all terminal panes (plain and session).
# Override per-demo with the top-level `terminal_font_size:` YAML field.
DEFAULT_TERMINAL_FONT_SIZE = 18

# Monospace char width / line-height ratios for font → grid-size math.
# Calibrated for JetBrains Mono (VHS default); ≤5% error at typical sizes.
_CHAR_WIDTH_RATIO = 0.60
_LINE_HEIGHT_RATIO = 1.25

# VHS settle time: how long to hide the tmux attach phase before Show.
_SESSION_SETTLE_MS = 1000
# How long after spawning VHS the orchestrator waits before driving actions.
# Must be > _SESSION_SETTLE_MS / 1000 so that Show fires before the first
# keystroke and every action lands in the recording.
_SESSION_PREROLL_WAIT_S = 1.5
# Extra Sleep after the step's target duration in the VHS tape. Acts as a
# safety buffer so VHS doesn't exit before the last action's frames land.
_SESSION_STEP_BUFFER_MS = 1000


def _collect_terminal_sessions(step_plans):
    """Return {session_id: [(step_idx, pane_idx, pane, dims, target_ms), ...]}.

    Only includes panes that explicitly declare `session:`. Per-step terminals
    without `session:` keep the original render_terminal_pane path.
    """
    sessions = {}
    for plan in step_plans:
        for j, (pane, dims) in enumerate(zip(plan["panes"], plan["dims_list"])):
            if pane.get("type") == "terminal" and "session" in pane:
                sid = pane["session"]
                sessions.setdefault(sid, []).append(
                    (plan["idx"], j, pane, dims, plan["step_ms"])
                )
    return sessions


def _unique_session_dims(occurrences):
    """Distinct (w, h) dims across this session's occurrences, preserving order."""
    seen = []
    for occ in occurrences:
        if occ[3] not in seen:
            seen.append(occ[3])
    return seen


def _compute_session_geometry(unique_dims, font_size, padding=30):
    """Return (cols, rows) for the initial tmux session size.

    Uses the smallest dim so the session starts at a safe minimum. With
    window-size latest, tmux auto-resizes to each attaching client's
    natural grid, so every step's commands run at the correct column count
    for that step's viewport — no manual resize needed.
    """
    min_cols, min_rows = 10000, 10000
    for (w, h) in unique_dims:
        inner_w = max(1, w - 2 * padding)
        inner_h = max(1, h - 2 * padding)
        cols = max(40, int(inner_w / (font_size * _CHAR_WIDTH_RATIO)))
        rows = max(8, int(inner_h / (font_size * _LINE_HEIGHT_RATIO)))
        min_cols = min(min_cols, cols)
        min_rows = min(min_rows, rows)
    return min_cols, min_rows


def _wait_for_tmux_clients(tmux_sid, n, timeout_s=20.0):
    """Block until at least n clients are attached to the tmux session."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        result = subprocess.run(
            ["tmux", "list-clients", "-t", tmux_sid],
            capture_output=True, text=True,
        )
        count = len([ln for ln in result.stdout.strip().splitlines() if ln.strip()])
        if count >= n:
            return
        time.sleep(0.2)
    raise TimeoutError(
        f"Timed out after {timeout_s}s waiting for {n} tmux clients on '{tmux_sid}'"
    )


def _capture_last_output(tmux_sid, buffer_name):
    """Capture the last command's output from a tmux pane into _session_buffers.

    Works with any bash prompt style. Bash prompts always contain '$ ' as a
    separator between the prompt prefix and the command (e.g. 'user@host:~$ cmd'
    or '$ cmd'). Scans backward for the current empty prompt (line ending with
    '$' or '$ '), then the previous prompt+command line (line containing '$ '
    with text after it). Everything between them is the command's output.

    A `sleep_ms:` action before `capture:` is required so the command has time
    to complete before capture-pane runs.
    """
    result = subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", tmux_sid],
        capture_output=True, text=True, check=True,
    )
    lines = result.stdout.split("\n")
    stripped = [l.rstrip() for l in lines]

    # Find the last empty prompt: line ending with "$" or "$ " (nothing after)
    last_prompt = None
    for i in range(len(stripped) - 1, -1, -1):
        if re.search(r'\$\s*$', stripped[i]):
            last_prompt = i
            break

    # Find the previous prompt+command: line containing "$ " followed by text
    prev_prompt = None
    if last_prompt is not None:
        for i in range(last_prompt - 1, -1, -1):
            m = re.search(r'\$ (.+)', stripped[i])
            if m and m.group(1).strip():
                prev_prompt = i
                break

    if prev_prompt is not None and last_prompt is not None:
        output = "\n".join(
            l for l in stripped[prev_prompt + 1:last_prompt] if l.strip()
        )
        _session_buffers[buffer_name] = output
    else:
        _session_buffers[buffer_name] = ""


def _drive_actions_via_tmux(tmux_sid, actions):
    """Drive terminal actions into a live tmux session via send-keys.

    Mirrors _emit_terminal_actions semantics (same action types, same timing)
    but sends to a live shell instead of emitting tape lines.

    Extra actions beyond _emit_terminal_actions:
      capture: <name>     — snapshot last command's output to a named buffer
      paste_from: <name>  — type a previously captured buffer char-by-char
    """
    for a in actions or []:
        if "type" in a:
            text = _to_shell_safe_ascii(a["type"])
            for ch in text:
                subprocess.run(
                    ["tmux", "send-keys", "-t", tmux_sid, "-l", ch],
                    check=True, capture_output=True,
                )
                time.sleep(DEFAULT_TYPING_SPEED_MS / 1000)
        if "paste" in a:
            text = _to_shell_safe_ascii(a["paste"])
            chunks = _paste_chunks(text)
            for k, chunk in enumerate(chunks):
                subprocess.run(
                    ["tmux", "send-keys", "-t", tmux_sid, "-l", chunk],
                    check=True, capture_output=True,
                )
                subprocess.run(
                    ["tmux", "send-keys", "-t", tmux_sid, "Enter"],
                    check=True, capture_output=True,
                )
                if k < len(chunks) - 1:
                    time.sleep(0.3)
        if a.get("enter"):
            subprocess.run(
                ["tmux", "send-keys", "-t", tmux_sid, "Enter"],
                check=True, capture_output=True,
            )
            time.sleep(0.1)
        if "sleep_ms" in a:
            time.sleep(int(a["sleep_ms"]) / 1000)
        if "capture" in a:
            _capture_last_output(tmux_sid, a["capture"])
        if "paste_from" in a:
            content = _session_buffers.get(a["paste_from"], "")
            if not content:
                raise ValueError(
                    f"paste_from: buffer {a['paste_from']!r} is empty — "
                    "ensure a `capture:` action ran before this step"
                )
            # Near-instant, like paste: — no per-character delay.
            subprocess.run(
                ["tmux", "send-keys", "-t", tmux_sid, "-l",
                 _to_shell_safe_ascii(content)],
                check=True, capture_output=True,
            )


def _setup_sessions(step_plans, font_size):
    """Start one tmux session per unique session: <id> found in step_plans.

    Returns {sid: tmux_sid}.
    Call _teardown_sessions() when rendering is complete (or on error).
    """
    sessions = _collect_terminal_sessions(step_plans)
    result = {}
    for sid, occurrences in sessions.items():
        unique_dims = _unique_session_dims(occurrences)
        cols, rows = _compute_session_geometry(unique_dims, font_size)
        tmux_sid = f"st_{sid}"
        # LC_ALL=C avoids "cannot change locale" warnings from bash startup.
        # /bin/bash keeps the demo shell predictable — no zsh/oh-my-zsh prompt
        # redraws that would cause visual noise in recordings.
        bash = shutil.which("bash") or "/usr/bin/bash" or "/bin/bash"
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", tmux_sid,
             "-x", str(cols), "-y", str(rows), "-e", "LC_ALL=C", bash],
            check=True,
        )
        # window-size latest: tmux auto-resizes the window to each attaching
        # client's natural grid. Every step's commands run at the correct
        # column count for that step's viewport. No dots (window always
        # matches the client), no manual resize needed per step.
        subprocess.run(
            ["tmux", "set-option", "-t", tmux_sid, "window-size", "latest"],
            check=True, capture_output=True,
        )
        result[sid] = tmux_sid
        dim_str = ", ".join(f"{w}x{h}" for (w, h) in unique_dims)
        print(f"  session '{sid}' → tmux '{tmux_sid}' {cols}x{rows} font={font_size}px ({dim_str})")
    return result


def advance_terminal_session_pane(pane, tmux_sid):
    """Drive session terminal actions without recording — advances shell state only."""
    _drive_actions_via_tmux(tmux_sid, pane.get("actions", []))


def _teardown_sessions(session_map):
    """Kill all tmux sessions started by _setup_sessions."""
    for _sid, tmux_sid in session_map.items():
        subprocess.run(["tmux", "kill-session", "-t", tmux_sid], capture_output=True)


def record_terminal_session_pane(pane, target_ms, dims, work_dir, key, tmux_sid, font_size):
    """Record one step of a session terminal pane.

    Attaches a fresh VHS client to the live tmux session, drives this step's
    actions via send-keys while VHS records, then exits. The output MP4
    contains exactly this step's content — no slicing required.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    out_path = (work_dir / f"{key}.mp4").resolve()
    tape_path = work_dir / f"{key}.tape"
    w, h = dims
    tape = "\n".join([
        f'Output "{out_path}"',
        f"Set Width {w}",
        f"Set Height {h}",
        f"Set FontSize {font_size}",
        "Set TypingSpeed 1ms",
        'Set Theme "Dracula"',
        "Set Padding 30",
        "Hide",
        f'Type "tmux attach -t {tmux_sid}"',
        "Enter",
        f"Sleep {_SESSION_SETTLE_MS}ms",
        "Show",
        f"Sleep {target_ms + _SESSION_STEP_BUFFER_MS}ms",
    ]) + "\n"
    tape_path.write_text(tape)

    proc = subprocess.Popen(["vhs", str(tape_path)])
    _wait_for_tmux_clients(tmux_sid, 1)
    # Wait until Show fires (settle sleep completes) before driving actions.
    time.sleep(_SESSION_PREROLL_WAIT_S)
    _drive_actions_via_tmux(tmux_sid, pane.get("actions", []))
    proc.wait()
    # Brief pause so the tmux client fully detaches before the next step's
    # VHS client attaches to the same session.
    time.sleep(0.3)
    return out_path


# ---------- Composite ----------

def composite_step(pane_videos, audio_wav, output_mp4, total_ms, n_panes, layout, w, h):
    output_mp4.parent.mkdir(parents=True, exist_ok=True)
    total_s = total_ms / 1000
    fc = filter_graph(n_panes, layout, FPS, w, h) + f";[{n_panes}:a]apad=whole_dur={total_s}[a]"
    cmd = ["ffmpeg", "-y"]
    for v in pane_videos:
        cmd.extend(["-i", str(v)])
    cmd.extend(["-i", str(audio_wav)])
    cmd.extend([
        "-filter_complex", fc,
        "-map", "[v]", "-map", "[a]",
        "-t", f"{total_s}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        str(output_mp4),
    ])
    subprocess.run(cmd, check=True)


def concat_clips(clip_paths, out_path, work_dir):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    list_file = work_dir / "concat.txt"
    list_file.write_text("\n".join(f"file '{p.resolve()}'" for p in clip_paths) + "\n")
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_file), "-c", "copy", str(out_path),
    ], check=True)


# ---------- Public API ----------

def render(yaml_path, out=None, work_dir=None, voice_model=None, keep_work=False):
    """Render a demo YAML to MP4. Paths default to cwd-relative.

    Returns the absolute path of the produced MP4.
    """
    yaml_path = Path(yaml_path).resolve()
    spec = yaml.safe_load(yaml_path.read_text())
    res = spec.get("resolution", {"w": 1920, "h": 1080})
    output_w, output_h = res["w"], res["h"]
    default_voice = int(spec.get("speaker", 0))
    spec_voice_model = spec.get("voice_model")       # overrides --voice-model if set
    pronunciations = spec.get("pronunciations") or {}
    font_size = int(spec.get("terminal_font_size", DEFAULT_TERMINAL_FONT_SIZE))

    out_path = Path(out or Path.cwd() / "out" / f"{yaml_path.stem}.mp4").resolve()
    work = Path(work_dir or Path.cwd() / ".showtape-work").resolve()
    if work.exists() and not keep_work:
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)

    # CLI --voice-model takes precedence; YAML voice_model: is the fallback.
    voice_path = resolve_voice_model(voice_model or spec_voice_model)
    print(f"Loading Piper voice from {voice_path}...")
    voice = PiperVoice.load(str(voice_path))
    # If the model is single-speaker, speaker 0 is the only option and
    # specifying `speaker:` in the YAML is optional. For multi-speaker models
    # (e.g. en_US-libritts_r-medium with 904 speakers), default_voice selects
    # which speaker to use; 0 is valid but the user may want to pick another.
    num_speakers = getattr(voice.config, "num_speakers", 1) or 1
    if default_voice >= num_speakers:
        raise ValueError(
            f"speaker: {default_voice} is out of range for {voice_path.stem} "
            f"({num_speakers} speaker{'s' if num_speakers != 1 else ''}; valid: 0–{num_speakers - 1})"
        )

    # Reset per-render state.
    _session_storage.clear()
    _session_scroll.clear()
    _session_url.clear()
    _session_buffers.clear()

    # ---- Pass 1: plan every step (synth narration, compute durations, layout) ----
    # Splitting this off lets us know all step durations BEFORE we render any
    # cross-step terminal sessions, which need the per-step durations to
    # build their tapes.
    print("\n=== Planning steps (synth + duration estimates) ===")
    step_plans = []
    for i, step in enumerate(spec["steps"]):
        sid = step.get("id", f"step{i}")
        record = step.get("record", True)
        narration = step.get("narration", "") if record else ""
        end_buffer_ms = int(step.get("end_buffer_ms", step.get("pause_ms", 0)))
        browser_warmup_ms = int(step.get("browser_warmup_ms", 0))
        panes = step.get("panes")
        if not panes or not (1 <= len(panes) <= 4):
            raise ValueError(f"step {sid}: must have 1-4 panes, got {len(panes) if panes else 0}")
        n = len(panes)
        layout = step.get("layout", "3-left" if n == 3 else None)
        if n == 3 and layout not in LAYOUTS_3:
            raise ValueError(f"step {sid}: 3-pane layout must be one of {LAYOUTS_3}, got {layout!r}")

        narration_wav = work / "audio" / f"{i}.wav"
        if narration:
            spoken = apply_pronunciations(narration, pronunciations)
            synth(voice, spoken, default_voice, narration_wav)
            narration_ms = wav_duration_ms(narration_wav)
        else:
            narration_ms = 0

        estimates = []
        for pane in panes:
            t = pane.get("type")
            if t == "browser":
                estimates.append(estimate_browser_ms(pane.get("actions", [])))
            elif t == "terminal":
                estimates.append(estimate_terminal_ms(pane.get("actions", [])))
            else:
                raise ValueError(f"step {sid}: unknown pane type {t!r}")
        step_ms = max([narration_ms, *estimates]) + end_buffer_ms

        if not narration:
            silent_wav(narration_wav, step_ms)

        dims_list = pane_dimensions(n, output_w, output_h, layout)
        plan = {
            "idx": i, "step": step, "sid": sid, "panes": panes, "n": n,
            "layout": layout, "step_ms": step_ms, "narration_ms": narration_ms,
            "narration_wav": narration_wav, "dims_list": dims_list,
            "record": record,
            "browser_warmup_ms": browser_warmup_ms,
        }
        step_plans.append(plan)
        layout_str = f", {layout}" if layout else ""
        rec_flag = "" if record else " [record: false]"
        print(f"  [{i}] {sid} ({n} pane{'s' if n > 1 else ''}{layout_str}){rec_flag} "
              f"narration={narration_ms}ms est={estimates} end_buffer={end_buffer_ms}ms → step={step_ms}ms")

    # ---- Pass 2: start tmux sessions for all session: <id> panes ----
    print("\n=== Starting tmux sessions ===")
    session_map = _setup_sessions(step_plans, font_size)

    # ---- Pass 3: record panes and composite, step by step ----
    # Each step's panes are recorded in sequence; all pane recordings complete
    # before the step is composited and we move to the next step.
    clip_paths = []
    try:
        for plan in step_plans:
            i, sid, n, layout = plan["idx"], plan["sid"], plan["n"], plan["layout"]
            step_ms, panes, dims_list = plan["step_ms"], plan["panes"], plan["dims_list"]
            narration_wav = plan["narration_wav"]
            record = plan["record"]
            browser_warmup_ms = plan["browser_warmup_ms"]
            layout_str = f", {layout}" if layout else ""
            rec_flag = "" if record else " [record: false]"
            print(f"\n=== [{i}] {sid} ({n} pane{'s' if n > 1 else ''}{layout_str}){rec_flag} ===")

            if not record:
                # Advance state without recording — runs actions, updates sessions,
                # but produces no clip. Useful for write-ops (helm upgrade, kubectl
                # apply) or waits (kubectl wait) that shouldn't appear in the output.
                for j, (pane, dims) in enumerate(zip(panes, dims_list)):
                    t = pane["type"]
                    if t == "browser":
                        advance_browser_pane(pane, dims)
                    elif t == "terminal" and "session" in pane:
                        advance_terminal_session_pane(pane, session_map[pane["session"]])
                    # plain terminals: no persistent state, nothing to advance
                continue

            pane_videos = []
            for j, (pane, dims) in enumerate(zip(panes, dims_list)):
                t = pane["type"]
                if t == "browser":
                    v = record_browser_pane(pane, step_ms, dims,
                                            work / "panes" / f"{i}-{j}-browser",
                                            warmup_ms=browser_warmup_ms)
                    sess_label = f" session={pane.get('session', 'default')}"
                elif t == "terminal" and "session" in pane:
                    sid_term = pane["session"]
                    tmux_sid = session_map[sid_term]
                    v = record_terminal_session_pane(
                        pane, step_ms, dims,
                        work / "panes", f"{i}-{j}-sess",
                        tmux_sid, font_size,
                    )
                    sess_label = f" session={sid_term} @ {dims[0]}x{dims[1]}"
                else:
                    v = record_terminal_pane(pane, step_ms, dims,
                                             work / "panes", f"{i}-{j}-term", font_size)
                    sess_label = ""
                pane_videos.append(v)
                print(f"  pane[{j}] {t}{sess_label} {dims} → {v.name}")

            clip_path = work / "clips" / f"{i}.mp4"
            composite_step(pane_videos, narration_wav, clip_path, step_ms, n, layout,
                           output_w, output_h)
            clip_paths.append(clip_path)
    finally:
        _teardown_sessions(session_map)

    print(f"\n=== Concatenating {len(clip_paths)} clips → {out_path} ===")
    concat_clips(clip_paths, out_path, work)

    probe = subprocess.run(
        ["ffprobe", "-v", "error",
         "-show_entries", "format=duration:stream=codec_name,width,height",
         "-of", "default=nw=1", str(out_path)],
        capture_output=True, text=True,
    )
    print(probe.stdout)
    print(f"✅ {out_path}")
    return out_path
