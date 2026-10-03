// The top-bar indicator: a coloured dot, and a popover with the verdict, the
// path from your computer to the internet, live numbers, the last hour and
// a few switches.

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

import {
    drawChain, drawGraph, fmtDbm, fmtMbps, fmtMs, fmtPct, hbox, label, levelFor,
    setLevelClass, vbox, wrapLabel,
} from './ui.js';

const CONTENT_WIDTH = 344;

const SPEED_PHASES = {
    latency: 'Measuring idle latency…',
    download: 'Downloading…',
    upload: 'Uploading…',
};

function relativeTime(ts) {
    const seconds = Math.max(0, Date.now() / 1000 - ts);
    if (seconds < 90)
        return 'just now';
    if (seconds < 3600)
        return `${Math.round(seconds / 60)} min ago`;
    if (seconds < 86400)
        return `${Math.round(seconds / 3600)} h ago`;
    return `${Math.round(seconds / 86400)} d ago`;
}

const MetricTile = GObject.registerClass(
class MetricTile extends St.BoxLayout {
    _init(title) {
        super._init({style_class: 'cg-tile', x_expand: true});
        this._box = vbox({x_expand: true});
        this._value = label('–', 'cg-tile-value');
        this._title = label(title, 'cg-tile-title');
        this._box.add_child(this._value);
        this._box.add_child(this._title);
        this.add_child(this._box);
    }

    update(text, level) {
        this._value.text = text;
        setLevelClass(this._value, level, 'cg-text-');
    }
});

const SignalBars = GObject.registerClass(
class SignalBars extends St.BoxLayout {
    _init() {
        super._init({style_class: 'cg-bars', y_align: Clutter.ActorAlign.CENTER});
        this._bars = [4, 7, 10, 13].map(height => {
            const bar = new St.Widget({
                style_class: 'cg-bar', style: `height: ${height}px;`,
                y_align: Clutter.ActorAlign.END,
            });
            this.add_child(bar);
            return bar;
        });
    }

    update(dbm, pct) {
        let lit = 0;
        if (dbm !== null && dbm !== undefined)
            lit = dbm >= -60 ? 4 : dbm >= -67 ? 3 : dbm >= -75 ? 2 : 1;
        else if (pct !== null && pct !== undefined)
            lit = Math.max(1, Math.ceil(pct / 25));
        this._bars.forEach((bar, i) => {
            if (i < lit)
                bar.add_style_class_name('cg-bar-on');
            else
                bar.remove_style_class_name('cg-bar-on');
        });
    }
});

