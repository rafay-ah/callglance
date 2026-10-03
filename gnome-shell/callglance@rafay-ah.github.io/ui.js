// Shared helpers: version-safe widgets, formatting, colours and Cairo drawing
// (the path chain and the one-hour graph) used by both the popover and the
// desktop widget.

import Cairo from 'cairo';
import Clutter from 'gi://Clutter';
import GObject from 'gi://GObject';
import Pango from 'gi://Pango';
import PangoCairo from 'gi://PangoCairo';
import St from 'gi://St';

// St.BoxLayout gained `orientation` in GNOME 48 and lost `vertical` in 51.
// Unknown constructor properties throw, so detect instead of guessing.
const HAS_ORIENTATION = Boolean(GObject.Object.find_property.call(St.BoxLayout, 'orientation'));

export function verticalParams() {
    return HAS_ORIENTATION ? {orientation: Clutter.Orientation.VERTICAL} : {vertical: true};
}

export function vbox(params = {}) {
    return new St.BoxLayout({...params, ...verticalParams()});
}

export function hbox(params = {}) {
    return new St.BoxLayout(params);
}

export function label(text, styleClass, params = {}) {
    return new St.Label({text, style_class: styleClass, ...params});
}

export function wrapLabel(text, styleClass, params = {}) {
    const lbl = label(text, styleClass, params);
    lbl.clutter_text.line_wrap = true;
    lbl.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
    return lbl;
}

export const LEVELS = ['good', 'fair', 'poor', 'offline', 'unknown'];

// Apple-style system colours: vivid on light and dark backgrounds alike.
export const COLORS = {
    good: [0.204, 0.780, 0.349],
    fair: [1.000, 0.624, 0.039],
    poor: [1.000, 0.271, 0.227],
    offline: [1.000, 0.271, 0.227],
    unknown: [0.557, 0.557, 0.576],
    line: [0.039, 0.518, 1.000],
};

export function setLevelClass(actor, level, prefix = 'cg-') {
    for (const lv of LEVELS)
        actor.remove_style_class_name(`${prefix}${lv}`);
    actor.add_style_class_name(`${prefix}${LEVELS.includes(level) ? level : 'unknown'}`);
}

export function fmtMs(v) {
    if (v === null || v === undefined)
        return '–';
    return v >= 10 ? `${Math.round(v)} ms` : `${v.toFixed(1)} ms`;
}

export function fmtPct(v) {
    if (v === null || v === undefined)
        return '–';
    if (v === 0)
        return '0%';
    return v < 10 ? `${v.toFixed(1)}%` : `${Math.round(v)}%`;
}

export function fmtDbm(v) {
    return v === null || v === undefined ? '' : `${v < 0 ? '−' : ''}${Math.abs(v)} dBm`;
}

export function fmtMbps(v) {
    if (v === null || v === undefined)
        return '–';
    return v >= 100 ? `${Math.round(v)}` : v.toFixed(1);
}

export function levelFor(value, fair, poor) {
    if (value === null || value === undefined)
        return 'unknown';
    if (value >= poor)
        return 'poor';
    if (value >= fair)
        return 'fair';
    return 'good';
}

// -- Cairo helpers -------------------------------------------------------------

function fgColor(actor) {
    const c = actor.get_theme_node().get_foreground_color();
    return [c.red / 255, c.green / 255, c.blue / 255];
}

function rgba(cr, rgb, a = 1) {
    cr.setSourceRGBA(rgb[0], rgb[1], rgb[2], a);
}

function textLayout(cr, actor, text, px, weight = Pango.Weight.NORMAL) {
    const layout = PangoCairo.create_layout(cr);
    const font = actor.get_theme_node().get_font().copy();
    font.set_absolute_size(px * Pango.SCALE);
    font.set_weight(weight);
    layout.set_font_description(font);
    layout.set_text(text, -1);
    return layout;
}

function drawText(cr, actor, text, x, y, px, {align = 'left', weight, alpha = 1, rgb, within} = {}) {
    const layout = textLayout(cr, actor, text, px, weight);
    const [, logical] = layout.get_pixel_extents();
    let dx = x;
    if (align === 'center')
        dx = x - logical.width / 2;
    else if (align === 'right')
        dx = x - logical.width;
    if (within)  // keep the text inside [0, within]
        dx = Math.max(0, Math.min(dx, within - logical.width));
    rgba(cr, rgb ?? fgColor(actor), alpha);
    cr.moveTo(dx, y);
    PangoCairo.show_layout(cr, layout);
    return logical.width;
}

