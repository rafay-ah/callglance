#!/bin/bash
# Build an architecture-independent .deb:  packaging/build-deb.sh [VERSION] [OUTDIR]
# shellcheck source=packaging/common.sh
source "$(dirname "$0")/common.sh"
VERSION=${1:-$(source_version)}
OUT=$(mkdir -p "${2:-$REPO/dist}" && cd "${2:-$REPO/dist}" && pwd)
ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT

stage_python "$ROOT/usr/share/callglance" "$VERSION"
stage_extension "$ROOT/usr/share/gnome-shell/extensions/$UUID" "$VERSION"
install -Dm644 "$REPO/data/$APP_ID.desktop" "$ROOT/usr/share/applications/$APP_ID.desktop"
install -Dm644 "$REPO/data/$APP_ID.autostart.desktop" "$ROOT/etc/xdg/autostart/$APP_ID.desktop"
install -Dm644 "$REPO/data/$APP_ID.svg" "$ROOT/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg"
install -Dm644 "$REPO/data/$APP_ID.metainfo.xml" "$ROOT/usr/share/metainfo/$APP_ID.metainfo.xml"

install -d "$ROOT/usr/bin"
cat > "$ROOT/usr/bin/callglance" <<'PY'
#!/usr/bin/python3
import sys

sys.path.insert(0, "/usr/share/callglance")
from callglance.cli import main  # noqa: E402

sys.exit(main())
PY
chmod 755 "$ROOT/usr/bin/callglance"

install -d "$ROOT/usr/share/doc/callglance"
{
  echo "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/"
  echo "Upstream-Name: callglance"
  echo "Source: https://github.com/rafay-ah/callglance"
  echo
  echo "Files: *"
  echo "Copyright: 2026 rafay-ah"
  echo "License: MIT"
  sed 's/^$/./; s/^/ /' "$REPO/LICENSE" | tail -n +3
} > "$ROOT/usr/share/doc/callglance/copyright"

install -d "$ROOT/DEBIAN"
SIZE=$(du -sk --exclude=DEBIAN "$ROOT" | cut -f1)
cat > "$ROOT/DEBIAN/control" <<CONTROL
Package: callglance
Version: $VERSION
Architecture: all
Maintainer: rafay-ah <54492363+rafay-ah@users.noreply.github.com>
Installed-Size: $SIZE
Depends: python3 (>= 3.10), python3-gi, gir1.2-glib-2.0
Recommends: gir1.2-gtk-3.0, python3-gi-cairo, gir1.2-ayatanaappindicator3-0.1, iw
Suggests: gnome-shell
Section: net
Priority: optional
Homepage: https://github.com/rafay-ah/callglance
Description: see at a glance whether your connection is good enough for calls
 CallGlance measures latency, jitter and packet loss to your router, your
 ISP and the internet, and says in plain words whether your connection is
 good for calls, and whether your Wi-Fi or your ISP is to blame.
 .
 It adds a coloured dot to the GNOME top bar (with live numbers and a
 one-hour graph), notifies you when quality drops below what Zoom, Meet
 and Teams need, offers a speed test with a bufferbloat grade and a desktop
 widget. Other desktops get a tray icon. No root needed.
CONTROL
echo "/etc/xdg/autostart/$APP_ID.desktop" > "$ROOT/DEBIAN/conffiles"
cat > "$ROOT/DEBIAN/postinst" <<'SH'
#!/bin/sh
set -e
# Compile the Python files once, so nothing has to write next to them later.
if [ "$1" = "configure" ] && command -v py3compile >/dev/null 2>&1; then
  py3compile -q /usr/share/callglance || true
elif [ "$1" = "configure" ]; then
  python3 -m compileall -q /usr/share/callglance >/dev/null 2>&1 || true
fi
SH
cat > "$ROOT/DEBIAN/prerm" <<'SH'
#!/bin/sh
set -e
find /usr/share/callglance -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
SH
chmod 755 "$ROOT/DEBIAN/postinst" "$ROOT/DEBIAN/prerm"

find "$ROOT" -type d -exec chmod 755 {} +
DEB="$OUT/callglance_${VERSION}_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$ROOT" "$DEB" >/dev/null
echo "$DEB"
