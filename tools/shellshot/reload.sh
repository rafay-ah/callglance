#!/bin/bash
# Restart the headless session (picks up extension changes): reload.sh [args for start.sh]
HERE=$(cd "$(dirname "$0")" && pwd)
pkill -u cg 2>/dev/null
sleep 1
rm -f /tmp/cg-session/ready
(setsid "$HERE/start.sh" "$@" > /tmp/cg-session.log 2>&1 &)
sleep 2
for _ in $(seq 1 60); do [ -f /tmp/cg-session/ready ] && break; sleep 1; done
sleep "${CG_SETTLE:-8}"
