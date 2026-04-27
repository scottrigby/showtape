#!/usr/bin/env bash
# Bump showtape's version in the two places that need an explicit string:
#   - pyproject.toml ([project].version)
#   - feature/showtape/devcontainer-feature.json ("version")
#
# src/showtape/__init__.py reads via importlib.metadata, so no edit needed there.
#
# Usage:  scripts/bump-version.sh 0.3.0
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: $0 <new-version>   e.g. 0.3.0" >&2
  exit 2
fi
NEW="$1"

# Validate semver-ish (digits, dots, optional pre-release suffix)
if ! [[ "$NEW" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]]; then
  echo "error: '$NEW' doesn't look like a semver version" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# pyproject.toml [project].version (only the first/canonical one)
python3 - "$NEW" <<'PY'
import re, sys
new = sys.argv[1]
p = "pyproject.toml"
text = open(p).read()
text, n = re.subn(r'^version = "[^"]*"', f'version = "{new}"', text, count=1, flags=re.MULTILINE)
assert n == 1, f"failed to replace version in {p}"
open(p, "w").write(text)
print(f"bumped {p} → {new}")
PY

# devcontainer-feature.json "version"
python3 - "$NEW" <<'PY'
import json, sys
new = sys.argv[1]
p = "feature/showtape/devcontainer-feature.json"
data = json.load(open(p))
data["version"] = new
open(p, "w").write(json.dumps(data, indent=2) + "\n")
print(f"bumped {p} → {new}")
PY

cat <<EOF

Next:
  git diff
  git commit -am "Bump to v$NEW"
  git push origin main      # CI tags v$NEW + publishes OCI feature + creates GitHub Release
EOF
