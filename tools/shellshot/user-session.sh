#!/bin/bash
# As the unprivileged user: a private "system" bus, then a session bus with the shell.
SYS=$(mktemp -d)
dbus-daemon --config-file=/usr/share/dbus-1/session.conf --address="unix:path=$SYS/bus" \
  --fork --nopidfile >/dev/null
export DBUS_SYSTEM_BUS_ADDRESS="unix:path=$SYS/bus"
exec dbus-run-session -- bash "$(dirname "$0")/run-session.sh" "$1"
