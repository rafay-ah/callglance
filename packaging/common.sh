# shellcheck shell=bash
# Shared helpers for the package build scripts (sourced, not run).
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
APP_ID=io.github.rafay_ah.CallGlance
UUID=callglance@rafay-ah.github.io
export REPO APP_ID UUID

source_version() {
  python3 -c "import re,sys; print(re.search(r'__version__ = \"([^\"]+)\"', open(sys.argv[1]).read()).group(1))" \
    "$REPO/src/callglance/__init__.py"
}

# stage_python DEST VERSION [with-extension]: copy the package, stamped with VERSION.
stage_python() {
  local dest=$1 version=$2
  mkdir -p "$dest"
  cp -r "$REPO/src/callglance" "$dest/"
  find "$dest" -name __pycache__ -prune -exec rm -rf {} +
  sed -i "s/^__version__ = .*/__version__ = \"$version\"/" "$dest/callglance/__init__.py"
  if [ "${3:-}" = "with-extension" ]; then
    mkdir -p "$dest/callglance/data/gnome-shell"
    stage_extension "$dest/callglance/data/gnome-shell/$UUID" "$version"
  fi
}

# stage_extension DEST VERSION: copy the GNOME Shell extension, stamped with VERSION.
stage_extension() {
  local dest=$1 version=$2
  mkdir -p "$dest"
  cp "$REPO/gnome-shell/$UUID/"*.js "$REPO/gnome-shell/$UUID/"*.css \
     "$REPO/gnome-shell/$UUID/metadata.json" "$dest/"
  python3 - "$dest/metadata.json" "$version" <<'PY'
import json, sys
path, version = sys.argv[1], sys.argv[2]
meta = json.load(open(path))
meta["version-name"] = version
# An integer that grows with every release, so newer copies replace older ones.
major, minor, patch = (int(x) for x in (version.split("-")[0].split(".") + ["0", "0"])[:3])
meta["version"] = major * 10000 + minor * 100 + patch
json.dump(meta, open(path, "w"), indent=2)
open(path, "a").write("\n")
PY
}
