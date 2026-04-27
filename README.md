# Showtape

A lightweight, open-source, in-container demo recorder. Define a step-by-step demo in YAML — multiple browser and terminal panes in a 1–4-pane grid, with TTS narration — and Showtape produces a single MP4. Built from open-source parts: Playwright (browser), VHS (terminal), Piper (TTS), FFmpeg (composition).

## Use it as a devcontainer feature

In any project's `.devcontainer/devcontainer.json`:

```jsonc
{
  "image": "mcr.microsoft.com/devcontainers/base:debian",
  "features": {
    "ghcr.io/scottrigby/showtape/showtape:0.3.0": {}
  }
}
```

That installs everything (Playwright + Chromium, FFmpeg, VHS, ttyd, the `showtape` CLI, and a default Piper voice). Then in your project:

```bash
showtape render demos/feature-walkthrough.yaml
# → ./out/feature-walkthrough.mp4
```

Feature options (set in `devcontainer.json`):

| Option | Default | Effect |
|---|---|---|
| `version` | `main` | Git ref of `scottrigby/showtape` to install — branch (`main`), tag (`v0.3.0`), or commit. Pin to a tag for reproducible builds. |
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

**Pronunciations** are a top-level YAML map applied as whole-word, case-insensitive substitutions before Piper synthesises each step's narration. Use plain respellings (`Kubernetes: "kuber-NETT-eez"`) for most cases, or espeak's inline IPA syntax (`GitHub: "[[g'It_hVb]]"`) when respelling doesn't sound right.

## CLI

```bash
showtape render <yaml> [--out PATH] [--work-dir DIR] [--voice-model NAME] [--keep-work]
showtape fetch-voice <name> [--dir voices/] [--force]
showtape --version
```

`render` defaults are cwd-relative: output to `./out/<stem>.mp4`, scratch in `./.showtape-work/`, voice model looked up under `./voices/`, then `/usr/local/share/showtape/voices/`, then `~/.cache/showtape/voices/`.

## Contributing

The repo eats its own dog food: opening it in a devcontainer-aware editor (VS Code Dev Containers extension, JetBrains Gateway, `devcontainer-cli`) builds a dev environment via the showtape feature itself, then `pip install -e .` overrides the from-git install with the live source.

```bash
git clone https://github.com/scottrigby/showtape && cd showtape
# In VS Code: "Reopen in Container" — or:
devcontainer up --workspace-folder .
devcontainer exec --workspace-folder . showtape fetch-voice en_US-libritts_r-medium
devcontainer exec --workspace-folder . showtape render demos/example.yaml
```

### Cutting a release

The Python package version (`pyproject.toml`) and the feature artifact version (`feature/showtape/devcontainer-feature.json`) must agree. `scripts/bump-version.sh` keeps them in sync; CI (`.github/workflows/release.yaml`) does everything else when a version-bump commit lands on `main`.

```bash
./scripts/bump-version.sh 0.3.0
git diff                                # sanity-check
git commit -am "Bump to v0.3.0"
git push origin main                    # CI tags v0.3.0 + publishes OCI feature + creates GitHub Release
```

What CI does on each push to `main` that touches `pyproject.toml` or the feature manifest:
1. Asserts that `pyproject.toml` and `feature/showtape/devcontainer-feature.json` versions agree.
2. If a `v<version>` tag doesn't already exist, creates it on that commit and pushes.
3. Publishes the OCI feature to `ghcr.io/scottrigby/showtape/showtape:<version>` (plus floating `:0.x`, `:0`, `:latest`).

The git tag is the canonical release marker — there's no GitHub Release yet (deliberately, while the project is in 0.x and changing fast). When a release warrants written notes, add a `softprops/action-gh-release` step to the workflow.

If the two version files disagree, the workflow fails loudly — fix the files (or re-run `bump-version.sh`) and push again. The version-tag check is idempotent: pushing a no-op commit on `main` won't double-publish.

## Architecture & repo layout

See [ARCHITECTURE.md](./ARCHITECTURE.md).
