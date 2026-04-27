# Showtape

A lightweight, open-source, in-container demo recorder. Define a step-by-step demo in YAML — multiple browser and terminal panes in a 1–4-pane grid, with TTS narration — and Showtape produces a single MP4. Built from open-source parts: Playwright (browser), VHS (terminal), Piper (TTS), FFmpeg (composition).

## Use it as a devcontainer feature

In any project's `.devcontainer/devcontainer.json`:

```jsonc
{
  "image": "mcr.microsoft.com/devcontainers/base:debian",
  "features": {
    "ghcr.io/scottrigby/showtape/showtape:0.2.0": {}
  }
}
```

(The duplicated `showtape/showtape` is GHCR's convention for single-feature repos: `<owner>/<repo>/<feature-id>`. Tag options: `:0.2.0` exact, `:0.2`/`:0` floating, `:latest` always newest. The `:1` major-version tag will only exist once a 1.x.y release is published.)

That installs everything (Playwright + Chromium, FFmpeg, VHS, ttyd, the `showtape` CLI, and a default Piper voice). Then in your project:

```bash
showtape render demos/feature-walkthrough.yaml
# → ./out/feature-walkthrough.mp4
```

Feature options (set in `devcontainer.json`):

| Option | Default | Effect |
|---|---|---|
| `version` | `main` | Git ref of `scottrigby/showtape` to install — branch (`main`), tag (`v0.2.0`), or commit. Pin to a tag for reproducible builds. |
| `voiceModel` | `en_US-libritts_r-medium` | Piper voice to pre-fetch. Empty string disables. |
| `installChromium` | `true` | Install Playwright's Chromium + system deps. Set false for terminal-only demos. |

The feature `dependsOn` `python`, `ffmpeg-apt-get`, and `apt-packages` (with the right Chromium runtime libs already filled in), so you don't need to list those yourself.

## YAML schema

```yaml
title: my-feature-walkthrough
resolution: { w: 1920, h: 1080 }
voice: 0    # Piper speaker id

pronunciations:                       # optional — applied to every step's narration
  Kubernetes: "kuber-NETT-eez"        # whole-word, case-insensitive substitution
  k8s: "kates"                        # respellings
  showtape: "show tape"               # add a syllable break
  GitHub: "[[g'It_hVb]]"              # or espeak inline IPA when respelling falls short

steps:
  - narration: "Open the dashboard."
    pause_ms: 250                  # optional, post-step extension
    panes:                         # 1–4 entries; layout is derived
      - type: browser
        session: dashboard         # optional; cookies/storage persist across steps
        actions:
          - goto: "https://example.com/login"
          - fill: { selector: "[name=email]", value: "demo@x.com" }
          - click: "text=Submit"
          - wait_for: "[data-dashboard]"
          - press: { selector: "input", key: "Enter" }
          - scroll: { y: 400 }
      - type: terminal
        actions:
          - type: "tail -f /var/log/app.log"
          - enter: true
          - sleep_ms: 500
```

Layouts come from pane count:

| Panes | Layout | Notes |
|---|---|---|
| 1 | full screen | — |
| 2 | side-by-side | — |
| 3 | `3-left` (default), `3-right`, `3-top`, `3-bottom` | Pane 0 is the "big" pane |
| 4 | 2×2 grid | Index order: TL, TR, BL, BR |

Step duration = `max(narration, all action estimates) + pause_ms`. Each pane stretches to fill the step.

**Browser sessions** persist cookies / localStorage across steps within a render — `session: gmail` in step 2 and again in step 5 stays logged in. JavaScript-memory state (unsubmitted form values, open modals) does *not* persist; only what the page itself writes to cookies/storage.

**Pronunciations** are a top-level YAML map applied as whole-word, case-insensitive substitutions before Piper synthesises each step's narration. Use plain respellings (`Kubernetes: "kuber-NETT-eez"`) for most cases, or espeak's inline IPA syntax (`GitHub: "[[g'It_hVb]]"`) when respelling doesn't sound right. The map is per-demo (lives in the YAML); promote frequently-used words to a shared file and merge yourself if you author many demos sharing the same vocabulary.

## CLI

```bash
showtape render <yaml> [--out PATH] [--work-dir DIR] [--voice-model NAME] [--keep-work]
showtape fetch-voice <name> [--dir voices/] [--force]
showtape --version
```

`render` defaults are cwd-relative: output to `./out/<stem>.mp4`, scratch in `./.showtape-work/`, voice model looked up under `./voices/`, then `/usr/local/share/showtape/voices/`, then `~/.cache/showtape/voices/`.

## Local development on showtape itself

```bash
git clone https://github.com/scottrigby/showtape && cd showtape
bash setup.sh                          # editable pip install + binaries
showtape fetch-voice en_US-libritts_r-medium
showtape render demos/example.yaml     # smoke test
```

## Repo layout

```
pyproject.toml            Package metadata + console_scripts entry
src/showtape/             Python package
  cli.py                  argparse subcommands (render, fetch-voice)
  recorder.py             Render pipeline (YAML → MP4)
feature/                  Devcontainer feature
  devcontainer-feature.json
  install.sh              Runs at build time as root; installs the rest
demos/                    Example YAMLs
voices/                   Piper voice models (gitignored)
out/                      Generated MP4s (gitignored)
.showtape-work/           Per-step intermediate artifacts (gitignored)
setup.sh                  Dev convenience: pip install -e . + binaries
claudeman-profile.json    Claudeman dev profile for working on showtape itself
.claude/claudeman/profiles/showtape.json → ../../../claudeman-profile.json
```
