#!/bin/bash
# Talk to the headless shell started by start.sh:
#   shell.sh eval 'JS'            evaluate JavaScript in the shell
#   shell.sh shot FILE [x y w h]  save a screenshot (whole screen, or an area)
#   shell.sh run CMD...           run a command inside the session (as user cg)
STATE=/tmp/cg-session
source "$STATE/bus.env"
as_cg() { setpriv --reuid=cg --regid=cg --init-groups env DBUS_SESSION_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS" HOME=/home/cg "$@"; }
case "$1" in
  eval) as_cg gdbus call --session -d test.CallGlance.Shot -o /test/CallGlance/Shot -m test.CallGlance.Shot.Eval "$2" ;;
  shot)
    tmp="$STATE/shot-$$.png"
    as_cg gdbus call --session -d test.CallGlance.Shot -o /test/CallGlance/Shot \
      -m test.CallGlance.Shot.Screenshot "$tmp" ${3:-0} ${4:-0} ${5:-0} ${6:-0} >/dev/null \
      && mv "$tmp" "$2" ;;
  run) shift; as_cg "$@" ;;
esac
