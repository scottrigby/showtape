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
    and cross-step session tapes (_compile_session_tape).
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
# A terminal pane with `session: <id>` joins a shared shell that persists
# across every step using the same id. Implementation: build ONE tape per
# session covering all its occurrences in step-order (no inter-step Sleep —
# we only render time when the session is on screen), run VHS once, then
# ffmpeg-trim per-step slices. The shell stays alive for the entire VHS run
# so scrollback accumulates naturally.

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


def _compile_session_tape_for_dim(occurrences, output_mp4, dims):
    """Build a tape covering ALL of a session's occurrences at one viewport dim.

    Each (session, dim) pair gets its own tape + MP4. The shell runs the same
    commands and accumulates the same scrollback content; only the line-wrap
    at the viewport boundary differs. Per-step time offsets are identical
    across dim variants because step durations don't depend on viewport.

    Returns (tape_text, [(step_idx, start_ms_in_tape, duration_ms), ...]).
    """
    lines = _tape_header(output_mp4, dims)
    offsets = []
    cursor_ms = 0
    for step_idx, _pane_idx, pane, _dims, target_ms in occurrences:
        action_lines, used_ms = _emit_terminal_actions(pane.get("actions", []))
        lines.extend(action_lines)
        remaining = target_ms - used_ms
        if remaining > 0:
            lines.append(f"Sleep {remaining}ms")
        offsets.append((step_idx, cursor_ms, target_ms))
        cursor_ms += target_ms
    # Trailing safety buffer — VHS occasionally drops a few frames off the very
    # end of a tape, which would clip the last occurrence's slice. 1s of
    # trailing Sleep guarantees the last slice has full requested duration.
    lines.append("Sleep 1000ms")
    return "\n".join(lines) + "\n", offsets


def _measure_video_duration_ms(mp4_path):
    """Read a video's actual duration in milliseconds via ffprobe."""
    result = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(mp4_path)
    ], capture_output=True, text=True, check=True)
    return int(float(result.stdout.strip()) * 1000)


def _slice_session_video(session_mp4, start_ms, source_dur_ms, target_dur_ms, output_mp4):
    """Extract source_dur_ms starting at start_ms; pad to target_dur_ms with the
    frozen last frame if needed.

    VHS often renders typing slightly faster than the 50 ms/char our estimator
    assumes, so the actual session MP4 is shorter than the scripted total.
    Caller passes scaled (start_ms, source_dur_ms) for the actual content slice;
    target_dur_ms is the step's scripted duration. tpad freezes the last frame
    to bridge any shortfall, keeping the slice's visible duration consistent
    with the rest of the composite.
    """
    output_mp4.parent.mkdir(parents=True, exist_ok=True)
    pad_s = max(0, (target_dur_ms - source_dur_ms) / 1000)
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start_ms / 1000}",
        "-t", f"{source_dur_ms / 1000}",
        "-i", str(session_mp4),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-an",
    ]
    if pad_s > 0.01:
        cmd.extend(["-vf", f"tpad=stop_mode=clone:stop_duration={pad_s:.3f}"])
    cmd.append(str(output_mp4))
    subprocess.run(cmd, check=True, capture_output=True)
    return output_mp4


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
        pause_ms = int(step.get("pause_ms", 0))
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
        step_ms = max([narration_ms, *estimates]) + pause_ms

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
              f"narration={narration_ms}ms est={estimates} pause={pause_ms}ms → step={step_ms}ms")

    # ---- Pass 2: render terminal session tapes (one per (session, dim)) ----
    # When a session appears at multiple dims (e.g., a shell that's split-screen
    # in some steps and full-screen in others), we run VHS once per unique dim.
    # Same shell, same commands, same scrollback content — different line-wrap
    # at the viewport boundary. Cheap; deterministic.
    sessions = _collect_terminal_sessions(step_plans)
    session_videos = {}    # (session_id, dims) → (mp4_path, {step_idx: (start_ms, duration_ms)})
    for sid, occs in sessions.items():
        unique_dims = _unique_session_dims(occs)
        steps_str = ", ".join(str(o[0]) for o in occs)
        dim_str = ", ".join(f"{w}x{h}" for (w, h) in unique_dims)
        print(f"\n=== Terminal session '{sid}' (steps {steps_str}, dims {dim_str}) ===")
        sess_dir = work / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        for dims in unique_dims:
            suffix = f"_{dims[0]}x{dims[1]}" if len(unique_dims) > 1 else ""
            tape_path = sess_dir / f"{sid}{suffix}.tape"
            mp4_path = (sess_dir / f"{sid}{suffix}.mp4").resolve()
            tape_text, scripted_offsets = _compile_session_tape_for_dim(occs, mp4_path, dims)
            tape_path.write_text(tape_text)
            subprocess.run(["vhs", str(tape_path)], check=True)

            # VHS renders slightly faster than our 50 ms/char estimate (per-char
            # frame overhead, exact Sleep semantics). Scale the scripted offsets
            # to actual MP4 time so per-step slices land in the right segment
            # and don't bleed into the next.
            scripted_total_ms = sum(target_ms for _, _, target_ms in scripted_offsets) + 1000
            actual_total_ms = _measure_video_duration_ms(mp4_path)
            scale = actual_total_ms / scripted_total_ms
            actual_offsets = {
                step_idx: (int(start_ms * scale), int(dur_ms * scale))
                for step_idx, start_ms, dur_ms in scripted_offsets
            }
            print(f"  {dims[0]}x{dims[1]}: scripted={scripted_total_ms}ms "
                  f"actual={actual_total_ms}ms scale={scale:.4f}")
            session_videos[(sid, dims)] = (mp4_path, actual_offsets)

    # ---- Pass 3: render browser panes, slice session videos, composite ----
    clip_paths = []
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
                session_mp4, offset_map = session_videos[(sid_term, dims)]
                start_ms, source_dur_ms = offset_map[i]   # scaled to actual MP4 time
                v = work / "panes" / f"{i}-{j}-term-slice.mp4"
                _slice_session_video(session_mp4, start_ms, source_dur_ms, step_ms, v)
                sess_label = f" session={sid_term} (sliced from {start_ms}ms, src={source_dur_ms}ms→pad to {step_ms}ms @ {dims[0]}x{dims[1]})"
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
