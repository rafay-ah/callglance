#!/bin/bash
# Zip the GNOME Shell extension for `gnome-extensions install`:
#   packaging/build-extension-zip.sh [VERSION] [OUTDIR]
# shellcheck source=packaging/common.sh
source "$(dirname "$0")/common.sh"
VERSION=${1:-$(source_version)}
OUT=$(mkdir -p "${2:-$REPO/dist}" && cd "${2:-$REPO/dist}" && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
stage_extension "$TMP/$UUID" "$VERSION"
ZIP="$OUT/$UUID.shell-extension.zip"
rm -f "$ZIP"
(cd "$TMP/$UUID" && zip -qr "$ZIP" .)
echo "$ZIP"
