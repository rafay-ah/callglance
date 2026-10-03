// D-Bus client for the CallGlance service (all calls are asynchronous: never
// block the compositor on another process).

import Gio from 'gi://Gio';

import * as Signals from 'resource:///org/gnome/shell/misc/signals.js';

const BUS_NAME = 'io.github.rafay_ah.CallGlance';
const OBJECT_PATH = '/io/github/rafay_ah/CallGlance';
const IFACE = 'io.github.rafay_ah.CallGlance1';

const IFACE_XML = `<node>
  <interface name="${IFACE}">
    <method name="GetSnapshot"><arg type="s" direction="out"/></method>
    <method name="GetHistory">
      <arg type="u" direction="in"/><arg type="s" direction="out"/>
    </method>
    <method name="GetSettings"><arg type="s" direction="out"/></method>
    <method name="SetSetting"><arg type="s" direction="in"/><arg type="s" direction="in"/></method>
    <method name="RunSpeedTest"/>
    <method name="CancelSpeedTest"/>
    <method name="RegisterPanel"/>
    <method name="UnregisterPanel"/>
    <method name="Show"/>
    <signal name="SnapshotChanged"><arg type="s"/></signal>
    <signal name="SettingsChanged"><arg type="s"/></signal>
    <signal name="ShowRequested"/>
  </interface>
</node>`;

const CallGlanceProxy = Gio.DBusProxy.makeProxyWrapper(IFACE_XML);

const HOUR = 3600;

function parseJson(text) {
    try {
        return JSON.parse(text);
    } catch {
        return null;
    }
}

export class CallGlanceClient extends Signals.EventEmitter {
    constructor() {
        super();
        this.snapshot = null;
        this.settings = null;
        this.history = [];
        this.connected = false;
        this._proxy = null;
        this._signalIds = [];
        this._cancellable = null;
        this._destroyed = false;
        this._historyPending = null;
        this._watchId = Gio.bus_watch_name(
            Gio.BusType.SESSION, BUS_NAME, Gio.BusNameWatcherFlags.NONE,
            () => this._onAppeared(), () => this._onVanished());
    }

    _onAppeared() {
        if (this._proxy || this._destroyed)
            return;
        this._cancellable = new Gio.Cancellable();
        // eslint-disable-next-line no-new
        new CallGlanceProxy(Gio.DBus.session, BUS_NAME, OBJECT_PATH, (proxy, error) => {
            if (this._destroyed)
                return;
            if (error) {
                console.warn(`CallGlance: cannot reach the service: ${error.message}`);
                return;
            }
            this._proxy = proxy;
            this._signalIds = [
                proxy.connectSignal('SnapshotChanged', (_p, _s, [json]) => this._setSnapshot(json)),
                proxy.connectSignal('SettingsChanged', (_p, _s, [json]) => this._setSettings(json)),
                proxy.connectSignal('ShowRequested', () => this.emit('show-requested')),
            ];
            this.connected = true;
            proxy.RegisterPanelRemote(() => {});
            proxy.GetSettingsRemote(([json] = [], err) => !err && this._setSettings(json));
            proxy.GetSnapshotRemote(([json] = [], err) => !err && this._setSnapshot(json));
            this.history = [];
            this.refreshHistory();
            this.emit('connection-changed', true);
        }, this._cancellable, Gio.DBusProxyFlags.DO_NOT_AUTO_START);
    }

    _onVanished() {
        this._dropProxy();
        this.snapshot = null;
        if (this.connected) {
            this.connected = false;
            this.emit('connection-changed', false);
        }
    }

    _dropProxy() {
        if (this._proxy) {
            for (const id of this._signalIds)
                this._proxy.disconnectSignal(id);
        }
        this._signalIds = [];
        this._proxy = null;
        this._cancellable?.cancel();
        this._cancellable = null;
    }

    _setSnapshot(json) {
        const snap = parseJson(json);
        if (!snap || !snap.level)
            return;
        this.snapshot = snap;
        this.emit('snapshot', snap);
    }

    _setSettings(json) {
        const settings = parseJson(json);
        if (!settings)
            return;
        this.settings = settings;
        this.emit('settings', settings);
    }

    // Fetch history and merge it with what we have; only the new part is
    // requested once we hold a full hour.
    refreshHistory() {
        if (!this._proxy || this._historyPending)
            return;
        const last = this.history.length ? this.history[this.history.length - 1].ts : 0;
        const now = Date.now() / 1000;
        const span = last ? Math.min(HOUR, Math.ceil(now - last) + 30) : HOUR;
        this._historyPending = true;
        this._proxy.GetHistoryRemote(Math.max(60, span), ([json] = [], err) => {
            this._historyPending = null;
            if (err || this._destroyed)
                return;
            const data = parseJson(json);
            if (!data || !data.fields)
                return;
            const rows = data.rows.map(row => Object.fromEntries(
                data.fields.map((field, i) => [field, row[i]])));
            const known = new Set(this.history.map(p => p.ts));
            const merged = this.history.concat(rows.filter(p => !known.has(p.ts)));
            merged.sort((a, b) => a.ts - b.ts);
            const horizon = Date.now() / 1000 - HOUR;
            this.history = merged.filter(p => p.ts >= horizon);
            this.emit('history', this.history);
        });
    }

    setSetting(key, value) {
        this._proxy?.SetSettingRemote(key, JSON.stringify(value), (_r, err) => {
            if (err)
                console.warn(`CallGlance: could not change ${key}: ${err.message}`);
        });
    }

    runSpeedTest() {
        this._proxy?.RunSpeedTestRemote(() => {});
    }

    cancelSpeedTest() {
        this._proxy?.CancelSpeedTestRemote(() => {});
    }

    destroy() {
        this._destroyed = true;
        if (this._proxy)
            this._proxy.UnregisterPanelRemote(() => {});
        this._dropProxy();
        Gio.bus_unwatch_name(this._watchId);
        this.disconnectAll();
    }
}
