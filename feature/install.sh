#!/usr/bin/env bash
#
# Showtape devcontainer feature install script.
# Runs as root during image build, AFTER the dependsOn features (python,
# ffmpeg, apt-packages) have already populated their pieces.
#
# Installs: ttyd + VHS binaries, the `showtape` Python package, Playwright's
# Chromium, and (optionally) a pre-fetched Piper voice model.

set -euo pipefail

# ---- Inputs (from feature options) ----
VERSION="${VERSION:-main}"
VOICEMODEL="${VOICEMODEL:-en_US-libritts_r-medium}"
INSTALLCHROMIUM="${INSTALLCHROMIUM:-true}"

# ---- Resolved environment ----
ARCH="$(uname -m)"
case "$ARCH" in
  aarch64|arm64) VHS_ARCH="arm64";  TTYD_ARCH="aarch64" ;;
  x86_64)        VHS_ARCH="x86_64"; TTYD_ARCH="x86_64"  ;;
  *) echo "showtape: unsupported architecture $ARCH" >&2; exit 1 ;;
esac

# Python feature installs python at /usr/local/python/current; its bin dir
# is on PATH for the user. We drop binaries there too so they're discovered.
PYTHON_BIN="/usr/local/python/current/bin"
SHARE_DIR="/usr/local/share/showtape"
VOICES_DIR="${SHARE_DIR}/voices"

mkdir -p "$SHARE_DIR" "$VOICES_DIR"

# ---- VHS (terminal pane renderer) ----
if ! command -v vhs >/dev/null 2>&1; then
  VHS_TAG="$(curl -fsSL https://api.github.com/repos/charmbracelet/vhs/releases/latest \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])')"
  VHS_VER="${VHS_TAG#v}"
  echo "showtape: installing VHS ${VHS_TAG} (${VHS_ARCH}) → ${PYTHON_BIN}/vhs"
  TMP="$(mktemp -d)"
  curl -fsSL "https://github.com/charmbracelet/vhs/releases/download/${VHS_TAG}/vhs_${VHS_VER}_Linux_${VHS_ARCH}.tar.gz" \
    | tar xz -C "$TMP"
  install -m0755 "$TMP"/vhs*/vhs "${PYTHON_BIN}/vhs"
  rm -rf "$TMP"
fi

# ---- ttyd (used internally by VHS) ----
if ! command -v ttyd >/dev/null 2>&1; then
  echo "showtape: installing ttyd (${TTYD_ARCH}) → ${PYTHON_BIN}/ttyd"
  curl -fsSL "https://github.com/tsl0922/ttyd/releases/latest/download/ttyd.${TTYD_ARCH}" \
    -o "${PYTHON_BIN}/ttyd"
  chmod +x "${PYTHON_BIN}/ttyd"
fi

# ---- showtape Python package ----
echo "showtape: installing showtape Python package (ref=${VERSION})"
pip install --no-cache-dir "git+https://github.com/scottrigby/showtape@${VERSION}"

# ---- Playwright Chromium ----
if [ "${INSTALLCHROMIUM}" = "true" ]; then
  echo "showtape: installing Playwright Chromium"
  # We're root here, so --with-deps would re-install apt deps already covered
  # by the apt-packages feature dependency. Skip --with-deps; trust the deps.
  playwright install chromium

  # VHS uses go-rod, which searches PATH for "chromium". Symlink Playwright's
  # Chromium so VHS can find it without a duplicate download.
  PW_CHROME="$(ls -d /root/.cache/ms-playwright/chromium-*/chrome-linux/chrome 2>/dev/null \
    | tail -1 || true)"
  if [ -z "$PW_CHROME" ]; then
    PW_CHROME="$(ls -d "${PLAYWRIGHT_BROWSERS_PATH:-}"/chromium-*/chrome-linux/chrome 2>/dev/null \
      | tail -1 || true)"
  fi
  if [ -n "$PW_CHROME" ] && [ -x "$PW_CHROME" ]; then
    ln -sf "$PW_CHROME" "${PYTHON_BIN}/chromium"
    echo "showtape: symlinked ${PW_CHROME} → ${PYTHON_BIN}/chromium (for VHS/go-rod)"
  else
    echo "showtape: WARN — Playwright Chromium not found post-install; VHS may fail at runtime" >&2
  fi
fi

# ---- Pre-fetch voice model ----
if [ -n "${VOICEMODEL}" ]; then
  echo "showtape: pre-fetching voice model ${VOICEMODEL} → ${VOICES_DIR}"
  showtape fetch-voice "${VOICEMODEL}" --dir "${VOICES_DIR}" || \
    echo "showtape: WARN — voice pre-fetch failed; users can fetch later with \`showtape fetch-voice\`" >&2
fi

echo "showtape: install complete."
showtape --version
