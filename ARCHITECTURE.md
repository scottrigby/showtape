# Architecture

A pipeline that turns a YAML demo spec into a narrated multi-pane MP4 (1–4 panes per step, browser and/or terminal, in chosen layouts). Open-source from end to end. Distributed as a devcontainer feature so any project can consume it without depending on the source tree.

## File layout

### Code
| File | Purpose |
|---|---|
| `src/showtape/cli.py` | argparse-based CLI: `showtape render <yaml>`, `showtape fetch-voice <name>`. Lazy-imports `recorder` so `--version` doesn't pull Piper/Playwright. |
| `src/showtape/recorder.py` | The whole rendering pipeline. ~440 lines. Reads YAML, drives Piper / Playwright / VHS / FFmpeg in sequence, writes the final MP4. |
| `src/showtape/__init__.py` | Reads `__version__` via `importlib.metadata` so it always tracks `pyproject.toml`. |

### Distribution
| File | Purpose |
|---|---|
| `pyproject.toml` | Package metadata (PEP 621). Single source of truth for the showtape Python package version. Declares the `showtape` console script. |
| `feature/showtape/devcontainer-feature.json` | Devcontainer feature manifest — options (`version`, `voiceModel`, `installChromium`), `dependsOn` other features, sets `containerEnv.PLAYWRIGHT_BROWSERS_PATH`. |
| `feature/showtape/install.sh` | Runs as root at devcontainer build time, after the dependsOn features. Installs VHS + ttyd binaries, pip-installs `showtape` from the configured git ref, downloads Playwright Chromium to a system-wide path, symlinks Chromium onto PATH for VHS, pre-fetches a Piper voice. |
| `.devcontainer/devcontainer.json` | Dev container for the repo *itself* — pulls the published showtape feature, then `pip install -e .` overrides the from-git install with the live source. |
| `.github/workflows/release.yaml` | Triggered on git tag push (`v*`). Asserts that pyproject.toml + feature.json + tag all agree, publishes the OCI feature to ghcr.io, creates a GitHub Release. |
| `scripts/bump-version.sh` | One-command sync of the two version fields (`pyproject.toml` + `feature/showtape/devcontainer-feature.json`). |

### Examples & generated
| File | Purpose |
|---|---|
| `demos/example.yaml` | 5-step Piper TTS demo (split-screen). |
| `demos/multi-layout.yaml` | Layout showcase — 1, 2, 3-pane variants, 4-pane grid. |
| `voices/` | Piper voice models (gitignored — fetch via `showtape fetch-voice`). |
| `out/`, `.showtape-work/` | Generated artifacts (gitignored). |

## Pipeline

For each step in the YAML, in order:

1. **Narration synthesis** — `voice.synthesize(text)` → iterate `AudioChunk` objects, write `chunk.audio_int16_bytes` into a `wave.open()` file. The text is first run through the project's `pronunciations:` map for whole-word substitutions. Empty narration produces a silent WAV of the step's duration instead.
2. **Step duration** — `max(narration_ms, …per-pane action estimates) + end_buffer_ms`. Each pane's estimate is a sum of per-action constants (`goto`=3 s, `fill`=0.3 s, etc.) plus a 0.5 s safety margin. Data-driven instead of narration-only, so a long form-fill flow naturally extends the step.
3. **Pane recording** (sequential, one per pane):
   - **Browser panes** — Playwright launches headless Chromium, replays the `actions:` list (`goto`, `fill`, `click`, `wait_for`, `press`, `scroll`, `type`), then `wait_for_timeout(remaining_ms)` to fill the step. `record_video_dir` produces a WebM at the pane's exact final dimensions. Cookies/localStorage are loaded from / saved to a module-level `session_storage` dict keyed by the pane's `session:` field, so a session persists across steps within one run.
   - **Plain terminal panes** — A `.tape` file is generated from the YAML's `actions` list, padded with a trailing `Sleep` to the step duration, and fed to VHS.
   - **Session terminal panes** — A fresh VHS client attaches to the live tmux session for this step, the step's actions are driven via `tmux send-keys`, and VHS exits when the step's duration elapses. The tmux session persists across steps so scrollback accumulates naturally; each step's recording is self-contained with no slicing or offset math.
4. **Per-step composite** — FFmpeg's filter graph (one of seven shapes, picked by pane count + layout) stitches the pane videos into a single 1920×1080 frame, draws 1px black separators between adjacent panes, attaches the narration audio (`apad=whole_dur=step_s`), and re-encodes everything to a uniform timebase / fps / sample rate / pixel format. A hard `-t` cap guarantees the clip is exactly `step_ms` long.

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

