"""The GTK fallback: a tray icon (StatusNotifierItem via AyatanaAppIndicator),
a details window and the desktop widget.

The service starts this process whenever no GNOME Shell panel extension has
registered: on KDE, XFCE, Cinnamon, MATE, Budgie..., and on GNOME until the
freshly installed extension is loaded at the next log-in. It exits as soon as
the extension takes over, or when the service goes away.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


def _prefer_xwayland() -> None:
    # Only X11 windows may place themselves and stay above or below others, which
    # a desktop widget needs. GNOME's XWayland gives us that under Wayland too.
    if (os.environ.get("WAYLAND_DISPLAY") and os.environ.get("DISPLAY")
            and not os.environ.get("CALLGLANCE_NATIVE_WAYLAND")):
        os.environ["GDK_BACKEND"] = "x11"


_prefer_xwayland()

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402
except (ImportError, ValueError):
    try:
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3 as AppIndicator  # noqa: E402
    except (ImportError, ValueError):
        AppIndicator = None

from callglance import APP_ID  # noqa: E402
from callglance.gtkui import UI_EXIT_NO_TOOLKIT, draw  # noqa: E402
from callglance.gtkui.client import Client  # noqa: E402
from callglance.gtkui.details import DetailsWindow  # noqa: E402
from callglance.gtkui.widget import WidgetWindow  # noqa: E402

CSS_PATH = Path(__file__).with_name("style.css")


DOT_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 22 22">
  <circle cx="11" cy="11" r="9" fill="{color}" fill-opacity="0.35"/>
  <circle cx="11" cy="11" r="6.5" fill="{color}"/>
</svg>
"""


def _icon_dir() -> Path:
    """Write the coloured status dots (SVG, crisp at any scale) to a private icon dir."""
    base = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()) / "callglance-icons"
    base.mkdir(parents=True, exist_ok=True)
    for level in draw.LEVELS:
        r, g, b = (round(c * 255) for c in draw.COLORS[level])
        path = base / f"callglance-{level}.svg"
        path.write_text(DOT_SVG.format(color=f"#{r:02x}{g:02x}{b:02x}"))
    return base


