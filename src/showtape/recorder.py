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

def synth(voice, text, speaker_id, out_path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(voice.config.sample_rate)
        for chunk in voice.synthesize(text):
            wav.writeframes(chunk.audio_int16_bytes)


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

def estimate_terminal_ms(actions):
    total = 0
    for a in actions or []:
        if not isinstance(a, dict):
            continue
        if "type" in a:
            total += len(a["type"]) * 50
        if a.get("enter"):
            total += 100
        if "sleep_ms" in a:
            total += int(a["sleep_ms"])
    return total + 500


def vhs_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def compile_tape(actions, target_ms, output_mp4, dims):
    lines = [
        f'Output "{output_mp4}"',
        f"Set Width {dims[0]}",
        f"Set Height {dims[1]}",
        "Set FontSize 28",
        "Set TypingSpeed 50ms",
        'Set Theme "Dracula"',
        "Set Padding 30",
    ]
    used_ms = 0
    for a in actions or []:
        if "type" in a:
            lines.append(f'Type "{vhs_escape(a["type"])}"')
            used_ms += len(a["type"]) * 50
        if a.get("enter"):
            lines.append("Enter")
            used_ms += 100
        if "sleep_ms" in a:
            lines.append(f'Sleep {int(a["sleep_ms"])}ms')
            used_ms += int(a["sleep_ms"])
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

    out_path = Path(out or Path.cwd() / "out" / f"{yaml_path.stem}.mp4").resolve()
    work = Path(work_dir or Path.cwd() / ".showtape-work").resolve()
    if work.exists() and not keep_work:
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)

    voice_path = resolve_voice_model(voice_model)
    print(f"Loading Piper voice from {voice_path}...")
    voice = PiperVoice.load(str(voice_path))

    # Reset session storage per-render.
    _session_storage.clear()

    clip_paths = []
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

        print(f"\n=== [{i}] {sid} ({n} pane{'s' if n > 1 else ''}{', ' + layout if layout else ''}) ===")
        print(f"  narration: {narration[:60]!r}{'...' if len(narration) > 60 else ''}")

        narration_wav = work / "audio" / f"{i}.wav"
        if narration:
            synth(voice, narration, default_voice, narration_wav)
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
        print(f"  narration={narration_ms}ms estimates={estimates} pause={pause_ms}ms → step={step_ms}ms")

        if not narration:
            silent_wav(narration_wav, step_ms)

        dims_list = pane_dimensions(n, output_w, output_h, layout)
        pane_videos = []
        for j, (pane, dims) in enumerate(zip(panes, dims_list)):
            t = pane["type"]
            if t == "browser":
                v = record_browser_pane(pane, step_ms, dims,
                                        work / "panes" / f"{i}-{j}-browser")
            else:
                v = record_terminal_pane(pane, step_ms, dims,
                                         work / "panes", f"{i}-{j}-term")
            pane_videos.append(v)
            sess = f" session={pane.get('session', 'default')}" if t == "browser" else ""
            print(f"  pane[{j}] {t}{sess} {dims} → {v.name}")

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
