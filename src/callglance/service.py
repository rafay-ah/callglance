"""The CallGlance background service: one per desktop session.

It runs the measurement engine and exposes the results on the session bus
(``io.github.rafay_ah.CallGlance1``) for the user interfaces:

* the GNOME Shell extension (top-panel dot, popover, desktop widget), and
* the GTK tray fallback, which the service starts by itself whenever no panel
  extension has registered (other desktops, or before the first re-login).

It also raises notifications when call quality drops, and manages
start-at-login. Data is exchanged as JSON strings: easy to read from both
Python and GJS, and trivially versionable.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from callglance import (  # noqa: E402
    APP_ID,
    APP_NAME,
    DBUS_IFACE,
    DBUS_PATH,
    __version__,
    autostart,
    integration,
    shellext,
)
from callglance.config import UI_WRITABLE, Config, state_dir  # noqa: E402
from callglance.history import FIELDS, History  # noqa: E402
from callglance.notifier import Notice, QualityNotifier  # noqa: E402
from callglance.speedtest import SpeedTest, SpeedTestState  # noqa: E402

log = logging.getLogger(__name__)

INTERFACE_XML = f"""
<node>
  <interface name="{DBUS_IFACE}">
    <method name="GetSnapshot">
      <arg type="s" name="snapshot" direction="out"/>
    </method>
    <method name="GetHistory">
      <arg type="u" name="seconds" direction="in"/>
      <arg type="s" name="history" direction="out"/>
    </method>
    <method name="GetSettings">
      <arg type="s" name="settings" direction="out"/>
    </method>
    <method name="SetSetting">
      <arg type="s" name="key" direction="in"/>
      <arg type="s" name="value" direction="in"/>
    </method>
    <method name="RunSpeedTest"/>
    <method name="CancelSpeedTest"/>
    <method name="RegisterPanel"/>
    <method name="UnregisterPanel"/>
    <method name="Show"/>
    <method name="Quit"/>
    <signal name="SnapshotChanged">
      <arg type="s" name="snapshot"/>
    </signal>
    <signal name="SettingsChanged">
      <arg type="s" name="settings"/>
    </signal>
    <signal name="ShowRequested"/>
    <property name="Version" type="s" access="read"/>
    <property name="PanelRegistered" type="b" access="read"/>
    <property name="Demo" type="b" access="read"/>
  </interface>