class Tray:
    """The tray icon and its menu (DBusMenu: plain text only, so numbers are text)."""

    def __init__(self, app: FallbackApp) -> None:
        self.app = app
        client = app.client
        self.icons = _icon_dir()
        self.indicator = AppIndicator.Indicator.new(
            APP_ID, "callglance-unknown", AppIndicator.IndicatorCategory.SYSTEM_SERVICES)
        self.indicator.set_icon_theme_path(str(self.icons))
        self.indicator.set_title("CallGlance")
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self._updating = False

        menu = Gtk.Menu()
        self.headline = self._info(menu, "Checking your connection…")
        self.numbers = self._info(menu, "")
        self.path = self._info(menu, "")
        self.wifi = self._info(menu, "")
        menu.append(Gtk.SeparatorMenuItem())
        show = Gtk.MenuItem(label="Show Details…")
        show.connect("activate", lambda *_: app.show_details())
        menu.append(show)
        self.speed = Gtk.MenuItem(label="Run Speed Test")
        self.speed.connect("activate", lambda *_: self._on_speed())
        menu.append(self.speed)
        menu.append(Gtk.SeparatorMenuItem())
        self.checks = {}
        for key, text in (("widget", "Desktop Widget"), ("pinned", "Keep Widget on Top"),
                          ("notifications", "Notify When Quality Drops"),
                          ("autostart", "Start at Login"),
                          ("panel_show_latency", "Show Latency Next to Icon")):
            item = Gtk.CheckMenuItem(label=text)
            item.connect("toggled", self._on_check, key)
            menu.append(item)
            self.checks[key] = item
        menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Quit CallGlance")
        quit_item.connect("activate", lambda *_: client.quit_service())
        menu.append(quit_item)
        menu.show_all()
        self.indicator.set_menu(menu)
        self.indicator.set_secondary_activate_target(show)  # middle click
        self.menu = menu

        client.connect("snapshot", lambda _c, snap: self.update(snap))
        client.connect("settings", lambda _c, settings: self.update_settings(settings))
        if client.snapshot:
            self.update(client.snapshot)
        if client.settings:
            self.update_settings(client.settings)

    @staticmethod
    def _info(menu: Gtk.Menu, text: str) -> Gtk.MenuItem:
        item = Gtk.MenuItem(label=text)
        item.set_sensitive(False)
        menu.append(item)
        return item

    def update(self, snap: dict) -> None:
        level = snap.get("level", "unknown")
        self.indicator.set_icon_full(f"callglance-{level}", snap.get("headline", "CallGlance"))
        self.headline.set_label(snap.get("headline", ""))
        m = snap.get("metrics") or {}
        self.numbers.set_label(f"{draw.fmt_ms(m.get('latency_ms'))} latency · "
                               f"{draw.fmt_ms(m.get('jitter_ms'))} jitter · "
                               f"{draw.fmt_pct(m.get('loss_pct'))} loss")
        segs = {s["id"]: s for s in snap.get("segments", [])}
        hops = [f"{segs[k]['label']} {draw.fmt_ms(segs[k].get('latency_ms'))}"
                for k in ("router", "isp", "internet") if k in segs]
        self.path.set_label(" → ".join(hops))
        wifi = snap.get("wifi")
        self.wifi.set_visible(bool(wifi))
        if wifi:
            bits = [wifi.get("ssid") or "Wi-Fi"]
            if wifi.get("signal_dbm") is not None:
                bits.append(draw.fmt_dbm(wifi["signal_dbm"]))
            if wifi.get("band"):
                bits.append(wifi["band"])
            if wifi.get("bitrate_mbps"):
                bits.append(f"{draw.fmt_mbps(wifi['bitrate_mbps'])} Mb/s")
            self.wifi.set_label("Wi-Fi: " + " · ".join(bits))
        st = snap.get("speedtest") or {}
        if st.get("status") == "running":
            self.speed.set_label(f"Cancel Speed Test ({int((st.get('progress') or 0) * 100)}%)")
        elif st.get("status") == "done":
            self.speed.set_label(f"Speed Test: ↓ {draw.fmt_mbps(st.get('download_mbps'))} "
                                 f"↑ {draw.fmt_mbps(st.get('upload_mbps'))} Mb/s "
                                 f"{('· ' + st['grade']) if st.get('grade') else ''} (run again)")
        else:
            self.speed.set_label("Run Speed Test")
        if self.app.client.settings and self.app.client.settings.get("panel_show_latency"):
            self.indicator.set_label(draw.fmt_ms(m.get("latency_ms")), "000 ms")
        else:
            self.indicator.set_label("", "")

    def _on_speed(self) -> None:
        st = (self.app.client.snapshot or {}).get("speedtest") or {}
        if st.get("status") == "running":
            self.app.client.cancel_speedtest()
        else:
            self.app.client.run_speedtest()

    def update_settings(self, settings: dict) -> None:
        self._updating = True
        widget = settings.get("widget") or {}
        self.checks["widget"].set_active(bool(widget.get("enabled")))
        self.checks["pinned"].set_active(widget.get("on_top", True) is not False)
        self.checks["pinned"].set_sensitive(bool(widget.get("enabled")))
        for key in ("notifications", "autostart", "panel_show_latency"):
            self.checks[key].set_active(bool(settings.get(key)))
        self._updating = False

    def _on_check(self, item, key: str) -> None:
        if self._updating:
            return
        on = item.get_active()
        if key == "widget":
            self.app.set_widget(on)
        elif key == "pinned":
            self.app.client.set_setting("widget", {"on_top": on})
        else:
            self.app.client.set_setting(key, on)

    def hide(self) -> None:
        self.indicator.set_status(AppIndicator.IndicatorStatus.PASSIVE)


class FallbackApp:
    def __init__(self, show: bool) -> None:
        self.exit_code = 0
        provider = Gtk.CssProvider()
        provider.load_from_path(str(CSS_PATH))
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.client = Client()
        self.details = DetailsWindow(self.client, self.set_widget)
        self.widget: WidgetWindow | None = None
        self.tray = Tray(self) if AppIndicator is not None else None
        self.client.connect("settings", lambda _c, s: self._sync_widget(s))
        self.client.connect("show-requested", lambda _c: self.show_details())
        self.client.connect("panel-changed", self._on_panel)
        self.client.connect("vanished", lambda _c: self.quit())
        if show or self.tray is None:
            self.show_details()

    def show_details(self) -> None:
        self.details.present_now()

    def set_widget(self, enabled: bool) -> None:
        self.client.set_setting("widget", {"enabled": enabled})

    def _sync_widget(self, settings: dict) -> None:
        widget = settings.get("widget") or {}
        if widget.get("enabled"):
            if self.widget is None:
                self.widget = WidgetWindow(self.client, self.show_details)
                self.widget.apply_settings(widget)
                self.widget.show_all()
            else:
                self.widget.apply_settings(widget)
        elif self.widget is not None:
            self.widget.destroy_widget()
            self.widget = None

    def _on_panel(self, _client, registered: bool) -> None:
        if registered:
            log.info("the GNOME Shell extension took over; leaving")
            self.quit()

    def quit(self) -> None:
        if self.tray is not None:
            self.tray.hide()
        Gtk.main_quit()


def run(show: bool = False) -> int:
    if Gdk.Screen.get_default() is None:
        log.warning("no display to show a tray icon on")
        return UI_EXIT_NO_TOOLKIT
    app = FallbackApp(show)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda: app.quit() or False)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, lambda: app.quit() or False)
    if app.client.panel_registered:
        return 0
    Gtk.main()
    return app.exit_code


if __name__ == "__main__":
    sys.exit(run())