function roundedRect(cr, x, y, w, h, r) {
    cr.newSubPath();
    cr.arc(x + w - r, y + r, r, -Math.PI / 2, 0);
    cr.arc(x + w - r, y + h - r, r, 0, Math.PI / 2);
    cr.arc(x + r, y + h - r, r, Math.PI / 2, Math.PI);
    cr.arc(x + r, y + r, r, Math.PI, 1.5 * Math.PI);
    cr.closePath();
}

// -- The path chain: You -(Wi-Fi)- Router - ISP - Internet ------------------------

// Which link and nodes to colour, from the snapshot's per-segment status.
export function chainModel(snap) {
    const segs = Object.fromEntries((snap?.segments ?? []).map(s => [s.id, s]));
    const severity = snap && ['fair', 'poor', 'offline'].includes(snap.level) ? snap.level : null;
    const kind = snap?.link?.kind;
    const wifi = snap?.wifi;
    const first = kind === 'wifi' ? 'Wi-Fi' : kind === 'ethernet' ? 'Ethernet' : 'Link';
    const youValue = kind === 'wifi' && wifi?.signal_dbm !== null && wifi?.signal_dbm !== undefined
        ? fmtDbm(wifi.signal_dbm) : kind === 'ethernet' ? 'wired' : '';
    const statusOf = id => segs[id]?.status ?? (id === 'isp' ? 'unmeasured' : 'ok');
    const nodes = [
        {name: 'You', value: youValue, status: 'ok'},
        {name: 'Router', value: fmtMs(segs.router?.latency_ms), status: statusOf('router')},
        {name: 'ISP', value: segs.isp ? fmtMs(segs.isp.latency_ms) : '–', status: statusOf('isp')},
        {name: 'Internet', value: fmtMs(segs.internet?.latency_ms), status: statusOf('internet')},
    ];
    if (!segs.isp)
        nodes[2].status = 'unmeasured';
    return {nodes, severity, linkLabel: first, measuring: !snap || snap.level === 'unknown'};
}

export function drawChain(area, snap, {compact = false} = {}) {
    const cr = area.get_context();
    const [w, h] = area.get_surface_size();
    const {nodes, severity, linkLabel, measuring} = chainModel(snap);
    const margin = compact ? 12 : 30;
    const step = (w - 2 * margin) / (nodes.length - 1);
    const y = compact ? 9 : 22;
    const colorOf = node => {
        if (measuring)
            return COLORS.unknown;
        if (node.status === 'unmeasured')
            return COLORS.unknown;
        if ((node.status === 'origin' || node.status === 'affected') && severity)
            return COLORS[severity];
        return COLORS.good;
    };

    // Links: link i joins node i and node i+1 and takes the colour of node i+1.
    cr.setLineCap(Cairo.LineCap.ROUND);
    for (let i = 0; i < nodes.length - 1; i++) {
        const x0 = margin + i * step, x1 = margin + (i + 1) * step;
        const next = nodes[i + 1];
        const color = colorOf(next);
        const alpha = next.status === 'affected' ? 0.45 : 0.9;
        cr.setLineWidth(next.status === 'origin' ? 3.5 : 2.5);
        if (next.status === 'unmeasured')
            cr.setDash([2, 4], 0);
        else
            cr.setDash([], 0);
        rgba(cr, color, measuring ? 0.35 : alpha);
        cr.moveTo(x0 + 8, y);
        cr.lineTo(x1 - 8, y);
        cr.stroke();
        if (i === 0 && !compact)
            drawText(cr, area, linkLabel, (x0 + x1) / 2, y - 17, 10, {align: 'center', alpha: 0.55});
    }
    cr.setDash([], 0);

    // Nodes.
    nodes.forEach((node, i) => {
        const x = margin + i * step;
        const color = colorOf(node);
        if (node.status === 'origin' && severity) {
            rgba(cr, color, 0.22);
            cr.arc(x, y, compact ? 7.5 : 10.5, 0, 2 * Math.PI);
            cr.fill();
        }
        rgba(cr, color, 1);
        cr.arc(x, y, compact ? 4 : 5.5, 0, 2 * Math.PI);
        cr.fill();
        if (!compact) {
            drawText(cr, area, node.name, x, y + 11, 12,
                {align: 'center', weight: Pango.Weight.SEMIBOLD, within: w});
            drawText(cr, area, node.value, x, y + 27, 11, {align: 'center', alpha: 0.6, within: w});
        } else {
            drawText(cr, area, node.name, x, y + 8, 9.5, {align: 'center', alpha: 0.7, within: w});
        }
    });
    cr.$dispose();
}

