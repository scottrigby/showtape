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

# Cookie / localStorage persistence keyed by browser session id. Survives
# across steps within one render call; reset each call.
_session_storage: dict[str, dict] = {}


# ---------- Voice model resolution ----------

def voice_model_search_paths():
    """Lookup chain for Piper voice models (most specific to most general)."""
    return [
        Path.cwd() / "voices",
        Path("/usr/local/share/showtape/voices"),
        Path.home() / ".cache" / "showtape" / "voices",
    ]


def resolve_voice_model(voice_model: str | None) -> Path:
    """Resolve a voice model spec to an absolute path.

    Accepts:
      - None         → DEFAULT_VOICE_MODEL_NAME, looked up in search paths
      - bare name    → looked up in search paths
      - relative path → resolved against cwd
      - absolute path → used as-is
    """
    if voice_model is None:
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
    raise FileNotFoundError(
        f"voice model {voice_model!r} not found. Searched:\n  {searched}\n"
        f"Run `showtape fetch-voice {DEFAULT_VOICE_MODEL_NAME}` to install."
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
    total = sum(BROWSER_ACTION_ESTIMATES.get(next(iter(a)), 1000)
                for a in (actions or []) if isinstance(a, dict))
    return total + 500


def run_browser_action(page, action):
    if not isinstance(action, dict) or len(action) != 1:
        raise ValueError(f"browser action must be a single-key mapping: {action!r}")
    key, val = next(iter(action.items()))
    if key == "goto":
        page.goto(val, wait_until="domcontentloaded", timeout=20000)
    elif key == "fill":
        page.fill(val["selector"], val["value"], timeout=10000)
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


def record_browser_pane(pane, target_ms, dims, video_dir):
    video_dir.mkdir(parents=True, exist_ok=True)
    session = pane.get("session", "default")
    actions = pane.get("actions", [])
    storage = _session_storage.get(session)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx_kwargs = dict(
            viewport={"width": dims[0], "height": dims[1]},
            record_video_dir=str(video_dir),
            record_video_size={"width": dims[0], "height": dims[1]},
        )
        if storage is not None:
            ctx_kwargs["storage_state"] = storage
        ctx = browser.new_context(**ctx_kwargs)
        page = ctx.new_page()
        start = time.monotonic()
        try:
            for action in actions:
                run_browser_action(page, action)
        except Exception as e:
            print(f"  ! browser action error: {e}", file=sys.stderr)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        remaining = target_ms - elapsed_ms
        if remaining > 0:
            page.wait_for_timeout(remaining)
        try:
            _session_storage[session] = ctx.storage_state()
        except Exception as e:
            print(f"  ! could not save session storage: {e}", file=sys.stderr)
        ctx.close()
        browser.close()

    webms = sorted(video_dir.glob("*.webm"))
    if not webms:
        raise RuntimeError(f"playwright produced no webm in {video_dir}")
    return webms[-1]


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


def _tape_header(output_mp4, dims):
    return [
        f'Output "{output_mp4}"',
        f"Set Width {dims[0]}",
        f"Set Height {dims[1]}",
        "Set FontSize 28",
        f"Set TypingSpeed {DEFAULT_TYPING_SPEED_MS}ms",
        'Set Theme "Dracula"',
        "Set Padding 30",
        "Set Shell /bin/bash",
    ]


def compile_tape(actions, target_ms, output_mp4, dims):
    """Tape for a single per-step terminal pane (no session continuity)."""
    lines = _tape_header(output_mp4, dims)
    action_lines, used_ms = _emit_terminal_actions(actions)
    lines.extend(action_lines)
    remaining = target_ms - used_ms
    if remaining > 0:
        lines.append(f"Sleep {remaining}ms")
    return "\n".join(lines) + "\n"


def record_terminal_pane(pane, target_ms, dims, work_dir, key):
    work_dir.mkdir(parents=True, exist_ok=True)
    tape_path = work_dir / f"{key}.tape"
    out_path = (work_dir / f"{key}.mp4").resolve()
    tape_path.write_text(compile_tape(pane.get("actions", []), target_ms, out_path, dims))
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


def _compute_session_geometry(unique_dims, cols=80, padding=30):
    """Return (rows, {dims: font_size}) for a tmux session shared across dims.

    Font is sized so `cols` columns fill the post-padding viewport width.
    Session rows = smallest natural row count across all dims so every client
    displays the full grid without clipping.
    """
    font_map = {}
    all_rows = []
    for (w, h) in unique_dims:
        inner_w = max(1, w - 2 * padding)
        inner_h = max(1, h - 2 * padding)
        font = max(8, round(inner_w / (cols * _CHAR_WIDTH_RATIO)))
        rows = max(4, int(inner_h / (font * _LINE_HEIGHT_RATIO)))
        font_map[(w, h)] = font
        all_rows.append(rows)
    return min(all_rows), font_map


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


def _drive_actions_via_tmux(tmux_sid, actions):
    """Drive terminal actions into a live tmux session via send-keys.

    Mirrors _emit_terminal_actions semantics (same action types, same timing)
    but sends to a live shell instead of emitting tape lines.
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


def _setup_sessions(step_plans):
    """Start one tmux session per unique session: <id> found in step_plans.

    Returns {sid: (tmux_sid, font_map)} where font_map is {dims: font_size}.
    Call _teardown_sessions() when rendering is complete (or on error).
    """
    sessions = _collect_terminal_sessions(step_plans)
    result = {}
    for sid, occurrences in sessions.items():
        unique_dims = _unique_session_dims(occurrences)
        rows, font_map = _compute_session_geometry(unique_dims)
        tmux_sid = f"st_{sid}"
        # LC_ALL=C avoids "cannot change locale" warnings from bash startup.
        # /bin/bash keeps the demo shell predictable — no zsh/oh-my-zsh prompt
        # redraws that would cause visual noise in recordings.
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", tmux_sid,
             "-x", "80", "-y", str(rows), "-e", "LC_ALL=C", "/bin/bash"],
            check=True,
        )
        subprocess.run(
            ["tmux", "set-option", "-t", tmux_sid, "window-size", "manual"],
            check=True, capture_output=True,
        )
        result[sid] = (tmux_sid, font_map)
        dim_str = ", ".join(f"{w}x{h}" for (w, h) in unique_dims)
        print(f"  session '{sid}' → tmux '{tmux_sid}' 80x{rows} ({dim_str})")
    return result


def _teardown_sessions(session_map):
    """Kill all tmux sessions started by _setup_sessions."""
    for _sid, (tmux_sid, _font_map) in session_map.items():
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
    default_voice = spec.get("voice", 0)
    pronunciations = spec.get("pronunciations") or {}

    out_path = Path(out or Path.cwd() / "out" / f"{yaml_path.stem}.mp4").resolve()
    work = Path(work_dir or Path.cwd() / ".showtape-work").resolve()
    if work.exists() and not keep_work:
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)

    voice_path = resolve_voice_model(voice_model)
    print(f"Loading Piper voice from {voice_path}...")
    voice = PiperVoice.load(str(voice_path))

    # Reset browser session storage per-render.
    _session_storage.clear()

    # ---- Pass 1: plan every step (synth narration, compute durations, layout) ----
    # Splitting this off lets us know all step durations BEFORE we render any
    # cross-step terminal sessions, which need the per-step durations to
    # build their tapes.
    print("\n=== Planning steps (synth + duration estimates) ===")
    step_plans = []
    for i, step in enumerate(spec["steps"]):
        sid = step.get("id", f"step{i}")
        narration = step.get("narration", "")
        end_buffer_ms = int(step.get("end_buffer_ms", step.get("pause_ms", 0)))
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
        }
        step_plans.append(plan)
        layout_str = f", {layout}" if layout else ""
        print(f"  [{i}] {sid} ({n} pane{'s' if n > 1 else ''}{layout_str}) "
              f"narration={narration_ms}ms est={estimates} end_buffer={end_buffer_ms}ms → step={step_ms}ms")

    # ---- Pass 2: start tmux sessions for all session: <id> panes ----
    print("\n=== Starting tmux sessions ===")
    session_map = _setup_sessions(step_plans)

    # ---- Pass 3: record panes and composite, step by step ----
    # Each step's panes are recorded in sequence; all pane recordings complete
    # before the step is composited and we move to the next step.
    clip_paths = []
    try:
        for plan in step_plans:
            i, sid, n, layout = plan["idx"], plan["sid"], plan["n"], plan["layout"]
            step_ms, panes, dims_list = plan["step_ms"], plan["panes"], plan["dims_list"]
            narration_wav = plan["narration_wav"]
            layout_str = f", {layout}" if layout else ""
            print(f"\n=== [{i}] {sid} ({n} pane{'s' if n > 1 else ''}{layout_str}) ===")

            pane_videos = []
            for j, (pane, dims) in enumerate(zip(panes, dims_list)):
                t = pane["type"]
                if t == "browser":
                    v = record_browser_pane(pane, step_ms, dims,
                                            work / "panes" / f"{i}-{j}-browser")
                    sess_label = f" session={pane.get('session', 'default')}"
                elif t == "terminal" and "session" in pane:
                    sid_term = pane["session"]
                    tmux_sid, font_map = session_map[sid_term]
                    v = record_terminal_session_pane(
                        pane, step_ms, dims,
                        work / "panes", f"{i}-{j}-sess",
                        tmux_sid, font_map[dims],
                    )
                    sess_label = f" session={sid_term} @ {dims[0]}x{dims[1]}"
                else:
                    v = record_terminal_pane(pane, step_ms, dims,
                                             work / "panes", f"{i}-{j}-term")
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
