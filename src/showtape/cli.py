"""showtape CLI.

Subcommands:
    showtape render <yaml> [--out PATH] [--work-dir DIR] [--voice-model NAME]
    showtape fetch-voice <name> [--dir DIR]
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

from showtape import __version__

HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

# (voice_name, lang, region, family, quality) — used to build the HF download URL
KNOWN_VOICES = {
    "en_US-libritts_r-medium":  ("en", "en_US", "libritts_r", "medium"),
    "en_US-lessac-medium":      ("en", "en_US", "lessac",     "medium"),
    "en_US-amy-medium":         ("en", "en_US", "amy",        "medium"),
    "en_US-ryan-medium":        ("en", "en_US", "ryan",       "medium"),
    "en_GB-alan-medium":        ("en", "en_GB", "alan",       "medium"),
}


def cmd_render(args):
    # Imported lazily so `showtape --version` doesn't load Playwright/Piper.
    from showtape.recorder import render
    render(
        args.yaml_path,
        out=args.out,
        work_dir=args.work_dir,
        voice_model=args.voice_model,
        keep_work=args.keep_work,
    )


def cmd_fetch_voice(args):
    name = args.name
    if name not in KNOWN_VOICES:
        print(
            f"unknown voice {name!r}; known voices:\n  "
            + "\n  ".join(sorted(KNOWN_VOICES)),
            file=sys.stderr,
        )
        sys.exit(2)
    lang, locale, family, quality = KNOWN_VOICES[name]
    base = f"{HF_BASE}/{lang}/{locale}/{family}/{quality}/{name}"
    out_dir = Path(args.dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    for ext in (".onnx", ".onnx.json"):
        url = f"{base}{ext}"
        target = out_dir / f"{name}{ext}"
        if target.exists() and not args.force:
            print(f"already present: {target} (pass --force to re-download)")
            continue
        print(f"fetching {url}\n     → {target}")
        with urllib.request.urlopen(url) as r, open(target, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
    print(f"✅ voice {name!r} ready in {out_dir}")


def build_parser():
    p = argparse.ArgumentParser(prog="showtape",
                                description="Multi-pane in-container demo recorder.")
    p.add_argument("--version", action="version", version=f"showtape {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("render", help="Render a YAML demo spec to MP4.")
    r.add_argument("yaml_path", help="Path to the demo YAML.")
    r.add_argument("--out", default=None,
                   help="Output MP4 path. Default: ./out/<yaml-stem>.mp4")
    r.add_argument("--work-dir", default=None,
                   help="Scratch dir for per-step intermediates. Default: ./.showtape-work")
    r.add_argument("--voice-model", default=None,
                   help="Piper voice model name (looked up under voices/) or absolute path.")
    r.add_argument("--keep-work", action="store_true",
                   help="Don't wipe the work dir at start.")
    r.set_defaults(func=cmd_render)

    f = sub.add_parser("fetch-voice", help="Download a Piper voice model from Hugging Face.")
    f.add_argument("name", help=f"Voice name (one of: {', '.join(sorted(KNOWN_VOICES))})")
    f.add_argument("--dir", default="voices", help="Where to put the .onnx + .onnx.json. Default: ./voices/")
    f.add_argument("--force", action="store_true", help="Re-download even if files exist.")
    f.set_defaults(func=cmd_fetch_voice)

    return p


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
