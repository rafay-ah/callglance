// The desktop widget: a small card with the verdict, the path and the last hour.
//
// Pinned, it floats above windows (and hides for fullscreen apps); unpinned, it
// sits on the desktop behind your windows, like a macOS desktop widget. Drag it
// anywhere; it snaps to screen edges and remembers its place.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

import {
    drawChain, drawGraph, fmtDbm, fmtMs, fmtPct, hbox, label, setLevelClass,
    verticalParams, wrapLabel,
} from './ui.js';

const MARGIN = 20;
const SNAP = 24;
const DRAG_THRESHOLD = 4;

export const DesktopWidget = GObject.registerClass(
class CallGlanceWidget extends St.BoxLayout {
    _init(client, {openDetails}) {
        super._init({
            style_class: 'cg-widget',
            reactive: true,
            track_hover: true,
            ...verticalParams(),
        });
        this._client = client;
        this._openDetails = openDetails;
        this._snap = null;
        this._onTop = true;
        this._inChrome = false;
        this._drag = null;
        this._grab = null;
        this._historyTimer = 0;

        const top = hbox({style_class: 'cg-w-top'});
        top.add_child(label('CALLGLANCE', 'cg-w-app', {x_expand: true, y_align: Clutter.ActorAlign.CENTER}));
        this._dot = new St.Widget({style_class: 'cg-w-dot cg-unknown', y_align: Clutter.ActorAlign.CENTER});
        this._latency = label('–', 'cg-w-latency', {y_align: Clutter.ActorAlign.CENTER});
        top.add_child(this._dot);
        top.add_child(this._latency);
        this.add_child(top);

        this._headline = wrapLabel('Checking…', 'cg-w-headline');
        this.add_child(this._headline);
        this._detail = label('', 'cg-w-detail');
        this.add_child(this._detail);

        this._chain = new St.DrawingArea({style_class: 'cg-w-chain', x_expand: true});
        this._chain.connect('repaint', area => drawChain(area, this._snap, {compact: true}));
        this.add_child(this._chain);

        this._graph = new St.DrawingArea({style_class: 'cg-w-graph', x_expand: true});
        this._graph.connect('repaint', area => drawGraph(area, this._client.history, {compact: true}));
        this.add_child(this._graph);

        this._buildMenu();

        this.connect('button-press-event', (_a, event) => this._onPress(event));
        this.connect('motion-event', (_a, event) => this._onMotion(event));
        this.connect('button-release-event', (_a, event) => this._onRelease(event));

        this._interface = new Gio.Settings({schema_id: 'org.gnome.desktop.interface'});
        this._interfaceId = this._interface.connect('changed::color-scheme', () => this._applyScheme());
        this._applyScheme();

        this._monitorsId = Main.layoutManager.connect('monitors-changed', () => this._place());
        this._clientIds = [
            client.connect('snapshot', (_c, snap) => this._update(snap)),
            client.connect('history', () => this._graph.queue_repaint()),
        ];
        if (client.snapshot)
            this._update(client.snapshot);
        this._historyTimer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 30, () => {
            client.refreshHistory();
            return GLib.SOURCE_CONTINUE;
        });
    }

    _buildMenu() {
        this._menu = new PopupMenu.PopupMenu(this, 0.5, St.Side.TOP);
        this._menuManager = new PopupMenu.PopupMenuManager(this);
        this._menuManager.addMenu(this._menu);
        Main.uiGroup.add_child(this._menu.actor);
        this._menu.actor.hide();
        this._pinItem = new PopupMenu.PopupSwitchMenuItem('Keep on top', this._onTop);
        // Read item.state: on GNOME 47.0 the signal argument is not the state.
        this._pinItem.connect('toggled', item => {
            if (item.state !== this._onTop)
                this.setOnTop(item.state, true);
        });
        this._menu.addMenuItem(this._pinItem);
        this._menu.addAction('Open CallGlance', () => this._openDetails());
        this._menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._menu.addAction('Hide widget', () => this._client.setSetting('widget', {enabled: false}));
    }

    _applyScheme() {
        const scheme = this._interface.get_string('color-scheme');
        if (scheme === 'prefer-dark') {
            this.remove_style_class_name('cg-light');
        } else {
            this.add_style_class_name('cg-light');
        }
        this._chain?.queue_repaint();
        this._graph?.queue_repaint();
    }

    // -- data ---------------------------------------------------------------------
    _update(snap) {
        this._snap = snap;
        setLevelClass(this._dot, snap.level);
        const m = snap.metrics ?? {};
        this._latency.text = fmtMs(m.latency_ms);
        this._headline.text = snap.headline ?? '';
        setLevelClass(this._headline, snap.level, 'cg-head-');
        const parts = [`${fmtMs(m.jitter_ms)} jitter`, `${fmtPct(m.loss_pct)} loss`];
        if (snap.wifi?.signal_dbm !== null && snap.wifi?.signal_dbm !== undefined)
            parts.push(`Wi-Fi ${fmtDbm(snap.wifi.signal_dbm)}`);
        this._detail.text = parts.join('  ·  ');
        this._chain.queue_repaint();
    }

    // -- layers and placement ------------------------------------------------------
    setOnTop(onTop, save = false) {
        const changed = onTop !== this._onTop || !this.get_parent();
        this._onTop = onTop;
        if (this._pinItem.state !== onTop)
            this._pinItem.setToggleState(onTop);
        if (changed) {
            this._detach();
            if (onTop || !Main.layoutManager._backgroundGroup) {
                // (No affectsInputRegion: it is the default, and GNOME 50 rejects the key.)
                Main.layoutManager.addChrome(this, {trackFullscreen: true});
                this._inChrome = true;
            } else {
                // Above the wallpaper, below every window.
                Main.layoutManager._backgroundGroup.add_child(this);
                this._inChrome = false;
            }
            this._place();
        }
        if (save)
            this._client.setSetting('widget', {on_top: onTop});
    }

    _detach() {
        if (!this.get_parent())
            return;
        if (this._inChrome)
            Main.layoutManager.removeChrome(this);
        else
            this.get_parent().remove_child(this);
        this._inChrome = false;
    }

    _workAreaFor(x, y) {
        const monitors = Main.layoutManager.monitors;
        let index = Main.layoutManager.primaryIndex;
        monitors.forEach((m, i) => {
            if (x >= m.x && x < m.x + m.width && y >= m.y && y < m.y + m.height)
                index = i;
        });
        return Main.layoutManager.getWorkAreaForMonitor(index);
    }

    _size() {
        const [, w] = this.get_preferred_width(-1);
        const [, h] = this.get_preferred_height(w);
        return [w, h];
    }

    _place() {
        const settings = this._client.settings?.widget ?? {};
        const [w, h] = this._size();
        let x = settings.x ?? -1, y = settings.y ?? -1;
        if (x < 0 || y < 0) {
            const wa = Main.layoutManager.getWorkAreaForMonitor(Main.layoutManager.primaryIndex);
            x = wa.x + wa.width - w - MARGIN;
            y = wa.y + MARGIN;
        }
        const wa = this._workAreaFor(x + w / 2, y + h / 2);
        x = Math.min(Math.max(x, wa.x), wa.x + wa.width - w);
        y = Math.min(Math.max(y, wa.y), wa.y + wa.height - h);
        this.set_position(Math.round(x), Math.round(y));
    }

    // -- dragging -----------------------------------------------------------------
    _onPress(event) {
        const button = event.get_button();
        if (button === Clutter.BUTTON_SECONDARY) {
            this._menu.toggle();
            return Clutter.EVENT_STOP;
        }
        if (button !== Clutter.BUTTON_PRIMARY)
            return Clutter.EVENT_PROPAGATE;
        const [sx, sy] = event.get_coords();
        this._drag = {sx, sy, x: this.x, y: this.y, moved: false};
        this._grab = global.stage.grab(this);
        this.add_style_class_name('cg-dragging');
        return Clutter.EVENT_STOP;
    }

    _onMotion(event) {
        if (!this._drag)
            return Clutter.EVENT_PROPAGATE;
        const [sx, sy] = event.get_coords();
        const dx = sx - this._drag.sx, dy = sy - this._drag.sy;
        if (!this._drag.moved && Math.hypot(dx, dy) < DRAG_THRESHOLD)
            return Clutter.EVENT_STOP;
        this._drag.moved = true;
        this.set_position(Math.round(this._drag.x + dx), Math.round(this._drag.y + dy));
        return Clutter.EVENT_STOP;
    }

    _onRelease(event) {
        if (!this._drag || event.get_button() !== Clutter.BUTTON_PRIMARY)
            return Clutter.EVENT_PROPAGATE;
        const moved = this._drag.moved;
        this._endDrag();
        if (!moved) {
            this._openDetails();
            return Clutter.EVENT_STOP;
        }
        // Snap to the work area's edges when close to them.
        const [w, h] = this._size();
        const wa = this._workAreaFor(this.x + w / 2, this.y + h / 2);
        let x = this.x, y = this.y;
        if (Math.abs(x - wa.x) < SNAP + MARGIN)
            x = wa.x + MARGIN;
        if (Math.abs(wa.x + wa.width - (x + w)) < SNAP + MARGIN)
            x = wa.x + wa.width - w - MARGIN;
        if (Math.abs(y - wa.y) < SNAP + MARGIN)
            y = wa.y + MARGIN;
        if (Math.abs(wa.y + wa.height - (y + h)) < SNAP + MARGIN)
            y = wa.y + wa.height - h - MARGIN;
        x = Math.min(Math.max(x, wa.x), wa.x + wa.width - w);
        y = Math.min(Math.max(y, wa.y), wa.y + wa.height - h);
        this.ease({x: Math.round(x), y: Math.round(y), duration: 160,
            mode: Clutter.AnimationMode.EASE_OUT_QUAD});
        this._client.setSetting('widget', {x: Math.round(x), y: Math.round(y)});
        return Clutter.EVENT_STOP;
    }

    _endDrag() {
        this._grab?.dismiss();
        this._grab = null;
        this._drag = null;
        this.remove_style_class_name('cg-dragging');
    }

    destroy() {
        this._endDrag();
        if (this._historyTimer) {
            GLib.source_remove(this._historyTimer);
            this._historyTimer = 0;
        }
        for (const id of this._clientIds)
            this._client.disconnect(id);
        this._clientIds = [];
        Main.layoutManager.disconnect(this._monitorsId);
        this._interface.disconnect(this._interfaceId);
        this._interface = null;
        this._menu.destroy();
        this._detach();
        super.destroy();
    }
});
