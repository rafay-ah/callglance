"""The desktop widget as a small borderless, translucent GTK window.

On GNOME Wayland the window runs through XWayland (see app.py), because only
X11 windows may position themselves and ask to stay above or below others.
"""

from __future__ import annotations

import cairo
from gi.repository import Gdk, Gio, GLib, Gtk

from callglance.gtkui import draw

DRAG_THRESHOLD = 4


class WidgetWindow(Gtk.Window):
    def __init__(self, client, open_details) -> None:
        super().__init__(type=Gtk.WindowType.TOPLEVEL, title="CallGlance widget")
        self.client = client
        self.open_details = open_details
        self.snap = client.snapshot
        self.on_top = True
        self._press: tuple[float, float] | None = None
        self._save_timer = 0
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_accept_focus(False)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_app_paintable(True)
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None and screen.is_composited():
            self.set_visual(visual)
        size = (draw.CARD_W + 2 * draw.SHADOW, draw.CARD_H + 2 * draw.SHADOW)
        self.set_default_size(*size)
        self.set_size_request(*size)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                        | Gdk.EventMask.POINTER_MOTION_MASK)
        self.connect("draw", self._on_draw)
        self.connect("button-press-event", self._on_press)
        self.connect("button-release-event", self._on_release)
        self.connect("motion-notify-event", self._on_motion)
        self.connect("configure-event", self._on_configure)
        self.connect("delete-event", lambda *_: True)

        self.interface = None
        source = Gio.SettingsSchemaSource.get_default()
        if source is not None and source.lookup("org.gnome.desktop.interface", True) is not None:
            self.interface = Gio.Settings.new("org.gnome.desktop.interface")
            self.interface.connect("changed::color-scheme", lambda *_: self.queue_draw())

        self.menu = Gtk.Menu()
        self.pin_item = Gtk.CheckMenuItem(label="Keep on top")
        self.pin_item.connect("toggled", self._on_pin_toggled)
        open_item = Gtk.MenuItem(label="Open CallGlance")
        open_item.connect("activate", lambda *_: self.open_details())
        hide_item = Gtk.MenuItem(label="Hide widget")
        hide_item.connect("activate", lambda *_: client.set_setting("widget", {"enabled": False}))
        for item in (self.pin_item, open_item, Gtk.SeparatorMenuItem(), hide_item):
            self.menu.append(item)
        self.menu.show_all()

        client.connect("snapshot", self._on_snapshot)
        client.connect("history", lambda _c: self.queue_draw())
        self._history_timer = GLib.timeout_add_seconds(30, self._refresh_history)

    # -- appearance ----------------------------------------------------------------------
    def _dark(self) -> bool:
        if self.interface is not None:
            return self.interface.get_string("color-scheme") == "prefer-dark"
        settings = Gtk.Settings.get_default()
        return bool(settings and settings.get_property("gtk-application-prefer-dark-theme"))

    def _on_draw(self, _window, cr) -> bool:
        cr.set_operator(cairo.OPERATOR_CLEAR)  # start from a fully transparent window
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        draw.draw_card(cr, self.snap, self.client.history, self._dark())
        return False

    def _on_snapshot(self, _client, snap: dict) -> None:
        self.snap = snap
        self.queue_draw()

    def _refresh_history(self) -> bool:
        if self.get_visible():
            self.client.refresh_history()
        return GLib.SOURCE_CONTINUE

    # -- placement -------------------------------------------------------------------------
    def apply_settings(self, widget: dict) -> None:
        self.on_top = widget.get("on_top", True) is not False
        if self.pin_item.get_active() != self.on_top:
            self.pin_item.set_active(self.on_top)
        self.set_keep_above(self.on_top)
        self.set_keep_below(not self.on_top)
        self.stick()  # on every workspace
        x, y = widget.get("x", -1), widget.get("y", -1)
        if x is None or y is None or x < 0 or y < 0:
            display = Gdk.Display.get_default()
            monitor = display.get_primary_monitor() or display.get_monitor(0)
            area = monitor.get_workarea()
            x = area.x + area.width - draw.CARD_W - 20
            y = area.y + 20
        self.move(int(x) - draw.SHADOW, int(y) - draw.SHADOW)

    def _on_pin_toggled(self, item) -> None:
        if item.get_active() != self.on_top:
            self.client.set_setting("widget", {"on_top": item.get_active()})

    # -- interaction -------------------------------------------------------------------------
    def _on_press(self, _w, event) -> bool:
        if event.button == 3:
            self.menu.popup_at_pointer(event)
            return True
        if event.button == 1:
            self._press = (event.x_root, event.y_root)
        return True

    def _on_motion(self, _w, event) -> bool:
        if self._press and (abs(event.x_root - self._press[0]) > DRAG_THRESHOLD
                            or abs(event.y_root - self._press[1]) > DRAG_THRESHOLD):
            self._press = None
            # Let the window manager move us: works on X11 and Wayland alike.
            self.begin_move_drag(1, int(event.x_root), int(event.y_root), event.time)
        return True

    def _on_release(self, _w, event) -> bool:
        if event.button == 1 and self._press is not None:
            self._press = None
            self.open_details()
        return True

    def _on_configure(self, _w, _event) -> bool:
        if self._save_timer:
            GLib.source_remove(self._save_timer)
        self._save_timer = GLib.timeout_add(600, self._save_position)
        return False

    def _save_position(self) -> bool:
        self._save_timer = 0
        x, y = self.get_position()
        # Native Wayland windows do not know where they are (always 0, 0).
        if x or y:
            self.client.set_setting("widget", {"x": int(x) + draw.SHADOW,
                                               "y": int(y) + draw.SHADOW})
        return GLib.SOURCE_REMOVE

    def destroy_widget(self) -> None:
        if self._history_timer:
            GLib.source_remove(self._history_timer)
            self._history_timer = 0
        self.destroy()
