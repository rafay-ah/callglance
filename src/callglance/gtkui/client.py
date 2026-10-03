"""D-Bus client for the CallGlance service, for the GTK fallback interface."""

from __future__ import annotations

import json
import logging
import time

from gi.repository import Gio, GLib, GObject

from callglance import APP_ID, DBUS_IFACE, DBUS_PATH

log = logging.getLogger(__name__)
HOUR = 3600


class Client(GObject.Object):
    __gsignals__ = {
        "snapshot": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "settings": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "history": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "show-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "panel-changed": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        "vanished": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__()
        self.snapshot: dict | None = None
        self.settings: dict | None = None
        self.history: list[dict] = []
        self.panel_registered = False
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._history_pending = False
        self._subs = [
            self.bus.signal_subscribe(APP_ID, DBUS_IFACE, None, DBUS_PATH, None,
                                      Gio.DBusSignalFlags.NONE, self._on_signal),
            self.bus.signal_subscribe(APP_ID, "org.freedesktop.DBus.Properties",
                                      "PropertiesChanged", DBUS_PATH, DBUS_IFACE,
                                      Gio.DBusSignalFlags.NONE, self._on_properties),
        ]
        self._watch = Gio.bus_watch_name_on_connection(
            self.bus, APP_ID, Gio.BusNameWatcherFlags.NONE, None, self._on_vanished)
        self._call("GetSettings", None, lambda r: self._set_settings(r[0]))
        self._call("GetSnapshot", None, lambda r: self._set_snapshot(r[0]))
        self._get_panel_property()
        self.refresh_history()

    # -- plumbing ---------------------------------------------------------------------
    def _call(self, method: str, params: GLib.Variant | None, done=None) -> None:
        def finish(bus, result):
            try:
                reply = bus.call_finish(result)
            except GLib.Error as exc:
                log.debug("%s failed: %s", method, exc.message)
                return
            if done is not None:
                done(reply.unpack())

        self.bus.call(APP_ID, DBUS_PATH, DBUS_IFACE, method, params, None,
                      Gio.DBusCallFlags.NO_AUTO_START, 10000, None, finish)

    def _get_panel_property(self) -> None:
        def finish(bus, result):
            try:
                value = bus.call_finish(result).unpack()[0]
            except GLib.Error:
                return
            self._set_panel(bool(value))

        self.bus.call(APP_ID, DBUS_PATH, "org.freedesktop.DBus.Properties", "Get",
                      GLib.Variant("(ss)", (DBUS_IFACE, "PanelRegistered")), None,
                      Gio.DBusCallFlags.NO_AUTO_START, 5000, None, finish)

    def _on_signal(self, _bus, _sender, _path, _iface, name, params) -> None:
        if name == "SnapshotChanged":
            self._set_snapshot(params.unpack()[0])
        elif name == "SettingsChanged":
            self._set_settings(params.unpack()[0])
        elif name == "ShowRequested":
            self.emit("show-requested")

    def _on_properties(self, _bus, _sender, _path, _iface, _name, params) -> None:
        _iface_name, changed, _invalid = params.unpack()
        if "PanelRegistered" in changed:
            self._set_panel(bool(changed["PanelRegistered"]))

    def _on_vanished(self, _bus, _name) -> None:
        self.emit("vanished")

    def _set_panel(self, value: bool) -> None:
        if value != self.panel_registered:
            self.panel_registered = value
            self.emit("panel-changed", value)

    def _set_snapshot(self, text: str) -> None:
        try:
            snap = json.loads(text)
        except ValueError:
            return
        if snap.get("level"):
            self.snapshot = snap
            self.emit("snapshot", snap)

    def _set_settings(self, text: str) -> None:
        try:
            self.settings = json.loads(text)
        except ValueError:
            return
        self.emit("settings", self.settings)

    # -- API ------------------------------------------------------------------------------
    def refresh_history(self) -> None:
        if self._history_pending:
            return
        last = self.history[-1]["ts"] if self.history else 0
        span = min(HOUR, int(time.time() - last) + 30) if last else HOUR
        self._history_pending = True

        def done(result) -> None:
            self._history_pending = False
            try:
                data = json.loads(result[0])
            except ValueError:
                return
            fields = data.get("fields") or []
            rows = [dict(zip(fields, row)) for row in data.get("rows", [])]
            known = {p["ts"] for p in self.history}
            merged = self.history + [p for p in rows if p["ts"] not in known]
            merged.sort(key=lambda p: p["ts"])
            horizon = time.time() - HOUR
            self.history = [p for p in merged if p["ts"] >= horizon]
            self.emit("history")

        self._call("GetHistory", GLib.Variant("(u)", (max(60, span),)), done)
        GLib.timeout_add_seconds(15, self._clear_pending)

    def _clear_pending(self) -> bool:
        self._history_pending = False
        return GLib.SOURCE_REMOVE

    def set_setting(self, key: str, value) -> None:
        self._call("SetSetting", GLib.Variant("(ss)", (key, json.dumps(value))))

    def run_speedtest(self) -> None:
        self._call("RunSpeedTest", None)

    def cancel_speedtest(self) -> None:
        self._call("CancelSpeedTest", None)

    def quit_service(self) -> None:
        self._call("Quit", None)
