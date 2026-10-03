"""Cairo drawing shared by the GTK windows: the path chain, the hour graph, and the
whole desktop-widget card. A port of the GNOME Shell extension's ui.js, so both
interfaces look alike."""

from __future__ import annotations

import math
import time

import cairo
import gi

gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Pango, PangoCairo  # noqa: E402

LEVELS = ("good", "fair", "poor", "offline", "unknown")
COLORS = {
    "good": (0.204, 0.780, 0.349),
    "fair": (1.000, 0.624, 0.039),
    "poor": (1.000, 0.271, 0.227),
    "offline": (1.000, 0.271, 0.227),
    "unknown": (0.557, 0.557, 0.576),
    "line": (0.039, 0.518, 1.000),
}

FONT = "Sans"


def fmt_ms(v) -> str:
    if v is None:
        return "–"
    return f"{v:.0f} ms" if v >= 10 else f"{v:.1f} ms"


def fmt_pct(v) -> str:
    if v is None:
        return "–"
    if v == 0:
        return "0%"
    return f"{v:.1f}%" if v < 10 else f"{v:.0f}%"


def fmt_dbm(v) -> str:
    return "" if v is None else f"{'−' if v < 0 else ''}{abs(v)} dBm"


def fmt_mbps(v) -> str:
    if v is None:
        return "–"
    return f"{v:.0f}" if v >= 100 else f"{v:.1f}"


def level_for(value, fair, poor) -> str:
    if value is None or fair is None or poor is None:
        return "unknown"
    if value >= poor:
        return "poor"
    if value >= fair:
        return "fair"
    return "good"


def rgba(cr, rgb, a: float = 1.0) -> None:
    cr.set_source_rgba(rgb[0], rgb[1], rgb[2], a)


def layout(cr, text: str, px: float, weight: int = Pango.Weight.NORMAL, font: str = FONT):
    lay = PangoCairo.create_layout(cr)
    desc = Pango.FontDescription.from_string(font)
    desc.set_absolute_size(px * Pango.SCALE)
    desc.set_weight(weight)
    lay.set_font_description(desc)
    lay.set_text(text, -1)
    return lay


def text(cr, value: str, x: float, y: float, px: float, fg, *, align: str = "left",
         weight: int = Pango.Weight.NORMAL, alpha: float = 1.0, within: float | None = None,
         rgb=None, max_width: float | None = None) -> float:
    lay = layout(cr, value, px, weight)
    if max_width is not None:
        lay.set_width(int(max_width * Pango.SCALE))
        lay.set_ellipsize(Pango.EllipsizeMode.END)
    _ink, logical = lay.get_pixel_extents()
    dx = x
    if align == "center":
        dx = x - logical.width / 2
    elif align == "right":
        dx = x - logical.width
    if within is not None:
        dx = max(0.0, min(dx, within - logical.width))
    rgba(cr, rgb or fg, alpha)
    cr.move_to(dx, y)
    PangoCairo.show_layout(cr, lay)
    return logical.width


def rounded_rect(cr, x, y, w, h, r) -> None:
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


# -- the path chain -----------------------------------------------------------------

def chain_model(snap: dict | None):
    segs = {s["id"]: s for s in (snap or {}).get("segments", [])}
    level = (snap or {}).get("level")
    severity = level if level in ("fair", "poor", "offline") else None
    kind = ((snap or {}).get("link") or {}).get("kind")
    wifi = (snap or {}).get("wifi") or {}
    first = {"wifi": "Wi-Fi", "ethernet": "Ethernet"}.get(kind, "Link")
    you = fmt_dbm(wifi.get("signal_dbm")) if kind == "wifi" and wifi.get("signal_dbm") is not None \
        else ("wired" if kind == "ethernet" else "")

    def status(key: str) -> str:
        return (segs.get(key) or {}).get("status", "unmeasured" if key == "isp" else "ok")

    nodes = [
        {"name": "You", "value": you, "status": "ok"},
        {"name": "Router", "value": fmt_ms((segs.get("router") or {}).get("latency_ms")),
         "status": status("router")},
        {"name": "ISP", "value": fmt_ms(segs["isp"].get("latency_ms")) if "isp" in segs else "–",
         "status": status("isp") if "isp" in segs else "unmeasured"},
        {"name": "Internet", "value": fmt_ms((segs.get("internet") or {}).get("latency_ms")),
         "status": status("internet")},
    ]
    measuring = not snap or level == "unknown"
    return nodes, severity, first, measuring


