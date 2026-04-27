#!/usr/bin/env bash
# One-time setup for the demo-recorder POC.
# The claudeman profile provides Python + FFmpeg; everything else is here:
#   - ttyd + dejavu fonts (apt) for VHS rendering
#   - Chromium runtime libs (apt) — Playwright Python's bundled driver needs them
#   - VHS binary from GitHub releases
#   - Python deps (piper-tts, playwright, pyyaml)
#   - Playwright Chromium browser binary
#
# Note: we install Chromium deps via apt directly rather than using the
# `schlich/playwright` devcontainer feature. That feature pulls in
# `devcontainers/features/node`, which collides with NPM_CONFIG_PREFIX
# pre-set by Anthropic's upstream devcontainer image (nvm refuses to install).
# Playwright's Python binding ships its own Node driver, so we don't need
# system Node at all.
set -euo pipefail

cd "$(dirname "$0")"

echo "==> apt deps (ttyd, fonts, Chromium runtime libs)"
sudo apt-get update
sudo apt-get install -y \
  ttyd fonts-dejavu \
  libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
  libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
  libgbm1 libpango-1.0-0 libcairo2 libasound2

echo "==> Python deps"
pip install -r requirements.txt

echo "==> Playwright Chromium browser"
playwright install chromium

ARCH="$(uname -m)"
case "$ARCH" in
  aarch64|arm64) VHS_ARCH="arm64" ;;
  x86_64)        VHS_ARCH="x86_64" ;;
  *) echo "Unsupported arch: $ARCH" >&2; exit 1 ;;
esac

if ! command -v vhs >/dev/null 2>&1; then
  echo "==> Installing VHS (Linux $VHS_ARCH)"
  TMPDIR="$(mktemp -d)"
  curl -fsSL "https://github.com/charmbracelet/vhs/releases/latest/download/vhs_Linux_${VHS_ARCH}.tar.gz" \
    | tar xz -C "$TMPDIR"
  sudo install -m0755 "$TMPDIR"/vhs*/vhs /usr/local/bin/vhs
  rm -rf "$TMPDIR"
fi

echo "==> Versions"
vhs --version
ttyd --version | head -1
ffmpeg -version | head -1
python -c "import piper, playwright, yaml; print('piper, playwright, yaml: OK')"

echo "==> Setup complete."
