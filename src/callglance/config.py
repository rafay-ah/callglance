"""User settings, stored as JSON in ``$XDG_CONFIG_HOME/callglance/config.json``."""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    # Probing
    "interval": 2.0,  # seconds between bursts, per target
    "train_length": 5,  # probes per burst (the first one wakes the radio up)
    "train_spacing_ms": 20,  # one voice packet every 20 ms, like Opus/G.711
    "probe_timeout": 1.0,
    "window": 30,  # seconds of data behind the live verdict
    "public_targets": ["1.1.1.1", "8.8.8.8"],
    "dns_names": ["zoom.us", "meet.google.com", "teams.microsoft.com"],
    "dns_interval": 15,
    # Thresholds (see verdict.Thresholds); missing keys use the built-in defaults
    "thresholds": {},
    # Notifications
    "notifications": True,
    "notify_recovery": True,
    "notify_after": 20,  # seconds a problem must last before we tell you
    "notify_cooldown": 600,  # seconds between two "quality dropped" notifications
    # Desktop widget (shared by the GNOME Shell extension and the GTK fallback)
    "widget": {"enabled": False, "on_top": False, "x": -1, "y": -1, "monitor": 0},
    # Panel
    "panel_show_latency": False,
    # Housekeeping
    "history_hours": 24,
    "extension_auto_enabled": False,  # we enabled the GNOME Shell extension once
}

# Keys the UI may change over D-Bus, with their expected types.
UI_WRITABLE: dict[str, type | tuple[type, ...]] = {
    "notifications": bool,
    "notify_recovery": bool,
    "panel_show_latency": bool,
    "widget": dict,
    "autostart": bool,  # virtual: stored in ~/.config/autostart, not here
}


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "callglance"


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "callglance"


def _merge(defaults: dict, data: dict) -> dict:
    out = copy.deepcopy(defaults)
    for key, value in data.items():
        if isinstance(out.get(key), dict) and isinstance(value, dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config_dir() / "config.json"
        self._lock = threading.Lock()
        self.data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.first_run = not self.path.exists()
        self.load()

    def load(self) -> None:
        try:
            with open(self.path) as fh:
                stored = json.load(fh)
            if isinstance(stored, dict):
                self.data = _merge(DEFAULTS, stored)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable config %s: %s", self.path, exc)

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".config-", suffix=".json")
            try:
                with os.fdopen(fd, "w") as fh:
                    json.dump(self.data, fh, indent=2, sort_keys=True)
                    fh.write("\n")
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    def __getitem__(self, key: str) -> Any:
        return self.data.get(key, DEFAULTS.get(key))

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any, save: bool = True) -> None:
        if isinstance(self.data.get(key), dict) and isinstance(value, dict):
            merged = dict(self.data[key])
            merged.update(value)
            value = merged
        self.data[key] = value
        if save:
            self.save()
