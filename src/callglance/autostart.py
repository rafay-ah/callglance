"""Start-at-login via XDG autostart (works on GNOME, KDE, XFCE, Cinnamon, ...).

Packages may install a system-wide entry in ``/etc/xdg/autostart`` so CallGlance
starts on login by default. Turning it off writes a per-user override with
``Hidden=true``, which is how the XDG Autostart spec disables a system entry
for one user. Without a system entry, the per-user file is created or removed.
"""

from __future__ import annotations

import os
import shlex
import sys
from configparser import ConfigParser
from pathlib import Path

from callglance import APP_ID, APP_NAME

FILENAME = f"{APP_ID}.desktop"


def user_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "autostart" / FILENAME


def system_paths() -> list[Path]:
    dirs = os.environ.get("XDG_CONFIG_DIRS") or "/etc/xdg"
    return [Path(d) / "autostart" / FILENAME for d in dirs.split(":") if d]


def _system_entry() -> Path | None:
    return next((p for p in system_paths() if p.exists()), None)


def _read(path: Path) -> dict[str, str]:
    parser = ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # keep key case
    try:
        parser.read(path, encoding="utf-8")
    except Exception:
        return {}
    return dict(parser["Desktop Entry"]) if parser.has_section("Desktop Entry") else {}


def _entry_enabled(values: dict[str, str]) -> bool:
    if values.get("Hidden", "").lower() == "true":
        return False
    return values.get("X-GNOME-Autostart-enabled", "true").lower() != "false"


def is_enabled() -> bool:
    user = user_path()
    if user.exists():
        return _entry_enabled(_read(user))
    system = _system_entry()
    return system is not None and _entry_enabled(_read(system))


def launch_command() -> str:
    """How to start this copy of CallGlance from a desktop file."""
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        return f"{shlex.quote(appimage)} --background"
    argv0 = os.path.realpath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if os.path.basename(argv0) == "callglance" and os.access(argv0, os.X_OK):
        return f"{shlex.quote(argv0)} --background"
    return f"{shlex.quote(sys.executable)} -m callglance --background"


def _write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def set_enabled(enabled: bool) -> None:
    user = user_path()
    system = _system_entry()
    if enabled:
        if system is not None and _entry_enabled(_read(system)):
            # The packaged entry already starts us; just drop any per-user override.
            user.unlink(missing_ok=True)
            return
        _write(user, [
            "[Desktop Entry]",
            "Type=Application",
            f"Name={APP_NAME}",
            "Comment=Connection quality for calls, at a glance",
            f"Exec={launch_command()}",
            f"Icon={APP_ID}",
            "Terminal=false",
            "NoDisplay=true",
            "X-GNOME-Autostart-enabled=true",
            "X-GNOME-Autostart-Delay=3",
        ])
    elif system is not None:
        _write(user, [
            "[Desktop Entry]",
            "Type=Application",
            f"Name={APP_NAME}",
            "Hidden=true",
        ])
    else:
        user.unlink(missing_ok=True)
