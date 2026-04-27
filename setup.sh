#!/usr/bin/env bash
# One-time setup for the demo-recorder POC, runs as the unprivileged user.
# The claudeman profile handles all apt installs via devcontainer features
# (Python, FFmpeg, DejaVu fonts, and Chromium runtime libs). The Anthropic
# upstream image doesn't grant the user passwordless sudo, so anything
# system-wide must come through a feature, not this script.
#
# This script installs the rest in user space:
#   - VHS binary (single Go binary, dropped into a writable PATH dir)
#   - ttyd binary (not in Debian apt repos; static binary from GitHub releases)
#   - Python deps (piper-tts, playwright, pyyaml)
#   - Playwright Chromium browser binary
set -euo pipefail

cd "$(dirname "$0")"

echo "==> Python deps"
pip install -r requirements.txt

echo "==> Playwright Chromium browser"
playwright install chromium

# /usr/local/python/current/bin is provided by the python devcontainer feature,
# is on PATH, and is writable by our user — perfect spot for user-installed binaries.
BIN_DIR="/usr/local/python/current/bin"

ARCH="$(uname -m)"
case "$ARCH" in
  aarch64|arm64) VHS_ARCH="arm64";  TTYD_ARCH="aarch64" ;;
  x86_64)        VHS_ARCH="x86_64"; TTYD_ARCH="x86_64"  ;;
  *) echo "Unsupported arch: $ARCH" >&2; exit 1 ;;
esac

if ! command -v vhs >/dev/null 2>&1; then
  echo "==> Installing VHS ($VHS_ARCH) → $BIN_DIR/vhs"
  TMP="$(mktemp -d)"
  curl -fsSL "https://github.com/charmbracelet/vhs/releases/latest/download/vhs_Linux_${VHS_ARCH}.tar.gz" \
    | tar xz -C "$TMP"
  install -m0755 "$TMP"/vhs*/vhs "$BIN_DIR/vhs"
  rm -rf "$TMP"
fi

if ! command -v ttyd >/dev/null 2>&1; then
  echo "==> Installing ttyd ($TTYD_ARCH) → $BIN_DIR/ttyd"
  curl -fsSL "https://github.com/tsl0922/ttyd/releases/latest/download/ttyd.${TTYD_ARCH}" \
    -o "$BIN_DIR/ttyd"
  chmod +x "$BIN_DIR/ttyd"
fi

echo "==> Versions"
vhs --version
ttyd --version | head -1
ffmpeg -version | head -1
python -c "import piper, playwright, yaml; print('piper, playwright, yaml: OK')"

echo "==> Setup complete."
