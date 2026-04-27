# Open-source multi-pane demo recorder

A POC that records narrated product demos as a single MP4. Each step has 1–4 panes (browser or terminal) in a chosen layout, with TTS narration synced to the visuals. Built from open-source parts: Playwright (browser), VHS (terminal), Piper (TTS), FFmpeg (composition).

## Layout

```
claudeman-profile.json    Source of truth for the claudeman profile
record_demo.py            Entrypoint: YAML → MP4
setup.sh                  One-time install of bits the profile can't cover (VHS, ttyd, pip deps)
requirements.txt          Python deps
demos/example.yaml        Sample demo (Piper TTS story, 2-pane split-screen)
demos/multi-layout.yaml   Layout showcase (1, 2, 3-with-variants, 4 panes)
voices/                   Piper voice models (gitignored — see "Voice models" below)
out/                      Final MP4 lands here (gitignored)
work/                     Per-step intermediate artifacts (gitignored)
examples/piper-gradio/    Earlier Gradio TTS sandbox kept for reference
.claude/claudeman/profiles/demo-recorder.json  →  ../../../claudeman-profile.json
```

## Quick start

```bash
# Restart the session under the new profile (run on the host)
claudeman run --profile demo-recorder -- --continue

# Inside the container, one-time install
bash setup.sh

# Render a demo (output → out/<yaml-stem>.mp4)
python record_demo.py demos/example.yaml
python record_demo.py demos/multi-layout.yaml
```

On the Mac host: `open out/example.mp4` (or any other rendered file).

## Voice models

The Piper voice (`en_US-libritts_r-medium`) lives in `voices/` but is gitignored (~80 MB). Download from Hugging Face:

```bash
mkdir -p voices && cd voices
curl -LO https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/libritts_r/medium/en_US-libritts_r-medium.onnx
curl -LO https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/libritts_r/medium/en_US-libritts_r-medium.onnx.json
```

The model has 904 speakers; pick one with `voice: <0–903>` in your demo YAML.

## Demo YAML schema

Each step has `narration` (TTS), an optional `id`, an optional `pause_ms` to extend the step at the end, and a `panes` list (1–4 entries). Each pane has a `type` (`browser` or `terminal`), an optional `actions` list, and optional `session` (browsers only). Step duration = max(narration, all action estimates) + `pause_ms`, so panes naturally stay aligned.

```yaml
steps:
  - narration: "..."
    pause_ms: 250                    # optional, default 0
    panes:
      - type: browser
        session: one                 # optional, default "default"; persists across steps
        actions:
          - goto: "https://example.com/signup"
          - fill: { selector: "[name=email]", value: "demo@x.com" }
          - click: "text=Submit"
          - wait_for: "[data-verify]"        # selector or { ms: 1500 }
          - press: { selector: "input", key: "Enter" }
          - scroll: { y: 400 }
      - type: terminal
        actions:
          - type: "ls -lh"
          - enter: true
          - sleep_ms: 500
```

**Layouts** are derived from `len(panes)`: 1 = full screen, 2 = side-by-side, 4 = 2×2 grid. With 3 panes, set `layout:` to one of `3-left` (default), `3-right`, `3-top`, `3-bottom`. Pane index 0 is always the "big" pane in 3-pane layouts.

**Browser sessions** persist cookies and localStorage across steps within a run — call `session: gmail` in step 2 and `session: gmail` again in step 5, you're still logged in. State that lives only in JavaScript memory (unsubmitted form values, modal open/closed) does *not* persist; only what the page itself stores in cookies/storage. Sessions are scoped to a single `record_demo.py` run.

See `demos/example.yaml` for a 2-pane demo and `demos/multi-layout.yaml` for a layout tour.
