#!/bin/bash
# Runs inside dbus-run-session as an unprivileged user: start the shell and
# CallGlance (demo mode), record the bus address for other scripts, then idle.
REPO=$(cd "$(dirname "$0")/../.." && pwd)
STATE=${CG_STATE:-/tmp/cg-session}
mkdir -p "$STATE"
bash "$REPO/tools/shellshot/session.sh" "${1:-1280x800}" > /dev/null
echo "export DBUS_SESSION_BUS_ADDRESS='$DBUS_SESSION_BUS_ADDRESS'" > "$STATE/bus.env"
export XDG_CONFIG_HOME=$HOME/.config XDG_DATA_HOME=$HOME/.local/share XDG_STATE_HOME=$HOME/.local/state
export GSETTINGS_BACKEND=keyfile XDG_CURRENT_DESKTOP=ubuntu:GNOME
for _ in $(seq 1 60); do
  gdbus introspect --session -d test.CallGlance.Shot -o /test/CallGlance/Shot >/dev/null 2>&1 && break
  sleep 1
done
gdbus call --session -d test.CallGlance.Shot -o /test/CallGlance/Shot -m test.CallGlance.Shot.Eval "cgMain.overview.hide()" >/dev/null
# A real session starts the notification service at login; this one has to ask.
gdbus call --session -d org.freedesktop.DBus -o /org/freedesktop/DBus \
  -m org.freedesktop.DBus.StartServiceByName org.gnome.Shell.Notifications 0 >/dev/null || true
if [ -z "$CG_NO_APP" ]; then
  CALLGLANCE_DEMO_OFFSET="${CG_DEMO_OFFSET:-0}" PYTHONPATH="$REPO/src" \
    python3.12 -m callglance --demo -v > "$STATE/app.log" 2>&1 &
fi
echo ready > "$STATE/ready"
sleep infinity
