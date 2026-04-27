# Architecture

A single-script pipeline that turns a YAML demo spec into a narrated multi-pane MP4 (1–4 panes per step, browser and/or terminal, in chosen layouts). Everything is open source.

## Files

### Code
| File | Purpose |
|---|---|
| `record_demo.py` | The whole pipeline. ~340 lines of Python. Reads the YAML, drives Piper / Playwright / VHS / FFmpeg in sequence, and writes the final MP4. |
| `demos/example.yaml` | 2-pane TTS demo (the original example). |
| `demos/multi-layout.yaml` | Layout showcase — 1, 2, 3-pane variants, and 4-pane grid. |
| `requirements.txt` | Python deps: `piper-tts`, `playwright`, `pyyaml`. |

### Environment
| File | Purpose |
|---|---|
| `claudeman-profile.json` | Source of truth for the [claudeman](https://github.com/scottrigby/claudeman) profile. Declares devcontainer features (Python, FFmpeg, the apt packages list), persistent caches (pip, Playwright browsers), and outbound firewall domains. Built once per profile rebuild. |
| `.claude/claudeman/profiles/demo-recorder.json` | Symlink to `../../../claudeman-profile.json` — puts the profile where claudeman looks for project-scoped profiles, so the JSON only lives in one place. |
| `setup.sh` | One-shot, runs as the unprivileged user inside the container. Installs everything the profile can't: Python deps (pip), Playwright's Chromium, VHS + ttyd as static binaries, plus a `chromium` symlink onto PATH so VHS's bundled go-rod finds the browser instead of trying to download its own. |

### Reference
| File | Purpose |
|---|---|
| `voices/` | Piper voice models (gitignored — fetch from Hugging Face per the README). |
| `examples/piper-gradio/` | Earlier Gradio Piper sandbox kept around for reference; not part of the pipeline. |
| `PLAN.md` | Original design doc. |
| `README.md` | Quick start. |
| `out/`, `work/` | Generated artifacts (gitignored). |

## Pipeline

For each step in the YAML, in order:

1. **Narration** — `voice.synthesize(text)` → iterate `AudioChunk` objects, write `chunk.audio_int16_bytes` into a `wave.open()` file. Empty narration produces a silent WAV of the step's duration instead.
2. **Step duration** — `max(narration_ms, …per-pane action estimates) + pause_ms`. Each pane's estimate is a sum of per-action constants (`goto`=3 s, `fill`=0.3 s, etc.) plus a 0.5 s safety margin. This makes the duration data-driven instead of narration-only, so a long form-fill flow naturally extends the step.
3. **Pane recording** (one per pane, sequentially):
   - Browser panes — Playwright launches headless Chromium, replays the `actions:` list (`goto`, `fill`, `click`, `wait_for`, `press`, `scroll`, `type`), then `wait_for_timeout(remaining_ms)` to fill the step. `record_video_dir` produces a WebM at the pane's exact final dimensions. Cookies/localStorage are loaded from / saved to a module-level `session_storage` dict keyed by the pane's `session:` field, so a session persists across steps within one run.
   - Terminal panes — A `.tape` file is generated from the YAML's `actions` list, padded with a trailing `Sleep` to the step duration, and fed to VHS.
4. **Per-step composite** — FFmpeg's filter graph (one of seven shapes, picked by pane count + layout) stitches the pane videos into a single 1920×1080 frame, attaches the narration audio (`apad=whole_dur=step_s`), and re-encodes everything to a uniform timebase / fps / sample rate / pixel format. A hard `-t` cap guarantees the clip is exactly `step_ms` long.

After all steps: a concat demuxer streams the per-step clips into the final MP4 with no re-encode (safe because every clip already matches in format).

## Layouts

| Panes | Layout option | Big pane (index 0) | Small panes |
|---|---|---|---|
| 1 | (fixed) | full screen | — |
| 2 | (fixed) | left half | right half |
| 3 | `3-left` (default) | left full-height | top-right + bottom-right |
| 3 | `3-right` | right full-height | top-left + bottom-left |
| 3 | `3-top` | top full-width | bottom-left + bottom-right |
| 3 | `3-bottom` | bottom full-width | top-left + top-right |
| 4 | (fixed) | top-left of 2×2 | TR, BL, BR |

Each pane's WebM/MP4 is recorded at its exact final dimensions, so the composite step never rescales — only normalises fps and sample aspect ratio before stacking.

## Choices worth knowing

- **Step duration is data-driven, narration is *one* contributor.** Step length = `max(narration, browser estimates, terminal estimates) + pause_ms`. Visuals never get rate-changed; audio is padded with silence instead. Whichever stream is longest wins the step length.
- **Per-step re-encode, then concat-copy.** Avoids the audio/video drift you get from concatenating webms with different keyframe alignment. Each clip ends up a uniform building block.
- **Headless-only browser, no Xvfb.** Playwright's `record_video_dir` records the page directly. Multiple browser panes within a step are independent contexts (each its own WebM) — no tab-switching inside a single recording. Cross-step continuity comes from `session:` storage instead.
- **`session:` persists cookies, not page state.** Across steps, a session's cookies and localStorage survive (saved/restored via `storage_state`); JavaScript-memory state (modal-open, half-typed form input not yet committed) does not. Cheap to implement, covers most real signup/auth flows, accept the limitation for the POC.
- **VHS for the terminal pane.** Beats asciinema+agg (GIF-only, needs another transcode) and live xterm capture (fragile, needs Xvfb). Cost: VHS bundles Chromium for headless terminal rendering, which we share with Playwright's via a PATH symlink.
- **claudeman profile for environment.** Declarative features + caches + firewall scope, all in one JSON. The profile + Anthropic upstream image cover everything except a few binaries that have to live in user space (VHS, ttyd) because the unprivileged user has no sudo.
- **POC scope.** One Python file, no abstractions, no error retries, no progress bars. Easy to read; easy to throw away when the shape of the project changes.