def draw_chain(cr, w: float, h: float, snap: dict | None, fg, compact: bool = False) -> None:
    nodes, severity, link_label, measuring = chain_model(snap)
    margin = 12 if compact else 30
    step = (w - 2 * margin) / (len(nodes) - 1)
    y = 9 if compact else 22

    def color_of(node):
        if measuring or node["status"] == "unmeasured":
            return COLORS["unknown"]
        if node["status"] in ("origin", "affected") and severity:
            return COLORS[severity]
        return COLORS["good"]

    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    for i in range(len(nodes) - 1):
        x0, x1 = margin + i * step, margin + (i + 1) * step
        nxt = nodes[i + 1]
        cr.set_line_width(3.5 if nxt["status"] == "origin" else 2.5)
        cr.set_dash([2, 4] if nxt["status"] == "unmeasured" else [])
        alpha = 0.35 if measuring else (0.45 if nxt["status"] == "affected" else 0.9)
        rgba(cr, color_of(nxt), alpha)
        cr.move_to(x0 + 8, y)
        cr.line_to(x1 - 8, y)
        cr.stroke()
        if i == 0 and not compact:
            text(cr, link_label, (x0 + x1) / 2, y - 17, 10, fg, align="center", alpha=0.55)
    cr.set_dash([])
    for i, node in enumerate(nodes):
        x = margin + i * step
        color = color_of(node)
        if node["status"] == "origin" and severity:
            rgba(cr, color, 0.22)
            cr.arc(x, y, 7.5 if compact else 10.5, 0, 2 * math.pi)
            cr.fill()
        rgba(cr, color)
        cr.arc(x, y, 4 if compact else 5.5, 0, 2 * math.pi)
        cr.fill()
        if compact:
            text(cr, node["name"], x, y + 8, 9.5, fg, align="center", alpha=0.7, within=w)
        else:
            text(cr, node["name"], x, y + 11, 12, fg, align="center", weight=Pango.Weight.SEMIBOLD,
                 within=w)
            text(cr, node["value"], x, y + 27, 11, fg, align="center", alpha=0.6, within=w)


# -- the hour graph -----------------------------------------------------------------

def _nice_max(value: float) -> int:
    for step in (20, 50, 100, 150, 200, 300, 500, 1000, 2000):
        if value <= step:
            return step
    return int(math.ceil(value / 1000) * 1000)


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(p * (len(ordered) - 1)))]


