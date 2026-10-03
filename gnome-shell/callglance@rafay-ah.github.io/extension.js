// CallGlance: connection quality for calls, at a glance.
//
// The measuring happens in the CallGlance app (a small Python service); this
// extension only shows its results: the top-bar dot with its popover, and the
// optional desktop widget.

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {CallGlanceClient} from './client.js';
import {Indicator} from './indicator.js';
import {DesktopWidget} from './widget.js';

export default class CallGlanceExtension extends Extension {
    enable() {
        this._client = new CallGlanceClient();
        this._indicator = new Indicator(this._client, {
            setWidget: enabled => this._client.setSetting('widget', {enabled}),
            setPinned: onTop => this._client.setSetting('widget', {on_top: onTop}),
        });
        Main.panel.addToStatusArea(this.uuid, this._indicator);
        this._widget = null;
        this._clientIds = [
            this._client.connect('settings', () => this._syncWidget()),
            this._client.connect('connection-changed', () => this._syncWidget()),
        ];
        this._syncWidget();
    }

    _syncWidget() {
        const settings = this._client.settings?.widget;
        const wanted = this._client.connected && Boolean(settings?.enabled);
        if (wanted && !this._widget) {
            this._widget = new DesktopWidget(this._client, {
                openDetails: () => this._indicator.menu.open(true),
            });
        } else if (!wanted && this._widget) {
            this._widget.destroy();
            this._widget = null;
        }
        if (this._widget)
            this._widget.setOnTop(settings.on_top !== false);
    }

    disable() {
        for (const id of this._clientIds ?? [])
            this._client.disconnect(id);
        this._clientIds = [];
        this._widget?.destroy();
        this._widget = null;
        this._indicator?.destroy();
        this._indicator = null;
        this._client?.destroy();
        this._client = null;
    }
}
