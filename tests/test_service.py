"""The D-Bus service, on a private session bus."""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NAME, PATH, IFACE = "io.github.rafay_ah.CallGlance", "/io/github/rafay_ah/CallGlance", \
    "io.github.rafay_ah.CallGlance1"

pytestmark = pytest.mark.skipif(not shutil.which("dbus-daemon"), reason="needs dbus-daemon")


@pytest.fixture
def bus(tmp_path):
    proc = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--print-address"],
                            stdout=subprocess.PIPE, text=True)
    address = proc.stdout.readline().strip()
    env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=address, PYTHONPATH=str(ROOT / "src"),
               XDG_CONFIG_HOME=str(tmp_path / "config"), XDG_STATE_HOME=str(tmp_path / "state"),
               XDG_DATA_HOME=str(tmp_path / "data"), XDG_CURRENT_DESKTOP="Test")
    conn = Gio.DBusConnection.new_for_address_sync(
        address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
    yield conn, env
    conn.close_sync(None)
    proc.terminate()
    proc.wait()


def call(conn, method, params=None, iface=IFACE):
    reply = conn.call_sync(NAME, PATH, iface, method, params, None,
                           Gio.DBusCallFlags.NO_AUTO_START, 5000, None)
    return reply.unpack()


def wait_for_name(conn, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        owner = conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                               "org.freedesktop.DBus", "NameHasOwner",
                               GLib.Variant("(s)", (NAME,)), None, 0, 1000, None).unpack()[0]
        if owner:
            return True
        time.sleep(0.2)
    return False


def test_service_api(bus):
    conn, env = bus
    service = subprocess.Popen([sys.executable, "-m", "callglance", "--demo", "--background"],
                               env=env)
    try:
        assert wait_for_name(conn)
        snap = {}
        for _ in range(50):
            snap = json.loads(call(conn, "GetSnapshot")[0] or "{}")
            if snap.get("level"):
                break
            time.sleep(0.2)
        assert snap["level"] in ("good", "unknown") and snap["demo"] is True
        history = json.loads(call(conn, "GetHistory", GLib.Variant("(u)", (3600,)))[0])
        assert history["fields"][0] == "ts" and len(history["rows"]) >= 700

        call(conn, "SetSetting", GLib.Variant("(ss)", ("widget", json.dumps({"enabled": True}))))
        settings = json.loads(call(conn, "GetSettings")[0])
        assert settings["widget"]["enabled"] is True and settings["demo"] is True
        with pytest.raises(GLib.Error, match="InvalidArgs"):
            call(conn, "SetSetting", GLib.Variant("(ss)", ("interval", "1")))
        with pytest.raises(GLib.Error, match="InvalidArgs"):
            call(conn, "SetSetting", GLib.Variant("(ss)", ("notifications", '"yes"')))

        props = call(conn, "GetAll", GLib.Variant("(s)", (IFACE,)),
                     iface="org.freedesktop.DBus.Properties")[0]
        assert props["PanelRegistered"] is False and props["Demo"] is True
        call(conn, "RegisterPanel")
        props = call(conn, "GetAll", GLib.Variant("(s)", (IFACE,)),
                     iface="org.freedesktop.DBus.Properties")[0]
        assert props["PanelRegistered"] is True

        # A second launch hands over to the running instance and exits.
        second = subprocess.run([sys.executable, "-m", "callglance", "--background"], env=env,
                                timeout=20)
        assert second.returncode == 0

        call(conn, "Quit")
        assert service.wait(10) == 0
    finally:
        if service.poll() is None:
            service.kill()