def draw_graph(cr, w: float, h: float, points: list[dict], fg, compact: bool = False,
               hover_x: float | None = None, span: float = 3600.0) -> None:
    now = time.time()
    left, right = (0.0, w) if compact else (2.0, w - 2.0)
    top = 4.0 if compact else 16.0
    strip_h = 3.0 if compact else 4.0
    bottom = h - (strip_h + 3 if compact else strip_h + 18)

    def x_of(ts: float) -> float:
        return left + (right - left) * (1 - (now - ts) / span)

    lat = [p["net_ms"] + (p.get("net_jitter") or 0) / 2
           for p in points if p.get("net_ms") is not None]
    upper = _nice_max(max(20.0, _percentile(lat, 0.98) * 1.15))

    def y_of(v: float) -> float:
        return bottom - (bottom - top) * min(v, upper) / upper

    if not compact:
        cr.set_line_width(1)
        rgba(cr, fg, 0.08)
        for frac in (0, 0.5, 1):
            yy = round(top + (bottom - top) * frac) + 0.5
            cr.move_to(left, yy)
            cr.line_to(right, yy)
            cr.stroke()
        text(cr, f"{upper} ms", left, 0, 9.5, fg, alpha=0.5)
        text(cr, "60 min", left, h - 13, 9.5, fg, alpha=0.5)
        text(cr, "30", (left + right) / 2, h - 13, 9.5, fg, align="center", alpha=0.5)
        text(cr, "now", right, h - 13, 9.5, fg, align="right", alpha=0.5)
    if not points:
        if not compact:
            text(cr, "Collecting history…", (left + right) / 2, (top + bottom) / 2 - 7, 11, fg,
                 align="center", alpha=0.5)
        return

    runs: list[list[dict]] = []
    run: list[dict] = []
    for p in points:
        if p.get("net_ms") is None:
            if run:
                runs.append(run)
            run = []
            continue
        if run and p["ts"] - run[-1]["ts"] > 30:
            runs.append(run)
            run = []
        run.append(p)
    if run:
        runs.append(run)

    for r in runs:
        # Jitter band.
        rgba(cr, COLORS["line"], 0.16 if compact else 0.18)
        for i, p in enumerate(r):
            pt = (x_of(p["ts"]), y_of(p["net_ms"] + (p.get("net_jitter") or 0) / 2))
            (cr.move_to if i == 0 else cr.line_to)(*pt)
        for p in reversed(r):
            cr.line_to(x_of(p["ts"]), y_of(max(0.0, p["net_ms"] - (p.get("net_jitter") or 0) / 2)))
        cr.close_path()
        cr.fill()
        # Area under the line.
        if len(r) > 1:
            for i, p in enumerate(r):
                pt = (x_of(p["ts"]), y_of(p["net_ms"]))
                (cr.move_to if i == 0 else cr.line_to)(*pt)
            cr.line_to(x_of(r[-1]["ts"]), bottom)
            cr.line_to(x_of(r[0]["ts"]), bottom)
            cr.close_path()
            grad = cairo.LinearGradient(0, top, 0, bottom)
            grad.add_color_stop_rgba(0, *COLORS["line"], 0.28)
            grad.add_color_stop_rgba(1, *COLORS["line"], 0.02)
            cr.set_source(grad)
            cr.fill()
        # Latency line.
        cr.set_line_width(1.5 if compact else 1.8)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        rgba(cr, COLORS["line"], 0.95)
        for i, p in enumerate(r):
            pt = (x_of(p["ts"]), y_of(p["net_ms"]))
            (cr.move_to if i == 0 else cr.line_to)(*pt)
        cr.stroke()

    for p in points:
        loss = p.get("net_loss") or 0
        if loss > 0:
            x = x_of(p["ts"])
            length = min(1.0, loss / 10) * (6 if compact else 9) + 2
            rgba(cr, COLORS["poor"], 0.9)
            cr.set_line_width(1.6)
            y0 = top - (3 if compact else 2)
            cr.move_to(x, y0)
            cr.line_to(x, y0 + length)
            cr.stroke()

    strip_y = h - strip_h if compact else bottom + 4
    prev = None
    for p in points:
        if prev is not None and p["ts"] - prev["ts"] <= 30:
            level = prev.get("level") if prev.get("level") in LEVELS else "unknown"
            rgba(cr, COLORS[level], 0.55 if level == "good" else 0.95)
            cr.rectangle(x_of(prev["ts"]), strip_y, max(1.0, x_of(p["ts"]) - x_of(prev["ts"])),
                         strip_h)
            cr.fill()
        prev = p

    if hover_x is not None and not compact:
        ts = now - span * (1 - (hover_x - left) / (right - left))
        best = min(points, key=lambda p: abs(p["ts"] - ts))
        if abs(best["ts"] - ts) < 60:
            x = x_of(best["ts"])
            rgba(cr, fg, 0.35)
            cr.set_line_width(1)
            cr.move_to(round(x) + 0.5, top)
            cr.line_to(round(x) + 0.5, bottom)
            cr.stroke()
            if best.get("net_ms") is not None:
                rgba(cr, COLORS["line"])
                cr.arc(x, y_of(best["net_ms"]), 3, 0, 2 * math.pi)
                cr.fill()
            ago = round((now - best["ts"]) / 60)
            label = (f"{f'{ago} min ago' if ago else 'now'} · {fmt_ms(best.get('net_ms'))} · "
                     f"±{fmt_ms(best.get('net_jitter'))} · {fmt_pct(best.get('net_loss'))} loss")
            lay = layout(cr, label, 10.5, Pango.Weight.MEDIUM)
            _ink, ext = lay.get_pixel_extents()
            bw, bh = ext.width + 14, ext.height + 8
            bx = min(max(x - bw / 2, left), right - bw)
            by = top + 2
            rounded_rect(cr, bx, by, bw, bh, 6)
            cr.set_source_rgba(0.1, 0.1, 0.12, 0.88)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 0.95)
            cr.move_to(bx + 7, by + 4)
            PangoCairo.show_layout(cr, lay)


