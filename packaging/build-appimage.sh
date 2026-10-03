#!/bin/bash
# Build an AppImage:  packaging/build-appimage.sh [VERSION] [OUTDIR] [ARCH]
#
# CallGlance is pure Python on top of the system's PyGObject (python3-gi), which
# every GNOME desktop already has. The AppImage therefore carries only our code
# and runs it with the host's python3, which keeps it tiny and well integrated.
# shellcheck source=packaging/common.sh
source "$(dirname "$0")/common.sh"
VERSION=${1:-$(source_version)}
OUT=$(mkdir -p "${2:-$REPO/dist}" && cd "${2:-$REPO/dist}" && pwd)
ARCH=${3:-x86_64}
TOOLS=${APPIMAGE_TOOLS:-$REPO/.cache/appimage}
APPDIR=$(mktemp -d)/CallGlance.AppDir
trap 'rm -rf "$(dirname "$APPDIR")"' EXIT

stage_python "$APPDIR/usr/share/callglance" "$VERSION" with-extension
install -Dm644 "$REPO/data/$APP_ID.svg" "$APPDIR/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg"
install -Dm644 "$REPO/data/$APP_ID.metainfo.xml" "$APPDIR/usr/share/metainfo/$APP_ID.appdata.xml"
cp "$REPO/data/$APP_ID.svg" "$APPDIR/$APP_ID.svg"
ln -s "$APP_ID.svg" "$APPDIR/.DirIcon"
sed 's/^Exec=callglance$/Exec=AppRun/' "$REPO/data/$APP_ID.desktop" > "$APPDIR/$APP_ID.desktop"
install -Dm644 "$APPDIR/$APP_ID.desktop" "$APPDIR/usr/share/applications/$APP_ID.desktop"

cat > "$APPDIR/AppRun" <<'SH'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
export PYTHONPATH="$HERE/usr/share/callglance${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
PYTHON=$(command -v python3 || true)
if [ -z "$PYTHON" ] || ! "$PYTHON" -c 'import gi' 2>/dev/null; then
  MSG="CallGlance needs Python 3 with PyGObject. On Ubuntu or Debian: sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1"
  command -v notify-send >/dev/null 2>&1 && notify-send -i dialog-warning CallGlance "$MSG"
  echo "$MSG" >&2
  exit 1
fi
exec "$PYTHON" -m callglance "$@"
SH
chmod 755 "$APPDIR/AppRun"

mkdir -p "$TOOLS"
TOOL="$TOOLS/appimagetool-x86_64.AppImage"
RUNTIME="$TOOLS/runtime-$ARCH"
[ -x "$TOOL" ] || { curl -fsSL -o "$TOOL" \
  https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage \
  && chmod +x "$TOOL"; }
[ -s "$RUNTIME" ] || curl -fsSL -o "$RUNTIME" \
  "https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-$ARCH"

TARGET="$OUT/CallGlance-$VERSION-$ARCH.AppImage"
ARCH=$ARCH APPIMAGE_EXTRACT_AND_RUN=1 "$TOOL" --no-appstream --runtime-file "$RUNTIME" \
  "$APPDIR" "$TARGET" >/dev/null 2>&1 || {
  echo "appimagetool failed; rerunning verbosely" >&2
  ARCH=$ARCH APPIMAGE_EXTRACT_AND_RUN=1 "$TOOL" --no-appstream --runtime-file "$RUNTIME" \
    "$APPDIR" "$TARGET"
}
chmod 755 "$TARGET"
echo "$TARGET"
