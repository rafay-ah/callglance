#!/bin/bash
# Start a throwaway headless GNOME Shell with CallGlance (demo mode) and the
# screenshot harness. Usage (inside dbus-run-session):  session.sh WIDTHxHEIGHT
set -e
REPO=$(cd "$(dirname "$0")/../.." && pwd)
SIZE=${1:-1280x800}
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-$(mktemp -d)}
chmod 700 "$XDG_RUNTIME_DIR"
export HOME=${CG_HOME:-$HOME}
export XDG_CONFIG_HOME=$HOME/.config XDG_DATA_HOME=$HOME/.local/share XDG_STATE_HOME=$HOME/.local/state
export GSETTINGS_BACKEND=keyfile
export XDG_CURRENT_DESKTOP=ubuntu:GNOME XDG_SESSION_TYPE=wayland
mkdir -p "$XDG_DATA_HOME/gnome-shell/extensions"
ln -sfn "$REPO/gnome-shell/callglance@rafay-ah.github.io" "$XDG_DATA_HOME/gnome-shell/extensions/"
ln -sfn "$REPO/tools/shellshot/cgshot@callglance.test" "$XDG_DATA_HOME/gnome-shell/extensions/"
gsettings set org.gnome.shell enabled-extensions "['callglance@rafay-ah.github.io', 'cgshot@callglance.test']"
gsettings set org.gnome.shell disable-extension-version-validation true
gsettings set org.gnome.shell welcome-dialog-last-shown-version '999'
gsettings set org.gnome.desktop.interface color-scheme "${CG_SCHEME:-default}"
gsettings set org.gnome.desktop.interface enable-animations true
[ -n "$CG_BACKGROUND" ] && gsettings set org.gnome.desktop.background picture-uri "file://$CG_BACKGROUND" && gsettings set org.gnome.desktop.background picture-uri-dark "file://$CG_BACKGROUND"
export LIBGL_ALWAYS_SOFTWARE=1 MESA_LOADER_DRIVER_OVERRIDE=llvmpipe
gnome-shell --headless --wayland --no-x11 --mode="${CG_MODE:-ubuntu}" --virtual-monitor "$SIZE" > "$HOME/shell.log" 2>&1 &
echo "$HOME"