# -- the desktop widget card ----------------------------------------------------------

CARD_W, CARD_H = 296, 176
SHADOW = 22  # transparent margin around the card for its shadow


def draw_card(cr, snap: dict | None, history: list[dict], dark: bool) -> None:
    """The whole widget, drawn at (SHADOW, SHADOW) inside a transparent surface."""
    x0, y0, w, h, r = SHADOW, SHADOW, CARD_W, CARD_H, 22
    # Soft shadow: a few blurred-looking concentric rounded rects.
    for i in range(SHADOW, 0, -2):
        a = 0.02 * (1 - i / SHADOW) * (1.6 if dark else 1.0)
        rounded_rect(cr, x0 - i / 2, y0 - i / 2 + 6, w + i, h + i, r + i / 2)
        cr.set_source_rgba(0, 0, 0, a)
        cr.fill()
    rounded_rect(cr, x0, y0, w, h, r)
    grad = cairo.LinearGradient(0, y0, 0, y0 + h)
    if dark:
        grad.add_color_stop_rgba(0, 50 / 255, 50 / 255, 54 / 255, 0.95)
        grad.add_color_stop_rgba(1, 30 / 255, 30 / 255, 33 / 255, 0.95)
    else:
        grad.add_color_stop_rgba(0, 1, 1, 1, 0.97)
        grad.add_color_stop_rgba(1, 242 / 255, 242 / 255, 247 / 255, 0.97)
    cr.set_source(grad)
    cr.fill_preserve()
    cr.set_line_width(1)
    cr.set_source_rgba(*((1, 1, 1, 0.10) if dark else (0, 0, 0, 0.06)))
    cr.stroke()

    fg = (245 / 255, 245 / 255, 247 / 255) if dark else (29 / 255, 29 / 255, 31 / 255)
    dim = (142 / 255, 142 / 255, 147 / 255)
    snap = snap or {}
    level = snap.get("level") or "unknown"
    metrics = snap.get("metrics") or {}
    px, py = x0 + 16, y0 + 14
    inner_w = w - 32

    text(cr, "CALLGLANCE", px, py, 10, dim, weight=Pango.Weight.HEAVY)
    lat = fmt_ms(metrics.get("latency_ms"))
    lw = text(cr, lat, px + inner_w, py - 1, 12, fg, align="right", weight=Pango.Weight.BOLD)
    dot_x = px + inner_w - lw - 9
    rgba(cr, COLORS.get(level, COLORS["unknown"]))
    cr.arc(dot_x, py + 7, 4.5, 0, 2 * math.pi)
    cr.fill()

    head_rgb = COLORS[level] if level in ("fair", "poor", "offline") else fg
    text(cr, snap.get("headline") or "Checking…", px, py + 18, 18, head_rgb,
         weight=Pango.Weight.HEAVY, max_width=inner_w)
    parts = [f"{fmt_ms(metrics.get('jitter_ms'))} jitter",
             f"{fmt_pct(metrics.get('loss_pct'))} loss"]
    wifi = snap.get("wifi") or {}
    if wifi.get("signal_dbm") is not None:
        parts.append(f"Wi-Fi {fmt_dbm(wifi['signal_dbm'])}")
    text(cr, "  ·  ".join(parts), px, py + 44, 11.5, dim, max_width=inner_w)

    cr.save()
    cr.translate(px, py + 66)
    draw_chain(cr, inner_w, 26, snap, fg, compact=True)
    cr.restore()
    cr.save()
    cr.translate(px, py + 100)
    draw_graph(cr, inner_w, 44, history, fg, compact=True)
    cr.restore()
