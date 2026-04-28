# Showtape

A lightweight, open-source, in-container demo recorder. Define a step-by-step demo in YAML — multiple browser and terminal panes in a 1–4-pane grid, with TTS narration — and Showtape produces a single MP4. Built from open-source parts: Playwright (browser), VHS (terminal), Piper (TTS), FFmpeg (composition).

## Use it as a devcontainer feature

In any project's `.devcontainer/devcontainer.json`:

```jsonc
{
  "image": "mcr.microsoft.com/devcontainers/base:debian",
  "features": {
    "ghcr.io/scottrigby/showtape/showtape:0.7.0": {}
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
| `version` | `main` | Git ref of `scottrigby/showtape` to install — branch (`main`), tag (`v0.7.0`), or commit. Pin to a tag for reproducible builds. |
| `voiceModel` | `en_US-libritts_r-medium` | Piper voice to pre-fetch. Empty string disables. |
| `installChromium` | `true` | Install Playwright's Chromium + system deps. Set false for terminal-only demos. |

The feature `dependsOn` `python`, `ffmpeg-apt-get`, and `apt-packages` (with the right Chromium runtime libs already filled in), so you don't need to list those yourself.

**Optional — persist large downloads across rebuilds.** The Chromium browser binary (~200 MB) and Piper voice models (~80 MB each) are version-stable, identical across consumers, and worth caching outside the build layer. Add named volumes to your devcontainer.json so they survive container rebuilds (and are shared across any other project on the same host that uses showtape):

```jsonc
"mounts": [
  "source=showtape-playwright,target=/usr/local/share/playwright,type=volume",
  "source=showtape-voices,target=/usr/local/share/showtape/voices,type=volume"
]
```

These are pure binary caches — nothing project-specific writes to either path. The smaller binaries (VHS, ttyd, the showtape Python package itself) are handled by Docker's image layer cache and don't need volumes.

## YAML schema

```yaml
title: my-feature-walkthrough
resolution: { w: 1920, h: 1080 }
voice_model: en_US-libritts_r-medium   # optional — auto-detected if only one model is installed
speaker: 0                             # optional — speaker index within the model (default 0)
terminal_font_size: 18                 # optional — applies to all terminal panes (default 18)

pronunciations:                       # optional — applied to every step's narration
  Kubernetes: "kuber-NETT-eez"        # whole-word, case-insensitive substitution
  k8s: "kates"                        # respellings
  showtape: "show tape"               # add a syllable break
  GitHub: "[[g'It_hVb]]"              # or espeak inline IPA when respelling falls short

steps:
  - narration: "Open the dashboard."
    end_buffer_ms: 250             # optional — extend step after all actions finish
    browser_warmup_ms: 1500        # optional — trim leading white canvas from browser panes
    record: false                  # optional — run actions but exclude step from output
    wait_ms: 5000                  # with record: false: pure Python sleep, no panes needed
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
          - type: "tail -f /var/log/app.log"        # types char-by-char at 50ms each
          - enter: true
          - sleep_ms: 500
          - paste: |                                # near-instant, multi-line
              helm upgrade --install my-app chart/ \
                -n staging \
                --set image.tag=v0.7.0
              kubectl -n staging get pods
          - sleep_ms: 60000                         # let the actual command run
```

Layouts come from pane count:

| Panes | Layout | Notes |
|---|---|---|
| 1 | full screen | — |
| 2 | side-by-side | — |
| 3 | `3-left` (default), `3-right`, `3-top`, `3-bottom` | Pane 0 is the "big" pane |
| 4 | 2×2 grid | Index order: TL, TR, BL, BR |

Step duration = `max(narration, all action estimates) + end_buffer_ms`. Each pane stretches to fill the step.

`sleep_ms:` in terminal actions is for **mid-action pauses** only (e.g., waiting for a command to finish before typing the next one). A trailing `sleep_ms:` at the end of an actions list is redundant — the step's remaining time already pads every pane to `step_ms`. Use `end_buffer_ms:` on the step instead to extend viewing time after all actions complete.

**Browser sessions** persist cookies / localStorage across steps within a render — `session: gmail` in step 2 and again in step 5 stays logged in. JavaScript-memory state (unsubmitted form values, open modals) does *not* persist; only what the page itself writes to cookies/storage.

**Terminal sessions** preserve scrollback across steps. A terminal pane with `session: <id>` shares one shell with every other pane using the same id, so commands run in step 1 are still on screen when the session reappears in step 5 — even if intervening steps don't include the terminal at all. Each step attaches a fresh VHS client to a persistent tmux session, records exactly that step's duration, and exits — no slicing or offset math. Sessions can appear at different viewport sizes across steps (e.g., split-screen then full-screen); commands execute exactly once, making sessions safe for write-ops (`helm upgrade`, `kubectl apply`, `git push`). See `demos/terminal-sessions.yaml` for a worked example.

**Pronunciations** are a top-level YAML map applied as whole-word, case-insensitive substitutions before Piper synthesises each step's narration. Use plain respellings (`Kubernetes: "kuber-NETT-eez"`) for most cases, or espeak's inline IPA syntax (`GitHub: "[[g'It_hVb]]"`) when respelling doesn't sound right.

**Terminal actions: `type:` vs `paste:`.** `type:` emits one character at a time (50 ms each — VHS default), giving the natural live-typing feel for short commands. `paste:` emits everything near-instantly, the way a paste from clipboard reads in a real terminal. Use it for long commands that would otherwise spend 10+ seconds typing letter-by-letter. `paste:` accepts multi-line YAML (with `|` literal block style) and treats each line as a separate command — backslash continuations work because bash reassembles them on its own.

**Cross-pane copy/paste.** Named buffers flow between terminal sessions, browser panes, and across steps.

`capture: <name>` — in a **session terminal**, snapshots the last command's visible output. A `sleep_ms:` before it is required so the command finishes first.

`paste_from: <name>` — in a **session terminal**, pastes the buffer near-instantly (same feel as `paste:`). Works in a later step than the `capture:` — buffers persist for the full render.

In a **browser pane**, `capture:` extracts DOM text and `fill:` accepts `paste_from:` instead of `value:`:

```yaml
# browser → terminal
- capture: { selector: "h1", to: page_title }        # DOM element innerText
- capture: { eval: "document.title", to: tab_title }  # JS expression

