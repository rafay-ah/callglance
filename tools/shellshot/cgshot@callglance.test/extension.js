// Test-only helper for a throwaway headless GNOME Shell: lets scripts evaluate
// JavaScript in the shell and save screenshots. NEVER install in a real session.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Shell from 'gi://Shell';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const XML = `<node><interface name="test.CallGlance.Shot">
  <method name="Eval"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
  <method name="Screenshot">
    <arg type="s" direction="in"/>
    <arg type="i" direction="in"/><arg type="i" direction="in"/>
    <arg type="i" direction="in"/><arg type="i" direction="in"/>
    <arg type="b" direction="out"/>
  </method>
</interface></node>`;

export default class Harness extends Extension {
    enable() {
        globalThis.cgMain = Main;
        // A virtual pointer for scripted clicks, drags and hovers.
        const seat = (global.stage.context?.get_backend() ?? Clutter.get_default_backend())
            .get_default_seat();
        const pointer = seat.create_virtual_device(Clutter.InputDeviceType.POINTER_DEVICE);
        const now = () => GLib.get_monotonic_time();
        globalThis.cgPointer = {
            move: (x, y) => pointer.notify_absolute_motion(now(), x, y),
            press: (button = 1) => pointer.notify_button(now(), button, Clutter.ButtonState.PRESSED),
            release: (button = 1) => pointer.notify_button(now(), button, Clutter.ButtonState.RELEASED),
        };
        this._impl = Gio.DBusExportedObject.wrapJSObject(XML, this);
        this._impl.export(Gio.DBus.session, '/test/CallGlance/Shot');
        this._owner = Gio.bus_own_name(Gio.BusType.SESSION, 'test.CallGlance.Shot',
            Gio.BusNameOwnerFlags.NONE, null, null, null);
    }

    Eval(code) {
        try {
            // eslint-disable-next-line no-eval
            const result = (0, eval)(code);
            return JSON.stringify(result === undefined ? null : String(result));
        } catch (e) {
            return JSON.stringify(`ERROR: ${e}\n${e.stack}`);
        }
    }

    ScreenshotAsync([path, x, y, w, h], invocation) {
        const shot = new Shell.Screenshot();
        let stream;
        try {
            stream = Gio.File.new_for_path(path).replace(null, false, Gio.FileCreateFlags.NONE, null);
        } catch (e) {
            invocation.return_value(new GLib.Variant('(b)', [false]));
            console.error(`cgshot: ${e}`);
            return;
        }
        const done = (obj, res) => {
            try {
                if (w > 0)
                    obj.screenshot_area_finish(res);
                else
                    obj.screenshot_finish(res);
                stream.close(null);
                invocation.return_value(new GLib.Variant('(b)', [true]));
            } catch (e) {
                invocation.return_value(new GLib.Variant('(b)', [false]));
                console.error(`cgshot: ${e}`);
            }
        };
        if (w > 0)
            shot.screenshot_area(x, y, w, h, stream, done);
        else
            shot.screenshot(false, stream, done);
        // A headless stage only paints when something changes; the screenshot is
        // taken on the next paint, so ask for one.
        global.stage.queue_redraw();
    }

    disable() {
        this._impl?.unexport();
        if (this._owner)
            Gio.bus_unown_name(this._owner);
        delete globalThis.cgMain;
        delete globalThis.cgPointer;
    }
}
