"""Desktop integration for the AppImage: an app-grid launcher and icon in ~/.local.

Packages install these system-wide; an AppImage is a single file, so on first
run it registers itself for this user (and fixes the path if the file moved).
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from callglance import APP_ID

ICON = Path(__file__).resolve().parent / "data" / "icons" / f"{APP_ID}.svg"


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"))


def ensure_appimage_integration() -> bool:
    appimage = os.environ.get("APPIMAGE")
    if not appimage:
        return False
    data = _data_home()
    icon_dir = data / "icons" / "hicolor" / "scalable" / "apps"
    icon_dir.mkdir(parents=True, exist_ok=True)
    if ICON.exists():
        (icon_dir / ICON.name).write_bytes(ICON.read_bytes())
    desktop = data / "applications" / f"{APP_ID}.desktop"
    desktop.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=CallGlance",
        "GenericName=Call Quality Monitor",
        "Comment=See at a glance whether your connection is good enough for calls",
        f"Exec={shlex.quote(appimage)}",
        f"Icon={APP_ID}",
        "Terminal=false",
        "Categories=Network;Monitor;Utility;",
        "Keywords=network;latency;jitter;packet loss;ping;wifi;zoom;meet;teams;speed test;",
        "StartupNotify=false",
        "X-AppImage-Integrated=true",
        "",
    ])
    if not desktop.exists() or desktop.read_text() != content:
        desktop.write_text(content)
    return True
