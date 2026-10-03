#!/bin/bash
# Start a throwaway headless GNOME Shell (Ubuntu session) for screenshots.
# Needs root (for a private mount namespace) and an unprivileged user "cg".
#   tools/shellshot/start.sh 1280x800 [default|prefer-dark] [wallpaper]
# Then drive it with tools/shellshot/shell.sh.
set -e
SIZE=${1:-1280x800}
STATE=/tmp/cg-session
HERE=$(cd "$(dirname "$0")" && pwd)
rm -rf "$STATE" /home/cg/.config /home/cg/.local /home/cg/shell.log
mkdir -p "$STATE"; chmod 777 "$STATE"
# No logind in a container: hide /run/systemd so the shell uses its dummy login manager.
exec unshare -m --propagation private bash -c '
  mount -t tmpfs none /run/systemd
  exec setpriv --reuid=cg --regid=cg --init-groups env -i PATH=/usr/local/bin:/usr/bin:/bin \
    HOME=/home/cg USER=cg LANG=C.UTF-8 CG_SCHEME="$1" CG_BACKGROUND="$2" CG_NO_APP="$3" \
    CG_DEMO_OFFSET="$7" \
    CG_STATE="$4" bash "$5/user-session.sh" "$6"
' _ "${2:-default}" "${3:-}" "${CG_NO_APP:-}" "$STATE" "$HERE" "$SIZE" "${CG_DEMO_OFFSET:-0}"
