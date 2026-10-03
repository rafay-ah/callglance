"""Install and enable the bundled GNOME Shell extension (the top-panel indicator).

The .deb installs the extension system-wide. The AppImage and source checkouts
carry a copy that is installed into ``~/.local/share/gnome-shell/extensions``.
GNOME Shell only scans for new extensions when it starts, so a freshly
installed extension appears after the next log-in; until then the tray-icon
fallback is shown.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

UUID = "callglance@rafay-ah.github.io"
SHELL_SCHEMA = "org.gnome.shell"

# org.gnome.Shell.Extensions state values
STATES = {1: "active", 2: "inactive", 3: "error", 4: "out-of-date", 5: "downloading",
          6: "initialized", 7: "disabling", 8: "enabling", 99: "uninstalled"}


def is_gnome_session() -> bool:
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
    return any(part.upper() == "GNOME" for part in desktop.split(":"))


def bundled_source() -> Path | None:
    """The extension shipped inside this copy of CallGlance, if any."""
    here = Path(__file__).resolve().parent
    candidates = [
        here / "data" / "gnome-shell" / UUID,
        here.parents[1] / "gnome-shell" / UUID,  # source checkout: <repo>/gnome-shell/<uuid>
    ]
    return next((c for c in candidates if (c / "metadata.json").exists()), None)


def user_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "gnome-shell" / "extensions" / UUID


def system_dirs() -> list[Path]:
    dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    return [Path(d) / "gnome-shell" / "extensions" / UUID for d in dirs.split(":") if d]


def installed_dir() -> Path | None:
    for path in [user_dir(), *system_dirs()]:
        if (path / "metadata.json").exists():
            return path
    return None


def _version(path: Path) -> int:
    try:
        return int(json.loads((path / "metadata.json").read_text()).get("version", 0))
    except (OSError, ValueError):
        return 0


def install_user_copy(force: bool = False) -> Path | None:
    """Copy the bundled extension into the user's extension directory when needed.

    Skipped when a system-wide copy (from the .deb) is at least as new.
    """
    source = bundled_source()
    if source is None:
        return installed_dir()
    target = user_dir()
    system = next((p for p in system_dirs() if (p / "metadata.json").exists()), None)
    if not force and system is not None and _version(system) >= _version(source) \
            and not target.exists():
        return system
    if not force and target.exists() and _version(target) >= _version(source):
        return target
    tmp = target.with_name(target.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(source, tmp, ignore=shutil.ignore_patterns("*.pyc", "__pycache__"))
    shutil.rmtree(target, ignore_errors=True)
    tmp.rename(target)
    log.info("installed GNOME Shell extension to %s", target)
    return target


def _settings():
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio

        source = Gio.SettingsSchemaSource.get_default()
        if source is None or source.lookup(SHELL_SCHEMA, True) is None:
            return None
        return Gio.Settings.new(SHELL_SCHEMA)
    except Exception:
        return None


def is_enabled_in_settings() -> bool | None:
    settings = _settings()
    if settings is None:
        return None
    return UUID in settings.get_strv("enabled-extensions") and not settings.get_boolean(
        "disable-user-extensions")


def enable_in_settings() -> bool:
    """Add the extension to org.gnome.shell enabled-extensions (takes effect at once if
    GNOME Shell already knows the extension, otherwise after the next log-in)."""
    settings = _settings()
    if settings is None:
        return False
    enabled = settings.get_strv("enabled-extensions")
    if UUID not in enabled:
        settings.set_strv("enabled-extensions", [*enabled, UUID])
    disabled = settings.get_strv("disabled-extensions")
    if UUID in disabled:
        settings.set_strv("disabled-extensions", [u for u in disabled if u != UUID])
    try:
        from gi.repository import Gio

        Gio.Settings.sync()
    except Exception:
        pass
    return True


def shell_state() -> str | None:
    """What the running GNOME Shell thinks of the extension (None: shell not reachable,
    "unknown": the shell has not scanned it yet, i.e. a re-login is needed)."""
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(
            "org.gnome.Shell", "/org/gnome/Shell", "org.gnome.Shell.Extensions",
            "GetExtensionInfo", GLib.Variant("(s)", (UUID,)), None,
            Gio.DBusCallFlags.NONE, 2000, None,
        )
        info = reply.unpack()[0]
    except Exception:
        return None
    if not info:
        return "unknown"
    return STATES.get(int(info.get("state", 0)), str(info.get("state")))


def ensure_enabled_once(config) -> str:
    """First-run integration on GNOME: install (if needed) and enable the extension,
    exactly once, so a later "disable" by the user is respected."""
    if not is_gnome_session():
        return "not-gnome"
    try:
        install_user_copy()
    except OSError as exc:
        log.warning("could not install the GNOME Shell extension: %s", exc)
    if config.get("extension_auto_enabled"):
        return "already-handled"
    if enable_in_settings():
        config.set("extension_auto_enabled", True)
        return "enabled"
    return "no-settings"