# terminal → browser
- fill: { selector: "input[name=q]", paste_from: search_term }
```

`read -s` + `paste_from:` is the right pattern for secrets — the buffer is typed silently into the shell without echoing. Useful for realistic "grab the pod name, use it in the next command" demos:

```yaml
- type: terminal
  session: shell-A
  actions:
    - type: "kubectl get pods -o name | head -1"
    - enter: true
    - sleep_ms: 2000
    - capture: pod_name        # snapshot output of the above command

- type: terminal
  session: shell-B
  actions:
    - type: "kubectl logs "
    - paste_from: pod_name     # types the captured value char-by-char
    - enter: true
    - sleep_ms: 3000
```

Buffers persist across steps within one render; a `capture:` in step 2 is available to `paste_from:` in step 4.

**Stick to ASCII in `type:`/`paste:` action strings.** Smart quotes, em dashes (`—`), and other Unicode punctuation are sent through VHS → ttyd → bash readline as multi-byte UTF-8 sequences, and at least some byte values get interpreted by readline as command-line edit operations (transposing words, killing the line, etc.). Use plain `-` instead of `—`, plain `'`/`"` instead of curly quotes. Narration text (which goes through Piper, not the shell) is fine with any Unicode.

## CLI

```bash
showtape render <yaml> [--out PATH] [--work-dir DIR] [--voice-model NAME] [--keep-work]
showtape fetch-voice <name> [--dir voices/] [--force]
showtape --version
```

`render` defaults are cwd-relative: output to `./out/<stem>.mp4`, scratch in `./.showtape-work/`, voice model looked up under `./voices/`, then `/usr/local/share/showtape/voices/`, then `~/.cache/showtape/voices/`.

## Contributing

The repo eats its own dog food: opening it in a devcontainer-aware editor (VS Code Dev Containers extension, JetBrains Gateway, `devcontainer-cli`) builds a dev environment via the showtape feature itself, then `pip install -e .` overrides the from-git install with the live source.

**If you open the repo via a generic devcontainer profile** (e.g. a claudeman profile that doesn't use the repo's own `.devcontainer/`) the editable install won't fire automatically. Run this once at the start of the session:

```bash
pip install --user -e . && export PATH="$HOME/.local/bin:$PATH"
```

```bash
git clone https://github.com/scottrigby/showtape && cd showtape
# In VS Code: "Reopen in Container" — or:
devcontainer up --workspace-folder . --log-format json   # --log-format json avoids progress-bar terminal hijack
devcontainer exec --workspace-folder . showtape render demos/example.yaml
```

### Cutting a release

The version is pinned in four places: `pyproject.toml`, `feature/showtape/devcontainer-feature.json`, the dev `.devcontainer/devcontainer.json`, and the README's example refs. `scripts/bump-version.sh` updates all four at once; `scripts/check-version-sync.sh` audits them. CI runs the same audit on every push to `main` and fails the release if anything is out of sync.

```bash
./scripts/bump-version.sh 0.3.0         # bumps + audits in one shot
git diff                                # sanity-check
git commit -am "Bump to v0.7.0"
git push origin main                    # CI tags v0.7.0 + publishes OCI feature
```

What CI does on each push to `main` that touches `pyproject.toml` or the feature manifest:
1. Asserts that `pyproject.toml` and `feature/showtape/devcontainer-feature.json` versions agree.
2. If a `v<version>` tag doesn't already exist, creates it on that commit and pushes.
3. Publishes the OCI feature to `ghcr.io/scottrigby/showtape/showtape:<version>` (plus floating `:0.x`, `:0`, `:latest`).

The git tag is the canonical release marker — there's no GitHub Release yet (deliberately, while the project is in 0.x and changing fast). When a release warrants written notes, add a `softprops/action-gh-release` step to the workflow.

If the two version files disagree, the workflow fails loudly — fix the files (or re-run `bump-version.sh`) and push again. The version-tag check is idempotent: pushing a no-op commit on `main` won't double-publish.

## Architecture & repo layout

See [ARCHITECTURE.md](./ARCHITECTURE.md).