// -- The one-hour graph ----------------------------------------------------------

function niceMax(value) {
    for (const step of [20, 50, 100, 150, 200, 300, 500, 1000, 2000])
        if (value <= step)
            return step;
    return Math.ceil(value / 1000) * 1000;
}

function percentile(values, p) {
    if (!values.length)
        return 0;
    const sorted = [...values].sort((a, b) => a - b);
    return sorted[Math.min(sorted.length - 1, Math.floor(p * (sorted.length - 1)))];
}

// Draw latency (line + filled area), jitter (a band around the line), packet
// loss (red ticks along the top) and a strip at the bottom coloured by the
// verdict level, so "when was it bad" reads at a glance.
export function drawGraph(area, points, {compact = false, hoverX = null, span = 3600} = {}) {
    const cr = area.get_context();
    const [w, h] = area.get_surface_size();
    const fg = fgColor(area);
    const now = Date.now() / 1000;
    const left = compact ? 0 : 2;
    const right = w - (compact ? 0 : 2);
    const top = compact ? 4 : 16;
    const stripH = compact ? 3 : 4;
    const bottom = h - (compact ? stripH + 3 : stripH + 18);
    const xOf = ts => left + (right - left) * (1 - (now - ts) / span);

    // Grid and labels.
    const latencies = points.map(p => p.net_ms).filter(v => v !== null && v !== undefined);
    const upper = niceMax(Math.max(20, percentile(latencies.map((v, i) => v + (points[i]?.net_jitter ?? 0) / 2), 0.98) * 1.15));
    const yOf = v => bottom - (bottom - top) * Math.min(v, upper) / upper;
    if (!compact) {
        cr.setLineWidth(1);
        rgba(cr, fg, 0.08);
        for (const frac of [0, 0.5, 1]) {
            const yy = Math.round(top + (bottom - top) * frac) + 0.5;
            cr.moveTo(left, yy);
            cr.lineTo(right, yy);
            cr.stroke();
        }
        drawText(cr, area, `${upper} ms`, left, 0, 9.5, {alpha: 0.5});
        drawText(cr, area, '60 min', left, h - 13, 9.5, {alpha: 0.5});
        drawText(cr, area, '30', (left + right) / 2, h - 13, 9.5, {align: 'center', alpha: 0.5});
        drawText(cr, area, 'now', right, h - 13, 9.5, {align: 'right', alpha: 0.5});
    }

    if (!points.length) {
        if (!compact)
            drawText(cr, area, 'Collecting history…', (left + right) / 2, (top + bottom) / 2 - 7, 11, {align: 'center', alpha: 0.5});
        cr.$dispose();
        return;
    }

    // Split into runs where data is contiguous (gaps > 30 s break the line).
    const runs = [];
    let run = [];
    for (const p of points) {
        if (p.net_ms === null || p.net_ms === undefined) {
            if (run.length)
                runs.push(run);
            run = [];
            continue;
        }
        if (run.length && p.ts - run[run.length - 1].ts > 30) {
            runs.push(run);
            run = [];
        }
        run.push(p);
    }
    if (run.length)
        runs.push(run);

    for (const r of runs) {
        // Jitter band.
        rgba(cr, COLORS.line, compact ? 0.16 : 0.18);
        r.forEach((p, i) => {
            const half = (p.net_jitter ?? 0) / 2;
            const x = xOf(p.ts), yy = yOf(p.net_ms + half);
            if (i === 0)
                cr.moveTo(x, yy);
            else
                cr.lineTo(x, yy);
        });
        for (let i = r.length - 1; i >= 0; i--) {
            const p = r[i];
            cr.lineTo(xOf(p.ts), yOf(Math.max(0, p.net_ms - (p.net_jitter ?? 0) / 2)));
        }
        cr.closePath();
        cr.fill();

        // Area under the latency line, fading out downwards.
        if (r.length > 1) {
            r.forEach((p, i) => {
                const x = xOf(p.ts), yy = yOf(p.net_ms);
                if (i === 0)
                    cr.moveTo(x, yy);
                else
                    cr.lineTo(x, yy);
            });
            cr.lineTo(xOf(r[r.length - 1].ts), bottom);
            cr.lineTo(xOf(r[0].ts), bottom);
            cr.closePath();
            const grad = new Cairo.LinearGradient(0, top, 0, bottom);
            grad.addColorStopRGBA(0, ...COLORS.line, 0.28);
            grad.addColorStopRGBA(1, ...COLORS.line, 0.02);
            cr.setSource(grad);
            cr.fill();
        }

        // Latency line.
        cr.setLineWidth(compact ? 1.5 : 1.8);
        cr.setLineJoin(Cairo.LineJoin.ROUND);
        rgba(cr, COLORS.line, 0.95);
        r.forEach((p, i) => {
            const x = xOf(p.ts), yy = yOf(p.net_ms);
            if (i === 0)
                cr.moveTo(x, yy);
            else
                cr.lineTo(x, yy);
        });
        cr.stroke();
    }

    // Packet loss ticks along the top.
    for (const p of points) {
        if (p.net_loss && p.net_loss > 0) {
            const x = xOf(p.ts);
            const len = Math.min(1, p.net_loss / 10) * (compact ? 6 : 9) + 2;
            rgba(cr, COLORS.poor, 0.9);
            cr.setLineWidth(1.6);
            cr.moveTo(x, top - (compact ? 3 : 2));
            cr.lineTo(x, top - (compact ? 3 : 2) + len);
            cr.stroke();
        }
    }

    // Level strip.
    const stripY = compact ? h - stripH : bottom + 4;
    let prev = null;
    for (const p of points) {
        const x = xOf(p.ts);
        if (prev && p.ts - prev.ts <= 30) {
            const level = LEVELS.includes(prev.level) ? prev.level : 'unknown';
            rgba(cr, COLORS[level], level === 'good' ? 0.55 : 0.95);
            cr.rectangle(xOf(prev.ts), stripY, Math.max(1, x - xOf(prev.ts)), stripH);
            cr.fill();
        }
        prev = p;
    }

    // Hover read-out.
    if (hoverX !== null && !compact) {
        const ts = now - span * (1 - (hoverX - left) / (right - left));
        let best = null;
        for (const p of points)
            if (!best || Math.abs(p.ts - ts) < Math.abs(best.ts - ts))
                best = p;
        if (best && Math.abs(best.ts - ts) < 60) {
            const x = xOf(best.ts);
            rgba(cr, fg, 0.35);
            cr.setLineWidth(1);
            cr.moveTo(Math.round(x) + 0.5, top);
            cr.lineTo(Math.round(x) + 0.5, bottom);
            cr.stroke();
            if (best.net_ms !== null && best.net_ms !== undefined) {
                rgba(cr, COLORS.line, 1);
                cr.arc(x, yOf(best.net_ms), 3, 0, 2 * Math.PI);
                cr.fill();
            }
            const ago = Math.round((now - best.ts) / 60);
            const text = `${ago ? `${ago} min ago` : 'now'} · ${fmtMs(best.net_ms)} · ±${fmtMs(best.net_jitter)} · ${fmtPct(best.net_loss)} loss`;
            const layout = textLayout(cr, area, text, 10.5, Pango.Weight.MEDIUM);
            const [, ext] = layout.get_pixel_extents();
            const bw = ext.width + 14, bh = ext.height + 8;
            const bx = Math.min(Math.max(x - bw / 2, left), right - bw);
            const by = top + 2;
            roundedRect(cr, bx, by, bw, bh, 6);
            cr.setSourceRGBA(0.1, 0.1, 0.12, 0.88);
            cr.fill();
            cr.setSourceRGBA(1, 1, 1, 0.95);
            cr.moveTo(bx + 7, by + 4);
            PangoCairo.show_layout(cr, layout);
        }
    }
    cr.$dispose();
}