Each pane's WebM/MP4 is recorded at its exact final dimensions, so the composite step never rescales — only normalises fps and sample aspect ratio before stacking. 1px black separators are drawn after stacking to prevent same-coloured panes (two browsers, two terminals) from bleeding into each other.

## Choices worth knowing

- **Single source of truth for the package version: `pyproject.toml`.** `__init__.py` reads via `importlib.metadata`, so `showtape --version` always reflects the canonical value. The feature artifact's version (in `devcontainer-feature.json`) has to be written separately because the spec mandates it; `scripts/bump-version.sh` keeps both in sync, and CI asserts they match the git tag at release time.
- **Step duration is data-driven, narration is *one* contributor.** Step length = `max(narration, browser estimates, terminal estimates) + end_buffer_ms`. Visuals never get rate-changed; audio is padded with silence instead. Whichever stream is longest wins the step length.
- **Per-step re-encode, then concat-copy.** Avoids the audio/video drift you get from concatenating WebMs with different keyframe alignment. Each clip ends up a uniform building block.
- **Headless-only browser, no Xvfb.** Playwright's `record_video_dir` records the page directly. Multiple browser panes within a step are independent contexts (each its own WebM) — no tab-switching inside a single recording. Cross-step continuity comes from `session:` storage instead.
- **`session:` persists cookies, not page state.** Across steps, a *browser* session's cookies and localStorage survive (saved/restored via `storage_state`); JavaScript-memory state (modal-open, half-typed form input not yet committed) does not. Cheap to implement, covers most real signup/auth flows. (Browser visual continuity — same scroll position, same in-flight request — is a planned v0.7.0 enhancement that will mirror the terminal-session approach.)
- **Terminal sessions: one tmux session per id, one VHS recording per step.** A session that appears across multiple steps (and at different viewport dims) is backed by a single detached tmux server-side session that stays alive for the entire render. Each step that includes a session pane attaches a fresh VHS client to that tmux session, drives the step's actions via `tmux send-keys`, and exits when the step duration elapses. No batch recording, no time-based slicing, no scale factors — each step's MP4 is exactly what happened during that step. Commands execute exactly once (safe for write-ops: `helm upgrade`, `kubectl apply`, `git push`). The tmux session is set to a canonical 80-column width with `window-size manual`; font size is computed per dim so 80 cols fills each viewport, keeping scrollback visually consistent across dim changes. `/bin/bash` with `LC_ALL=C` is used so demo terminals are clean and predictable regardless of the host's default shell.
- **`pronunciations:` is plain substitution, not phoneme-aware.** Whole-word, case-insensitive find/replace on the narration text *before* Piper's phonemiser runs. Plain respellings (`Kubernetes: "kuber-NETT-eez"`) usually suffice; espeak inline IPA (`[[k_u:b@`net@s]]`) is supported as a fallback because the substitution is verbatim.
- **VHS for the terminal pane.** Beats asciinema+agg (GIF-only, needs another transcode) and live xterm capture (fragile, needs Xvfb). VHS bundles its own go-rod-driven Chromium for headless terminal rendering — we shortcut that by symlinking Playwright's Chromium onto PATH so go-rod's PATH lookup finds it before falling back to its (often broken) downloader.
- **Playwright browsers in a system-wide path.** The feature install.sh sets `PLAYWRIGHT_BROWSERS_PATH=/usr/local/share/playwright` so root (build time) and the unprivileged user (run time) read from the same place. `containerEnv` in the feature manifest makes the path stick at runtime without consumers having to set it.
- **Two GHCR packages per release.** `ghcr.io/scottrigby/showtape/showtape` is the actual feature artifact; `ghcr.io/scottrigby/showtape` is collection metadata that feature-discovery tools (containers.dev) index. The duplicated `<repo>/<feature-id>` segment is the convention for single-feature repos — `devcontainer features publish` always appends the feature id after the namespace, and GHCR rejects bare `<owner>` namespace artifacts. `oras push` directly to the bare path is possible but abandons the upstream tooling for negligible gain.

## Operational layout (host vs container)

```
host (Mac):
  ~/code/.../showtape/                    repo working tree (this layout)

container (any devcontainer host):
  /workspaces/<project>/                  consumer project's source mount
  /usr/local/python/current/bin/          showtape, vhs, ttyd, chromium (symlink)
  /usr/local/share/playwright/            Chromium binary tree
  /usr/local/share/showtape/voices/       pre-fetched Piper voices
```

`showtape render` paths default to cwd-relative (`./out/`, `./.showtape-work/`), so the same CLI works in any project's workspace without configuration.
