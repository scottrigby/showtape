#!/usr/bin/env bash
# Verify that every place that pins a demotape version agrees.
# Usage:
#   scripts/check-version-sync.sh                # asserts all pins agree
#   scripts/check-version-sync.sh 0.3.1          # asserts they all equal that value
#
# Files inspected: pyproject.toml, feature/demotape/devcontainer-feature.json,
# .devcontainer/devcontainer.json, README.md.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXPECTED="${1:-}"

PY=$(grep -E '^version = ' "$ROOT/pyproject.toml" | head -1 | cut -d'"' -f2)
FEAT=$(jq -r '.version' "$ROOT/feature/demotape/devcontainer-feature.json")

OCI_DEV=$(grep -oE 'demotape/demotape:[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?' \
  "$ROOT/.devcontainer/devcontainer.json" | head -1 | cut -d: -f2 || true)

OCI_README=$(grep -oE 'demotape/demotape:[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?' \
  "$ROOT/README.md" | head -1 | cut -d: -f2 || true)

# Print the audit table
printf "%-55s %s\n" "pyproject.toml [project].version"                   "$PY"
printf "%-55s %s\n" "feature/demotape/devcontainer-feature.json version" "$FEAT"
printf "%-55s %s\n" ".devcontainer/devcontainer.json OCI ref"             "${OCI_DEV:-<absent>}"
printf "%-55s %s\n" "README.md first OCI ref"                             "${OCI_README:-<absent>}"

# Decide pass/fail
fail=0
values=("$PY" "$FEAT" "${OCI_DEV:-}" "${OCI_README:-}")

if [ -n "$EXPECTED" ]; then
  for v in "${values[@]}"; do
    [ -n "$v" ] && [ "$v" != "$EXPECTED" ] && { fail=1; }
  done
  if [ $fail -eq 0 ]; then
    echo "✅ all pins agree on $EXPECTED"
  else
    echo "❌ at least one pin is not $EXPECTED" >&2; exit 1
  fi
else
  for v in "${values[@]}"; do
    [ -n "$v" ] && [ "$v" != "$PY" ] && { fail=1; }
  done
  if [ $fail -eq 0 ]; then
    echo "✅ all pins agree on $PY"
  else
    echo "❌ pins disagree" >&2; exit 1
  fi
fi