</node>
"""

UI_GRACE_GNOME = 6.0  # seconds to wait for the shell extension before showing a tray icon
UI_GRACE_OTHER = 0.5
UI_EXIT_NO_TOOLKIT = 3  # the tray helper could not load GTK / AppIndicator


def _app_flags() -> Gio.ApplicationFlags:
    flags = Gio.ApplicationFlags
    return getattr(flags, "DEFAULT_FLAGS", flags.FLAGS_NONE)


def package_data(*parts: str) -> Path:
    return Path(__file__).resolve().parent.joinpath("data", *parts)


def icon_reference() -> str:
    """An icon for notifications: the themed name if installed, else the bundled file."""
    for base in [os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
                 *(os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")]:
        if Path(base, "icons/hicolor/scalable/apps", f"{APP_ID}.svg").exists():
            return APP_ID
    bundled = package_data("icons", f"{APP_ID}.svg")
    return str(bundled) if bundled.exists() else "network-wireless"


class Notifications:
    """Desktop notifications over org.freedesktop.Notifications.

    Used instead of GNotification because it works on every desktop and does not
    depend on an installed .desktop file (so it also works from a source checkout).
    One notification is kept and replaced, never a pile of them.
    """

    BUS = "org.freedesktop.Notifications"
    PATH = "/org/freedesktop/Notifications"

    def __init__(self, connection: Gio.DBusConnection, on_click) -> None:
        self.conn = connection
        self.on_click = on_click
        self.last_id = 0
        self.icon = icon_reference()
        self._sub = connection.signal_subscribe(
            self.BUS, self.BUS, "ActionInvoked", self.PATH, None, Gio.DBusSignalFlags.NONE,
            self._on_action)

    def send(self, notice: Notice) -> None:
        hints = {
            "desktop-entry": GLib.Variant("s", APP_ID),
            "urgency": GLib.Variant("y", notice.urgency),
            "category": GLib.Variant("s", "network"),
        }
        params = GLib.Variant("(susssasa{sv}i)", (
            APP_NAME, self.last_id, self.icon, notice.title, notice.body,
            ["default", "Show details"], hints, -1))
        self.conn.call(self.BUS, self.PATH, self.BUS, "Notify", params, GLib.VariantType("(u)"),
                       Gio.DBusCallFlags.NONE, 5000, None, self._sent)

    def _sent(self, conn: Gio.DBusConnection, result: Gio.AsyncResult) -> None:
        try:
            self.last_id = conn.call_finish(result).unpack()[0]
        except GLib.Error as exc:
            log.warning("could not show a notification: %s", exc.message)

    def _on_action(self, _conn, _sender, _path, _iface, _signal, params) -> None:
        notification_id, action = params.unpack()
        if notification_id == self.last_id and action == "default":
            self.on_click()


class Service(Gio.Application):
    def __init__(self, demo: bool = False, background: bool = False) -> None:
        super().__init__(application_id=APP_ID, flags=_app_flags())
        self.demo = demo
        self.quiet_activation = background
        self.config: Config | None = None
        self.history: History | None = None
        self.engine = None
        self.speedtest: SpeedTest | None = None
        self.notifier: QualityNotifier | None = None
        self.notifications: Notifications | None = None
        self._conn: Gio.DBusConnection | None = None
        self._registrations: list[int] = []
        self._snapshot: dict = {}
        self._snapshot_json = "{}"
        self._panel_owner: str | None = None
        self._panel_watch = 0
        self._ui_proc: subprocess.Popen | None = None
        self._ui_unavailable = False
        self._ui_crashes: list[float] = []
        self._show_pending = False
        self._ui_timer = 0

    # -- GApplication -------------------------------------------------------------
    def do_dbus_register(self, connection: Gio.DBusConnection, object_path: str) -> bool:
        Gio.Application.do_dbus_register(self, connection, object_path)
        node = Gio.DBusNodeInfo.new_for_xml(INTERFACE_XML)
        reg = connection.register_object(DBUS_PATH, node.interfaces[0], self._on_method_call,
                                         self._on_get_property, None)
        self._registrations.append(reg)
        self._conn = connection
        return True

    def do_dbus_unregister(self, connection: Gio.DBusConnection, object_path: str) -> None:
        for reg in self._registrations:
            connection.unregister_object(reg)
        self._registrations.clear()
        Gio.Application.do_dbus_unregister(self, connection, object_path)

    def do_startup(self) -> None:
        Gio.Application.do_startup(self)
        self.hold()
        if self.demo:
            demo_dir = Path(tempfile.mkdtemp(prefix="callglance-demo-"))
            self.config = Config(demo_dir / "config.json")
            # Shorter windows so the story's twists show up within seconds.
            for key, value in (("window", 12), ("loss_window", 20), ("notify_after", 8)):
                self.config.set(key, value, save=False)
        else:
            self.config = Config()
        self._start_engine()
        self.notifier = QualityNotifier(self.config.get)
        conn = self.get_dbus_connection()
        if conn is not None:
            self.notifications = Notifications(conn, self.show)
            # GNOME disables extensions while the screen is locked: the panel goes
            # away and comes back on unlock. Do not put up a tray icon meanwhile.
            conn.signal_subscribe(
                "org.gnome.ScreenSaver", "org.gnome.ScreenSaver", "ActiveChanged",
                "/org/gnome/ScreenSaver", None, Gio.DBusSignalFlags.NONE,
                self._on_screensaver)
        if not self.demo:
            self._first_run_integration()
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, self._on_signal)
        grace = UI_GRACE_GNOME if shellext.is_gnome_session() else UI_GRACE_OTHER
        self._schedule_ui_check(grace)
        log.info("CallGlance %s started%s", __version__, " (demo)" if self.demo else "")

    def do_activate(self) -> None:
        if self.quiet_activation:
            self.quiet_activation = False  # started at login: stay in the background
            return
        self.show()

    def do_shutdown(self) -> None:
        if self.speedtest is not None:
            self.speedtest.cancel()
        if self.engine is not None:
            self.engine.stop()
        if self.history is not None:
            self.history.close()
        self._stop_ui()
        Gio.Application.do_shutdown(self)

    def _on_signal(self) -> bool:
        self.quit()
        return GLib.SOURCE_REMOVE

    # -- set-up -----------------------------------------------------------------------
    def _start_engine(self) -> None:
        assert self.config is not None
        if self.demo:
            from callglance.demo import DemoEngine, prefill_history

            self.history = History(None)
            prefill_history(self.history)
            # Seconds into the demo story to start at (handy for screenshots).
            offset = float(os.environ.get("CALLGLANCE_DEMO_OFFSET", "0") or 0)
            self.engine = DemoEngine(self.config, self.history, offset=offset)
        else:
            from callglance.engine import Engine

            self.history = History(state_dir() / "history.sqlite3",
                                   keep_hours=float(self.config["history_hours"]))
            self.engine = Engine(self.config, self.history)
        if self.demo:
            from callglance.demo import DemoSpeedTest

            self.speedtest = DemoSpeedTest(self.engine.internet_latency, self._on_speedtest)
        else:
            self.speedtest = SpeedTest(self.engine.internet_latency, self._on_speedtest)
        self.engine.add_listener(lambda snap: GLib.idle_add(self._publish, snap))
        self.engine.start()

    def _first_run_integration(self) -> None:
        assert self.config is not None
        if not self.config.get("autostart_initialized"):
            # "Start at login" is on by default; the user can turn it off any time.
            try:
                autostart.set_enabled(True)
            except OSError as exc:
                log.warning("could not enable start at login: %s", exc)
            self.config.set("autostart_initialized", True)
        result = shellext.ensure_enabled_once(self.config)
        log.info("GNOME Shell extension: %s", result)
        try:
            integration.ensure_appimage_integration()
        except OSError as exc:
            log.warning("could not add CallGlance to the app grid: %s", exc)

    # -- snapshots ----------------------------------------------------------------------
    def _publish(self, snap: dict) -> bool:
        if self.speedtest is not None:
            snap["speedtest"] = self.speedtest.state.as_dict()
        snap["demo"] = self.demo
        self._snapshot = snap
        self._snapshot_json = json.dumps(snap, separators=(",", ":"))
        self._emit("SnapshotChanged", GLib.Variant("(s)", (self._snapshot_json,)))
        if self.notifier is not None and self.notifications is not None:
            notice = self.notifier.update(snap)
            if notice is not None:
                self.notifications.send(notice)
        return GLib.SOURCE_REMOVE

    def _on_speedtest(self, state: SpeedTestState) -> None:
        def publish() -> bool:
            if self._snapshot:
                self._publish(dict(self._snapshot))
            return GLib.SOURCE_REMOVE

        GLib.idle_add(publish)

    # -- D-Bus --------------------------------------------------------------------------
    def _emit(self, name: str, params: GLib.Variant | None = None) -> None:
        if self._conn is None:
            return
        try:
            self._conn.emit_signal(None, DBUS_PATH, DBUS_IFACE, name, params)
        except GLib.Error as exc:
            log.debug("could not emit %s: %s", name, exc.message)

    def _emit_property(self, name: str, value: GLib.Variant) -> None:
        if self._conn is None:
            return
        self._conn.emit_signal(
            None, DBUS_PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
            GLib.Variant("(sa{sv}as)", (DBUS_IFACE, {name: value}, [])))

    def _on_get_property(self, _conn, _sender, _path, _iface, name: str) -> GLib.Variant | None:
        if name == "Version":
            return GLib.Variant("s", __version__)
        if name == "PanelRegistered":
            return GLib.Variant("b", self._panel_owner is not None)
        if name == "Demo":
            return GLib.Variant("b", self.demo)
        return None

    def _on_method_call(self, _conn, sender: str, _path, _iface, method: str,
                        params: GLib.Variant, invocation: Gio.DBusMethodInvocation) -> None:
        handler = getattr(self, f"_dbus_{method}", None)
        if handler is None:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)
            return
        try:
            result = handler(sender, *params.unpack())
        except (ValueError, TypeError, KeyError) as exc:
            invocation.return_dbus_error(f"{DBUS_IFACE}.InvalidArgs", str(exc))
            return
        except Exception as exc:
            log.exception("D-Bus method %s failed", method)
            invocation.return_dbus_error(f"{DBUS_IFACE}.Failed", str(exc))
            return
        invocation.return_value(result)

    def _dbus_GetSnapshot(self, _sender: str) -> GLib.Variant:
        return GLib.Variant("(s)", (self._snapshot_json,))

    def _dbus_GetHistory(self, _sender: str, seconds: int) -> GLib.Variant:
        seconds = max(60, min(int(seconds), 7 * 24 * 3600))
        points = self.history.since(seconds) if self.history is not None else []
        payload = {"fields": list(FIELDS), "rows": [[p.get(f) for f in FIELDS] for p in points]}
        return GLib.Variant("(s)", (json.dumps(payload, separators=(",", ":")),))

    def settings(self) -> dict:
        assert self.config is not None
        data = {key: self.config[key] for key in UI_WRITABLE if key != "autostart"}
        try:
            data["autostart"] = autostart.is_enabled()
        except OSError:
            data["autostart"] = False
        gnome = shellext.is_gnome_session()
        enabled = shellext.is_enabled_in_settings() if gnome else None
        data["shell"] = {
            "gnome": gnome,
            "extension_enabled": enabled,
            "panel_registered": self._panel_owner is not None,
            # Installed and enabled, but GNOME Shell has not loaded it yet.
            "relogin_needed": bool(gnome and enabled and self._panel_owner is None),
        }
        data["demo"] = self.demo
        data["version"] = __version__
        return data

    def _dbus_GetSettings(self, _sender: str) -> GLib.Variant:
        return GLib.Variant("(s)", (json.dumps(self.settings()),))

    def _dbus_SetSetting(self, _sender: str, key: str, value_json: str) -> None:
        assert self.config is not None
        if key not in UI_WRITABLE:
            raise KeyError(f"unknown setting {key!r}")
        value = json.loads(value_json)
        if not isinstance(value, UI_WRITABLE[key]):
            raise TypeError(f"{key} must be {UI_WRITABLE[key]}")
        if key == "autostart":
            autostart.set_enabled(bool(value))
        elif key == "widget":
            allowed = {"enabled": bool, "on_top": bool, "x": int, "y": int, "monitor": int}
            clean = {k: v for k, v in value.items()
                     if k in allowed and isinstance(v, allowed[k])}
            self.config.set("widget", clean)
        else:
            self.config.set(key, value)
        self._emit("SettingsChanged", GLib.Variant("(s)", (json.dumps(self.settings()),)))

    def _dbus_RunSpeedTest(self, _sender: str) -> None:
        if self.speedtest is not None:
            self.speedtest.start()

    def _dbus_CancelSpeedTest(self, _sender: str) -> None:
        if self.speedtest is not None:
            self.speedtest.cancel()

    def _dbus_RegisterPanel(self, sender: str) -> None:
        if self._panel_watch:
            Gio.bus_unwatch_name(self._panel_watch)
        self._panel_owner = sender
        self._panel_watch = Gio.bus_watch_name_on_connection(
            self._conn, sender, Gio.BusNameWatcherFlags.NONE, None, self._on_panel_vanished)
        log.info("panel extension registered (%s)", sender)
        self._emit_property("PanelRegistered", GLib.Variant("b", True))
        self._emit("SettingsChanged", GLib.Variant("(s)", (json.dumps(self.settings()),)))

    def _dbus_UnregisterPanel(self, sender: str) -> None:
        if sender == self._panel_owner:
            self._panel_gone()

    def _on_panel_vanished(self, _conn, name: str) -> None:
        if name == self._panel_owner:
            self._panel_gone()

    def _panel_gone(self) -> None:
        if self._panel_watch:
            Gio.bus_unwatch_name(self._panel_watch)
            self._panel_watch = 0
        self._panel_owner = None
        log.info("panel extension went away")
        self._emit_property("PanelRegistered", GLib.Variant("b", False))
        self._schedule_ui_check(2.0)

    def _dbus_Show(self, _sender: str) -> None:
        self.show()

    def _dbus_Quit(self, _sender: str) -> None:
        GLib.idle_add(lambda: self.quit() or GLib.SOURCE_REMOVE)

    # -- showing things -------------------------------------------------------------------
    def show(self) -> None:
        """Bring up the details: the panel popover, or the fallback window."""
        if self._panel_owner is not None or self._ui_running():
            self._emit("ShowRequested")
        else:
            self._show_pending = True
            self._ensure_ui()

    # -- the GTK tray fallback ------------------------------------------------------------
    def _schedule_ui_check(self, delay: float) -> None:
        if self._ui_timer:
            GLib.source_remove(self._ui_timer)

        def check() -> bool:
            self._ui_timer = 0
            self._ensure_ui()
            return GLib.SOURCE_REMOVE

        self._ui_timer = GLib.timeout_add(int(delay * 1000), check)

    def _on_screensaver(self, _conn, _sender, _path, _iface, _signal, params) -> None:
        (active,) = params.unpack()
        if not active:
            self._schedule_ui_check(6.0)  # give the extension time to come back

    def _screen_locked(self) -> bool:
        if self._conn is None or not shellext.is_gnome_session():
            return False
        try:
            reply = self._conn.call_sync(
                "org.gnome.ScreenSaver", "/org/gnome/ScreenSaver", "org.gnome.ScreenSaver",
                "GetActive", None, GLib.VariantType("(b)"), Gio.DBusCallFlags.NO_AUTO_START,
                500, None)
            return bool(reply.unpack()[0])
        except GLib.Error:
            return False

    def _ui_running(self) -> bool:
        return self._ui_proc is not None and self._ui_proc.poll() is None

    def _ensure_ui(self) -> None:
        if self._panel_owner is not None or self._ui_running() or self._ui_unavailable:
            return
        if self._screen_locked():
            return  # re-checked when the screen unlocks
        args = [sys.executable, "-m", "callglance", "ui"]
        if self._show_pending:
            args.append("--show")
            self._show_pending = False
        env = dict(os.environ)
        # The helper runs `python -m callglance`: make sure it finds this very copy
        # (packages keep it in a private directory, not in site-packages).
        here = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = os.pathsep.join(p for p in (here, env.get("PYTHONPATH")) if p)
        try:
            self._ui_proc = subprocess.Popen(args, env=env)
        except OSError as exc:
            log.warning("could not start the tray helper: %s", exc)
            return
        GLib.child_watch_add(GLib.PRIORITY_DEFAULT, self._ui_proc.pid, self._on_ui_exit)

    def _on_ui_exit(self, pid: int, status: int) -> None:
        code = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") \
            else status >> 8
        if self._ui_proc is not None and self._ui_proc.pid == pid:
            self._ui_proc.returncode = code
            self._ui_proc = None
        if code == UI_EXIT_NO_TOOLKIT:
            log.warning("tray icon unavailable (GTK 3 / AyatanaAppIndicator missing)")
            self._ui_unavailable = True
            return
        if code == 0 or self._panel_owner is not None:
            return
        now = time.monotonic()
        self._ui_crashes = [t for t in self._ui_crashes if now - t < 600] + [now]
        if len(self._ui_crashes) <= 3:
            self._schedule_ui_check(2.0 * len(self._ui_crashes))
        else:
            log.error("tray helper keeps crashing; giving up")

    def _stop_ui(self) -> None:
        if self._ui_running():
            self._ui_proc.terminate()
            try:
                self._ui_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._ui_proc.kill()


def run(demo: bool = False, background: bool = False) -> int:
    app = Service(demo=demo, background=background)
    try:
        app.register(None)
    except GLib.Error as exc:
        print(f"callglance: cannot connect to the session bus: {exc.message}", file=sys.stderr)
        return 1
    if app.get_is_remote():
        if not background:
            app.activate()  # the running instance shows its details
        return 0
    return app.run([sys.argv[0]])
