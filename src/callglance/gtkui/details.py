"""The details window: everything the GNOME Shell popover shows, as a GTK window."""

from __future__ import annotations

import time

from gi.repository import Gdk, GLib, Gtk

from callglance.gtkui import draw

SPEED_PHASES = {"latency": "Measuring idle latency…", "download": "Downloading…",
                "upload": "Uploading…"}


def _fg(widget: Gtk.Widget) -> tuple[float, float, float]:
    c = widget.get_style_context().get_color(Gtk.StateFlags.NORMAL)
    return (c.red, c.green, c.blue)


def _set_level(widget: Gtk.Widget, level: str, prefix: str = "cg-") -> None:
    ctx = widget.get_style_context()
    for lv in draw.LEVELS:
        ctx.remove_class(f"{prefix}{lv}")
    ctx.add_class(f"{prefix}{level if level in draw.LEVELS else 'unknown'}")


class Tile(Gtk.Box):
    def __init__(self, title: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.get_style_context().add_class("cg-tile")
        self.value = Gtk.Label(label="–")
        self.value.get_style_context().add_class("cg-tile-value")
        caption = Gtk.Label(label=title)
        caption.get_style_context().add_class("cg-tile-title")
        self.pack_start(self.value, False, False, 0)
        self.pack_start(caption, False, False, 0)

    def update(self, text: str, level: str) -> None:
        self.value.set_text(text)
        _set_level(self.value, level, "cg-text-")


class DetailsWindow(Gtk.Window):
    def __init__(self, client, on_widget_toggle) -> None:
        super().__init__(title="CallGlance")
        self.client = client
        self.on_widget_toggle = on_widget_toggle
        self.snap: dict | None = None
        self.hover_x: float | None = None
        self._updating = False
        self.set_default_size(400, -1)
        self.set_resizable(False)
        self.set_icon_name("io.github.rafay_ah.CallGlance")
        self.get_style_context().add_class("cg-details")
        self.connect("delete-event", lambda *_: self.hide() or True)

        header = Gtk.HeaderBar(title="CallGlance", show_close_button=True)
        self.set_titlebar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_border_width(16)
        box.set_size_request(380, -1)
        self.add(box)

        self.relogin = Gtk.InfoBar(message_type=Gtk.MessageType.INFO)
        self.relogin.get_content_area().add(Gtk.Label(
            label="Log out and back in once to finish setting up the top-bar indicator.",
            wrap=True, xalign=0))
        self.relogin.set_no_show_all(True)
        box.pack_start(self.relogin, False, False, 0)

        head = Gtk.Box(spacing=12)
        self.dot = Gtk.DrawingArea()
        self.dot.set_size_request(16, 16)
        self.dot.set_valign(Gtk.Align.START)
        self.dot.set_margin_top(6)
        self.dot.connect("draw", self._draw_dot)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.headline = Gtk.Label(label="Checking your connection…", xalign=0, wrap=True,
                                  max_width_chars=36)
        self.headline.get_style_context().add_class("cg-headline")
        self.detail = Gtk.Label(xalign=0, wrap=True, max_width_chars=52)
        self.detail.get_style_context().add_class("cg-detail")
        titles.pack_start(self.headline, False, False, 0)
        titles.pack_start(self.detail, False, False, 0)
        head.pack_start(self.dot, False, False, 0)
        head.pack_start(titles, True, True, 0)
        box.pack_start(head, False, False, 0)

        self.chain = Gtk.DrawingArea()
        self.chain.set_size_request(-1, 64)
        self.chain.connect("draw", self._draw_chain)
        box.pack_start(self.chain, False, False, 0)

        tiles = Gtk.Box(spacing=8, homogeneous=True)
        self.tiles = {k: Tile(t) for k, t in (("latency", "LATENCY"), ("jitter", "JITTER"),
                                               ("loss", "LOSS"), ("dns", "DNS"))}
        for tile in self.tiles.values():
            tiles.pack_start(tile, True, True, 0)
        box.pack_start(tiles, False, False, 0)

        graph_head = Gtk.Box(spacing=6)
        title = Gtk.Label(label="Last hour", xalign=0)
        title.get_style_context().add_class("cg-section-title")
        legend = Gtk.Label(label="line: latency · band: jitter · red: loss", xalign=1)
        legend.get_style_context().add_class("cg-legend")
        graph_head.pack_start(title, True, True, 0)
        graph_head.pack_start(legend, False, False, 0)
        box.pack_start(graph_head, False, False, 0)
        self.graph = Gtk.DrawingArea()
        self.graph.set_size_request(-1, 112)
        self.graph.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.graph.connect("draw", self._draw_graph)
        self.graph.connect("motion-notify-event", self._on_graph_motion)
        self.graph.connect("leave-notify-event", self._on_graph_leave)
        box.pack_start(self.graph, False, False, 0)

        self.wifi = Gtk.Label(xalign=0)
        self.wifi.get_style_context().add_class("cg-row-text")
        self.wifi.set_no_show_all(True)
        box.pack_start(self.wifi, False, False, 0)

        self.tips = Gtk.Label(xalign=0, wrap=True, max_width_chars=56)
        self.tips.get_style_context().add_class("cg-tips")
        self.tips.set_no_show_all(True)
        box.pack_start(self.tips, False, False, 0)

        speed = Gtk.Box(spacing=10)
        self.speed_text = Gtk.Label(label="Download, upload and latency under load (~20 s).",
                                    xalign=0, wrap=True, max_width_chars=40)
        self.speed_text.get_style_context().add_class("cg-speed-text")
        self.speed_button = Gtk.Button(label="Run speed test")
        self.speed_button.set_valign(Gtk.Align.CENTER)
        self.speed_button.connect("clicked", self._on_speed)
        speed.pack_start(self.speed_text, True, True, 0)
        speed.pack_start(self.speed_button, False, False, 0)
        box.pack_start(speed, False, False, 0)
        self.speed_bar = Gtk.ProgressBar()
        self.speed_bar.set_no_show_all(True)
        box.pack_start(self.speed_bar, False, False, 0)

        grid = Gtk.Grid(column_spacing=6, row_spacing=6, column_homogeneous=True)
        self.toggles = {}
        for i, (key, text) in enumerate((
                ("widget", "Desktop widget"), ("pinned", "Keep on top"),
                ("notifications", "Alerts"), ("autostart", "Start at login"),
                ("panel_show_latency", "Latency next to icon"))):
            button = Gtk.ToggleButton(label=text)
            button.get_style_context().add_class("cg-pill")
            button.connect("toggled", self._on_toggle, key)
            grid.attach(button, i % 2, i // 2, 2 if i == 4 else 1, 1)
            self.toggles[key] = button
        box.pack_start(grid, False, False, 0)

        self.footer = Gtk.Label(xalign=0)
        self.footer.get_style_context().add_class("cg-footer")
        box.pack_start(self.footer, False, False, 0)

        client.connect("snapshot", lambda _c, snap: self.update(snap))
        client.connect("settings", lambda _c, settings: self.update_settings(settings))
        client.connect("history", lambda _c: self.graph.queue_draw())
        if client.snapshot:
            self.update(client.snapshot)
        if client.settings:
            self.update_settings(client.settings)
        box.show_all()
        self._timer = 0
        self.connect("map", self._on_map)
        self.connect("unmap", self._on_unmap)

    # -- drawing -------------------------------------------------------------------------
    def _draw_dot(self, area, cr) -> bool:
        level = (self.snap or {}).get("level", "unknown")
        color = draw.COLORS.get(level, draw.COLORS["unknown"])
        draw.rgba(cr, color, 0.3)
        cr.arc(8, 8, 8, 0, 6.2832)
        cr.fill()
        draw.rgba(cr, color)
        cr.arc(8, 8, 6, 0, 6.2832)
        cr.fill()
        return False

    def _draw_chain(self, area, cr) -> bool:
        draw.draw_chain(cr, area.get_allocated_width(), area.get_allocated_height(), self.snap,
                        _fg(area))
        return False

    def _draw_graph(self, area, cr) -> bool:
        draw.draw_graph(cr, area.get_allocated_width(), area.get_allocated_height(),
                        self.client.history, _fg(area), hover_x=self.hover_x)
        return False

    def _on_graph_motion(self, _area, event) -> bool:
        self.hover_x = event.x
        self.graph.queue_draw()
        return False

    def _on_graph_leave(self, _area, _event) -> bool:
        self.hover_x = None
        self.graph.queue_draw()
        return False

    # -- data ------------------------------------------------------------------------------
    def update(self, snap: dict) -> None:
        self.snap = snap
        level = snap.get("level", "unknown")
        self.headline.set_text(snap.get("headline", ""))
        _set_level(self.headline, level, "cg-head-")
        self.detail.set_text(snap.get("detail", ""))
        th = snap.get("thresholds") or {}
        m = snap.get("metrics") or {}
        self.tiles["latency"].update(draw.fmt_ms(m.get("latency_ms")), draw.level_for(
            m.get("latency_ms"), th.get("latency_fair"), th.get("latency_poor")))
        self.tiles["jitter"].update(draw.fmt_ms(m.get("jitter_ms")), draw.level_for(
            m.get("jitter_ms"), th.get("jitter_fair"), th.get("jitter_poor")))
        self.tiles["loss"].update(draw.fmt_pct(m.get("loss_pct")), draw.level_for(
            m.get("loss_pct"), th.get("loss_fair"), th.get("loss_poor")))
        dns = snap.get("dns") or {}
        self.tiles["dns"].update("failing" if dns.get("failing") else
                                 draw.fmt_ms(dns.get("latency_ms")), dns.get("level", "unknown"))
        wifi = snap.get("wifi")
        if wifi:
            parts = [wifi.get("ssid") or "Wi-Fi"]
            if wifi.get("signal_dbm") is not None:
                parts.append(f"{draw.fmt_dbm(wifi['signal_dbm'])} ({wifi.get('quality')})")
            if wifi.get("band"):
                parts.append(wifi["band"])
            if wifi.get("bitrate_mbps"):
                parts.append(f"{draw.fmt_mbps(wifi['bitrate_mbps'])} Mb/s")
            if wifi.get("standard"):
                parts.append(wifi["standard"])
            self.wifi.set_text("Wi-Fi  " + " · ".join(parts))
            self.wifi.show()
        else:
            self.wifi.hide()
        tips = snap.get("tips") or []
        self.tips.set_text("\n".join(tips[:2]))
        self.tips.set_visible(bool(tips))
        _set_level(self.tips, level, "cg-tips-")
        self._update_speed(snap.get("speedtest") or {})
        probe = snap.get("probe") or {}
        self.footer.set_text("Demo mode · simulated connection" if snap.get("demo") else
                             "Measured with ICMP" if probe.get("icmp") else
                             "Measured with TCP, UDP & DNS (ICMP not allowed)")
        self.dot.queue_draw()
        self.chain.queue_draw()
        self.graph.queue_draw()

    def _update_speed(self, st: dict) -> None:
        status = st.get("status")
        running = status == "running"
        self.speed_button.set_label("Cancel" if running else
                                    "Run again" if status == "done" else "Run speed test")
        self.speed_bar.set_visible(running)
        if running:
            self.speed_bar.set_fraction(st.get("progress") or 0.0)
            live = f"  {draw.fmt_mbps(st['live_mbps'])} Mb/s" if st.get("live_mbps") else ""
            self.speed_text.set_text(f"{SPEED_PHASES.get(st.get('phase'), 'Testing…')}{live}")
        elif status == "done":
            grade = f" · Bufferbloat {st['grade']}" if st.get("grade") else ""
            ago = max(0, int((time.time() - (st.get("finished_at") or time.time())) / 60))
            when = "just now" if ago < 2 else f"{ago} min ago"
            bloat = ("\nCalls lag when the line is busy: turn on SQM/QoS in your router."
                     if st.get("grade") in ("C", "D", "F") else "")
            self.speed_text.set_text(
                f"↓ {draw.fmt_mbps(st.get('download_mbps'))}  ↑ "
                f"{draw.fmt_mbps(st.get('upload_mbps'))} Mb/s{grade}\n"
                f"{st.get('summary', '')} · {when}{bloat}")
        elif status == "error":
            self.speed_text.set_text(st.get("error") or "Speed test failed.")
        elif status == "cancelled":
            self.speed_text.set_text("Speed test cancelled.")

    def _on_speed(self, _button) -> None:
        if ((self.snap or {}).get("speedtest") or {}).get("status") == "running":
            self.client.cancel_speedtest()
        else:
            self.client.run_speedtest()

    def update_settings(self, settings: dict) -> None:
        self._updating = True
        widget = settings.get("widget") or {}
        self.toggles["widget"].set_active(bool(widget.get("enabled")))
        self.toggles["pinned"].set_active(widget.get("on_top", True) is not False)
        self.toggles["pinned"].set_sensitive(bool(widget.get("enabled")))
        self.toggles["notifications"].set_active(bool(settings.get("notifications")))
        self.toggles["autostart"].set_active(bool(settings.get("autostart")))
        self.toggles["panel_show_latency"].set_active(bool(settings.get("panel_show_latency")))
        self.relogin.set_visible(bool((settings.get("shell") or {}).get("relogin_needed")))
        self._updating = False

    def _on_toggle(self, button, key: str) -> None:
        if self._updating:
            return
        on = button.get_active()
        if key == "widget":
            self.on_widget_toggle(on)
        elif key == "pinned":
            self.client.set_setting("widget", {"on_top": on})
        else:
            self.client.set_setting(key, on)

    def _on_map(self, *_args) -> None:
        self.client.refresh_history()
        if not self._timer:
            self._timer = GLib.timeout_add_seconds(10, self._tick)

    def _on_unmap(self, *_args) -> None:
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0

    def _tick(self) -> bool:
        self.client.refresh_history()
        return GLib.SOURCE_CONTINUE

    def present_now(self) -> None:
        self.show()
        self.present()
