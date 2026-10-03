#!/bin/bash
# Record docs/demo.gif: the real extension in a headless GNOME Shell (Ubuntu
# session, dark style), with CallGlance in demo mode playing its story:
# all good, then the Wi-Fi breaks down (dot, widget and a notification react),
# the popover opens on the details, the Wi-Fi recovers, then the ISP drops packets.
#   sudo tools/shellshot/record-demo.sh [OUTDIR]
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
OUT=${1:-$REPO/docs}
WALLPAPER=${CG_WALLPAPER:-/usr/share/backgrounds/Fuji_san_by_amaral.png}
DURATION=${CG_DURATION:-96}  # seconds of story to record
OPEN_AT=${CG_OPEN_AT:-36}     # when to open the popover (after the notification)
S=$HERE/shell.sh
UUID=callglance@rafay-ah.github.io
INDICATOR="cgMain.panel.statusArea['$UUID']"

# The story turns bad 40 s in; start 10 s before that.
CG_DEMO_OFFSET=24 CG_SETTLE=4 "$HERE/reload.sh" 1280x800 prefer-dark "$WALLPAPER"
# Hide the overview and the headless session's own notices ("Screen Lock disabled").
"$S" eval "cgMain.overview.hide(); cgMain.messageTray.getSources().forEach(s => s.destroy()); 1" \
  >/dev/null
"$S" run gdbus call --session -d io.github.rafay_ah.CallGlance -o /io/github/rafay_ah/CallGlance \
  -m io.github.rafay_ah.CallGlance1.SetSetting widget \
  '{"enabled": true, "on_top": true, "x": 432, "y": 470}' >/dev/null
sleep 2

TMP=$(mktemp -d)
START=$(date +%s)
i=0
while :; do
  ELAPSED=$(( $(date +%s) - START ))
  [ "$ELAPSED" -ge "$DURATION" ] && break
  if [ "$ELAPSED" -ge "$OPEN_AT" ]; then
    # Open the popover (once), and keep it open.
    "$S" eval "if (!$INDICATOR.menu.isOpen) $INDICATOR.menu.open(); 1" >/dev/null
  fi
  if [ "$ELAPSED" -ge $((OPEN_AT + 3)) ] && [ "$ELAPSED" -le $((OPEN_AT + 6)) ]; then
    # GNOME keeps banners up while the user is idle; a little pointer movement
    # (away from the popover) lets the notification time out as it normally would.
    "$S" eval "cgPointer.move(120 + $ELAPSED, 400); 1" >/dev/null
  fi
  i=$((i + 1))
  "$S" shot "$(printf '%s/f%03d.png' "$TMP" "$i")" 380 0 900 700
  sleep 1.2
done

mkdir -p "$OUT"
ffmpeg -loglevel error -y -framerate 4 -i "$TMP/f%03d.png" -vf \
  "split[a][b];[a]palettegen=stats_mode=diff:max_colors=160[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" \
  "$OUT/demo.gif"
ls -la "$OUT/demo.gif"
echo "$i frames kept in $TMP"