export const Indicator = GObject.registerClass(
class CallGlanceIndicator extends PanelMenu.Button {
    _init(client, actions) {
        super._init(0.5, 'CallGlance', false);
        this._client = client;
        this._actions = actions; // {setWidget(enabled), setPinned(pinned)}
        this._snap = null;
        this._hoverX = null;
        this._historyTimer = 0;
        this._updatingSwitches = false;

        const box = hbox({style_class: 'cg-panel-box'});
        this._dot = new St.Widget({style_class: 'cg-dot cg-unknown', y_align: Clutter.ActorAlign.CENTER});
        this._panelLabel = label('', 'cg-panel-label', {y_align: Clutter.ActorAlign.CENTER, visible: false});
        box.add_child(this._dot);
        box.add_child(this._panelLabel);
        this.add_child(box);
        this.menu.box.add_style_class_name('cg-menu');

        this._buildMenu();

        this._clientIds = [
            client.connect('snapshot', (_c, snap) => this._update(snap)),
            client.connect('settings', (_c, settings) => this._updateSettings(settings)),
            client.connect('history', () => this._graph.queue_repaint()),
            client.connect('show-requested', () => this.menu.open(true)),
            client.connect('connection-changed', (_c, connected) => this._setConnected(connected)),
        ];
        this.menu.connect('open-state-changed', (_m, open) => this._onMenuToggled(open));
        this._setConnected(client.connected);
        if (client.snapshot)
            this._update(client.snapshot);
        if (client.settings)
            this._updateSettings(client.settings);
    }

    // -- building -------------------------------------------------------------------
    _buildMenu() {
        // Reactive (so it is not drawn as an insensitive item) but inert: no
        // hover highlight, and clicks do not close the menu.
        const item = new PopupMenu.PopupBaseMenuItem({
            reactive: true, activate: false, hover: false, can_focus: false,
        });
        item.add_style_class_name('cg-item');
        const content = vbox({style_class: 'cg-popover', style: `width: ${CONTENT_WIDTH}px;`});
        item.add_child(content);
        this.menu.addMenuItem(item);

        // Verdict.
        const header = hbox({style_class: 'cg-header'});
        this._bigDot = new St.Widget({style_class: 'cg-big-dot cg-unknown', y_align: Clutter.ActorAlign.START});
        const titles = vbox({x_expand: true});
        this._headline = wrapLabel('Checking your connection…', 'cg-headline');
        this._detail = wrapLabel('', 'cg-detail');
        titles.add_child(this._headline);
        titles.add_child(this._detail);
        header.add_child(this._bigDot);
        header.add_child(titles);
        content.add_child(header);

        // You - Router - ISP - Internet.
        this._chain = new St.DrawingArea({style_class: 'cg-chain', x_expand: true});
        this._chain.connect('repaint', area => drawChain(area, this._snap));
        content.add_child(this._chain);

        // Numbers.
        const tiles = hbox({style_class: 'cg-tiles'});
        this._tiles = {
            latency: new MetricTile('LATENCY'),
            jitter: new MetricTile('JITTER'),
            loss: new MetricTile('LOSS'),
            dns: new MetricTile('DNS'),
        };
        for (const tile of Object.values(this._tiles))
            tiles.add_child(tile);
        content.add_child(tiles);

        // The last hour.
        const graphBox = vbox({style_class: 'cg-graph-box'});
        const graphHead = hbox({style_class: 'cg-section-head'});
        graphHead.add_child(label('Last hour', 'cg-section-title', {x_expand: true}));
        for (const [swatch, text] of [['cg-swatch-latency', 'latency'], ['cg-swatch-jitter', 'jitter'],
            ['cg-swatch-loss', 'loss']]) {
            graphHead.add_child(new St.Widget({style_class: `cg-swatch ${swatch}`, y_align: Clutter.ActorAlign.CENTER}));
            graphHead.add_child(label(text, 'cg-legend', {y_align: Clutter.ActorAlign.CENTER}));
        }
        graphBox.add_child(graphHead);
        content.add_child(graphBox);
        this._graph = new St.DrawingArea({style_class: 'cg-graph', x_expand: true, reactive: true});
        this._graph.connect('repaint', area => drawGraph(area, this._client.history, {hoverX: this._hoverX}));
        this._graph.connect('motion-event', (actor, event) => {
            const [x] = event.get_coords();
            const [ok, lx] = actor.transform_stage_point(x, 0);
            this._hoverX = ok ? lx : null;
            actor.queue_repaint();
            return Clutter.EVENT_PROPAGATE;
        });
        this._graph.connect('leave-event', actor => {
            this._hoverX = null;
            actor.queue_repaint();
            return Clutter.EVENT_PROPAGATE;
        });
        graphBox.add_child(this._graph);

        // Wi-Fi details.
        this._wifiRow = hbox({style_class: 'cg-row'});
        this._wifiBars = new SignalBars();
        this._wifiText = label('', 'cg-row-text', {x_expand: true, y_align: Clutter.ActorAlign.CENTER});
        this._wifiRow.add_child(this._wifiBars);
        this._wifiRow.add_child(this._wifiText);
        content.add_child(this._wifiRow);

        // Advice.
        this._tips = wrapLabel('', 'cg-tips');
        content.add_child(this._tips);

        // Speed test.
        const speed = vbox({style_class: 'cg-speed'});
        const speedRow = hbox();
        this._speedButton = new St.Button({style_class: 'cg-button', label: 'Run speed test', can_focus: true});
        this._speedButton.connect('clicked', () => this._onSpeedButton());
        this._speedText = wrapLabel('Download, upload and latency under load (~20 s).', 'cg-speed-text',
            {x_expand: true, y_align: Clutter.ActorAlign.CENTER});
        speedRow.add_child(this._speedText);
        speedRow.add_child(this._speedButton);
        speed.add_child(speedRow);
        this._speedTrack = new St.Widget({style_class: 'cg-progress-track', x_expand: true, visible: false});
        this._speedBar = new St.Widget({style_class: 'cg-progress-bar'});
        this._speedTrack.add_child(this._speedBar);
        speed.add_child(this._speedTrack);
        content.add_child(speed);

        // Toggles, quick-settings style.
        const pills = vbox({style_class: 'cg-pills'});
        const row1 = hbox({style_class: 'cg-pill-row'});
        const row2 = hbox({style_class: 'cg-pill-row'});
        const row3 = hbox({style_class: 'cg-pill-row'});
        this._switches = {
            widget: this._addPill(row1, 'Desktop widget', on => this._actions.setWidget(on)),
            pinned: this._addPill(row1, 'Keep on top', on => this._actions.setPinned(on)),
            notifications: this._addPill(row2, 'Alerts', on => this._client.setSetting('notifications', on)),
            autostart: this._addPill(row2, 'Start at login', on => this._client.setSetting('autostart', on)),
            panel_show_latency: this._addPill(row3, 'Latency in top bar',
                on => this._client.setSetting('panel_show_latency', on)),
        };
        pills.add_child(row1);
        pills.add_child(row2);
        pills.add_child(row3);
        content.add_child(pills);

        // Footer.
        this._footer = label('', 'cg-footer');
        content.add_child(this._footer);
    }

    _addPill(row, text, onToggle) {
        const pill = new St.Button({
            style_class: 'cg-pill', toggle_mode: true, can_focus: true, x_expand: true,
            label: text,
        });
        pill.connect('notify::checked', () => {
            if (!this._updatingSwitches)
                onToggle(pill.checked);
        });
        row.add_child(pill);
        return pill;
    }

    // -- updates ----------------------------------------------------------------------
    _setConnected(connected) {
        this.visible = connected;
        if (!connected)
            this.menu.close();
    }

    _update(snap) {
        this._snap = snap;
        const level = snap.level ?? 'unknown';
        setLevelClass(this._dot, level);
        setLevelClass(this._bigDot, level);
        setLevelClass(this._headline, level, 'cg-head-');
        this._headline.text = snap.headline ?? '';
        this._detail.text = snap.detail ?? '';
        const metrics = snap.metrics ?? {};
        this._panelLabel.text = fmtMs(metrics.latency_ms);
        this.accessible_name = `CallGlance: ${snap.headline ?? ''}`;
        if (this.menu.isOpen)
            this._refreshDetails();
    }

    _refreshDetails() {
        const snap = this._snap;
        if (!snap)
            return;
        const th = snap.thresholds ?? {};
        const m = snap.metrics ?? {};
        this._tiles.latency.update(fmtMs(m.latency_ms), levelFor(m.latency_ms, th.latency_fair, th.latency_poor));
        this._tiles.jitter.update(fmtMs(m.jitter_ms), levelFor(m.jitter_ms, th.jitter_fair, th.jitter_poor));
        this._tiles.loss.update(fmtPct(m.loss_pct), levelFor(m.loss_pct, th.loss_fair, th.loss_poor));
        const dns = snap.dns ?? {};
        this._tiles.dns.update(dns.failing ? 'failing' : fmtMs(dns.latency_ms), dns.level ?? 'unknown');
        this._chain.queue_repaint();
        this._graph.queue_repaint();

        const wifi = snap.wifi;
        this._wifiRow.visible = Boolean(wifi);
        if (wifi) {
            const parts = [wifi.ssid ?? 'Wi-Fi'];
            if (wifi.signal_dbm !== null && wifi.signal_dbm !== undefined)
                parts.push(fmtDbm(wifi.signal_dbm));
            else if (wifi.signal_pct !== null && wifi.signal_pct !== undefined)
                parts.push(`${wifi.signal_pct}%`);
            if (wifi.band)
                parts.push(wifi.band);
            if (wifi.bitrate_mbps)
                parts.push(`${fmtMbps(wifi.bitrate_mbps)} Mb/s`);
            if (wifi.standard)
                parts.push(wifi.standard);
            this._wifiText.text = parts.join(' · ');
            const channel = wifi.channel ? `, channel ${wifi.channel}` : '';
            this._wifiRow.accessible_name = `Wi-Fi ${parts.join(', ')}${channel}`;
            this._wifiBars.update(wifi.signal_dbm, wifi.signal_pct);
        }

        const tips = snap.tips ?? [];
        this._tips.visible = tips.length > 0;
        this._tips.text = tips.slice(0, 2).join('\n');
        setLevelClass(this._tips, snap.level, 'cg-tips-');

        this._updateSpeed(snap.speedtest);

        const probe = snap.probe ?? {};
        const how = probe.icmp ? 'Measured with ICMP' : 'Measured with TCP, UDP & DNS (ICMP not allowed)';
        this._footer.text = snap.demo ? 'Demo mode · simulated connection' : how;
    }

    _updateSpeed(st) {
        const running = st?.status === 'running';
        this._speedButton.label = running ? 'Cancel' : st?.status === 'done' ? 'Run again' : 'Run speed test';
        this._speedTrack.visible = running;
        if (running) {
            const width = Math.round((CONTENT_WIDTH - 4) * (st.progress ?? 0));
            this._speedBar.set_width(Math.max(6, width));
            const live = st.live_mbps ? `  ${fmtMbps(st.live_mbps)} Mb/s` : '';
            this._speedText.text = `${SPEED_PHASES[st.phase] ?? 'Testing…'}${live}`;
        } else if (st?.status === 'done') {
            const grade = st.grade ? `  ·  Bufferbloat ${st.grade}` : '';
            // Bad bufferbloat is the usual cause of calls lagging when someone else is busy.
            const bloat = ['C', 'D', 'F'].includes(st.grade)
                ? `\nCalls lag when the line is busy: turn on SQM/QoS in your router.` : '';
            this._speedText.text = `↓ ${fmtMbps(st.download_mbps)}  ↑ ${fmtMbps(st.upload_mbps)} Mb/s${grade}\n${st.summary} · ${relativeTime(st.finished_at)}${bloat}`;
        } else if (st?.status === 'error') {
            this._speedText.text = st.error ?? 'Speed test failed.';
        } else if (st?.status === 'cancelled') {
            this._speedText.text = 'Speed test cancelled.';
        }
    }

    _onSpeedButton() {
        if (this._snap?.speedtest?.status === 'running')
            this._client.cancelSpeedTest();
        else
            this._client.runSpeedTest();
    }

    _updateSettings(settings) {
        this._updatingSwitches = true;
        const widget = settings.widget ?? {};
        this._switches.widget.checked = Boolean(widget.enabled);
        this._switches.pinned.checked = widget.on_top !== false;
        this._switches.pinned.reactive = Boolean(widget.enabled);
        this._switches.pinned.opacity = widget.enabled ? 255 : 110;
        this._switches.panel_show_latency.checked = Boolean(settings.panel_show_latency);
        this._switches.notifications.checked = Boolean(settings.notifications);
        this._switches.autostart.checked = Boolean(settings.autostart);
        this._updatingSwitches = false;
        this._panelLabel.visible = Boolean(settings.panel_show_latency);
    }

    _onMenuToggled(open) {
        if (this._historyTimer) {
            GLib.source_remove(this._historyTimer);
            this._historyTimer = 0;
        }
        if (!open)
            return;
        this._refreshDetails();
        this._client.refreshHistory();
        this._historyTimer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 10, () => {
            this._client.refreshHistory();
            return GLib.SOURCE_CONTINUE;
        });
    }

    destroy() {
        if (this._historyTimer) {
            GLib.source_remove(this._historyTimer);
            this._historyTimer = 0;
        }
        for (const id of this._clientIds)
            this._client.disconnect(id);
        this._clientIds = [];
        super.destroy();
    }
});
