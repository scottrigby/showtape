#!/usr/bin/env bash
# Development setup for the showtape repo itself.
#
# Consumers should NOT run this — they install the published devcontainer
# feature (`ghcr.io/scottrigby/showtape:1`) instead. This script is only
# for working on showtape's source: an editable pip install plus the
# system-level binaries the unprivileged user can drop into PATH (the
# claudeman base image doesn't grant passwordless sudo, so apt stays in
# the profile/feature layer).

set -euo pipefail
cd "$(dirname "$0")"

echo "==> editable install of showtape (pip install -e .)"
pip install -e .

echo "==> Playwright Chromium"
playwright install chromium

BIN_DIR="/usr/local/python/current/bin"   # writable, on PATH

ARCH="$(uname -m)"
case "$ARCH" in
  aarch64|arm64) VHS_ARCH="arm64";  TTYD_ARCH="aarch64" ;;
  x86_64)        VHS_ARCH="x86_64"; TTYD_ARCH="x86_64"  ;;
  *) echo "Unsupported arch: $ARCH" >&2; exit 1 ;;
esac

if ! command -v vhs >/dev/null 2>&1; then
  VHS_TAG="$(curl -fsSL https://api.github.com/repos/charmbracelet/vhs/releases/latest \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])')"
  VHS_VER="${VHS_TAG#v}"
  echo "==> Installing VHS ${VHS_TAG} ($VHS_ARCH) → $BIN_DIR/vhs"
  TMP="$(mktemp -d)"
  curl -fsSL "https://github.com/charmbracelet/vhs/releases/download/${VHS_TAG}/vhs_${VHS_VER}_Linux_${VHS_ARCH}.tar.gz" \
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

if ! command -v chromium >/dev/null 2>&1; then
  PW_CHROME="$(ls -d "${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}"/chromium-*/chrome-linux/chrome 2>/dev/null | tail -1 || true)"
  if [ -n "$PW_CHROME" ] && [ -x "$PW_CHROME" ]; then
    echo "==> Symlinking $PW_CHROME → $BIN_DIR/chromium (for VHS/go-rod)"
    ln -sf "$PW_CHROME" "$BIN_DIR/chromium"
  fi
fi

echo "==> Versions"
showtape --version
vhs --version
ttyd --version | head -1
ffmpeg -version | head -1
echo "==> Setup complete."
