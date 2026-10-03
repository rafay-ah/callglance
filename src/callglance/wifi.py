"""Wi-Fi link details: NetworkManager over D-Bus, plus /proc/net/wireless for dBm.

NetworkManager exposes the SSID, frequency, signal strength (as a percentage)
and the current link bitrate. The kernel's ``/proc/net/wireless`` is world
readable and adds the signal level in dBm, which is what Wi-Fi guidance is
written in (-67 dBm is the usual floor for voice). Both are optional: whatever
is available is reported.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass

log = logging.getLogger(__name__)

NM_BUS = "org.freedesktop.NetworkManager"
NM_PATH = "/org/freedesktop/NetworkManager"
NM_IFACE = "org.freedesktop.NetworkManager"
NM_WIRELESS = "org.freedesktop.NetworkManager.Device.Wireless"
NM_AP = "org.freedesktop.NetworkManager.AccessPoint"
PROPS = "org.freedesktop.DBus.Properties"


@dataclass
class WifiInfo:
    iface: str
    ssid: str | None = None
    signal_dbm: int | None = None
    signal_pct: int | None = None
    frequency_mhz: int | None = None
    band: str | None = None
    channel: int | None = None
    bitrate_mbps: float | None = None
    max_bitrate_mbps: float | None = None
    standard: str | None = None  # e.g. "Wi-Fi 6" when `iw` tells us
    source: str = "none"
    quality: str = "unknown"  # excellent / good / fair / poor

    def as_dict(self) -> dict:
        return asdict(self)


def band_for(freq_mhz: int | None) -> str | None:
    if not freq_mhz:
        return None
    if 2400 <= freq_mhz < 2500:
        return "2.4 GHz"
    if 5925 <= freq_mhz <= 7125:
        return "6 GHz"
    if 4900 <= freq_mhz < 5925:
        return "5 GHz"
    if 57000 <= freq_mhz <= 71000:
        return "60 GHz"
    return None


def channel_for(freq_mhz: int | None) -> int | None:
    if not freq_mhz:
        return None
    if freq_mhz == 2484:
        return 14
    if 2412 <= freq_mhz < 2484:
        return (freq_mhz - 2407) // 5
    if 5955 <= freq_mhz <= 7115:
        return (freq_mhz - 5950) // 5
    if 4910 <= freq_mhz <= 5895:
        return (freq_mhz - 5000) // 5
    return None


def signal_quality(dbm: int | None, pct: int | None = None) -> str:
    """Bucket signal strength using common voice-grade Wi-Fi guidance."""
    if dbm is None and pct is not None:
        # NetworkManager's percentage is roughly linear between -100 and -50 dBm.
        dbm = int(-100 + pct / 2)
    if dbm is None:
        return "unknown"
    if dbm >= -60:
        return "excellent"
    if dbm >= -67:
        return "good"
    if dbm >= -75:
        return "fair"
    return "poor"


def read_proc_wireless(iface: str, path: str = "/proc/net/wireless") -> int | None:
    """Signal level in dBm from /proc/net/wireless, if the driver reports it."""
    try:
        with open(path) as fh:
            lines = fh.read().splitlines()[2:]
    except OSError:
        return None
    for line in lines:
        name, _, rest = line.partition(":")
        if name.strip() != iface:
            continue
        fields = rest.split()
        if len(fields) < 3:
            return None
        try:
            level = float(fields[2].rstrip("."))
        except ValueError:
            return None
        if level > 0:  # some drivers report an unsigned value offset by 256
            level -= 256
        return int(level) if -120 < level < 0 else None
    return None


_IW_FIELDS = {
    "ssid": re.compile(r"^\s*SSID:\s*(.+)$", re.M),
    "freq": re.compile(r"^\s*freq:\s*([\d.]+)", re.M),
    "signal": re.compile(r"^\s*signal:\s*(-?\d+)\s*dBm", re.M),
    "tx": re.compile(r"^\s*tx bitrate:\s*([\d.]+)\s*MBit/s(.*)$", re.M),
}


def parse_iw_link(text: str) -> dict:
    out: dict = {}
    if "Not connected" in text:
        return out
    m = _IW_FIELDS["ssid"].search(text)
    if m:
        out["ssid"] = m.group(1).strip()
    m = _IW_FIELDS["freq"].search(text)
    if m:
        out["frequency_mhz"] = int(float(m.group(1)))
    m = _IW_FIELDS["signal"].search(text)
    if m:
        out["signal_dbm"] = int(m.group(1))
    m = _IW_FIELDS["tx"].search(text)
    if m:
        out["bitrate_mbps"] = float(m.group(1))
        flags = m.group(2)
        if "EHT-MCS" in flags:
            out["standard"] = "Wi-Fi 7"
        elif "HE-MCS" in flags:
            out["standard"] = "Wi-Fi 6"
        elif "VHT-MCS" in flags:
            out["standard"] = "Wi-Fi 5"
        elif " MCS" in flags:
            out["standard"] = "Wi-Fi 4"
    return out


class WifiReader:
    """Collects Wi-Fi details for an interface. Safe to call from any thread."""

    def __init__(self) -> None:
        self._bus = None
        self._bus_failed = False
        self._iw = shutil.which("iw")
        self._iw_cache: dict[str, tuple[float, dict]] = {}

    def _system_bus(self):
        if self._bus is None and not self._bus_failed:
            try:
                import gi

                gi.require_version("Gio", "2.0")
                from gi.repository import Gio

                self._bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            except Exception as exc:  # no PyGObject, no system bus, sandbox...
                log.debug("NetworkManager unavailable: %s", exc)
                self._bus_failed = True
        return self._bus

    def _get_props(self, path: str, iface: str) -> dict:
        from gi.repository import GLib

        bus = self._system_bus()
        reply = bus.call_sync(
            NM_BUS, path, PROPS, "GetAll", GLib.Variant("(s)", (iface,)),
            GLib.VariantType("(a{sv})"), 0, 1000, None,
        )
        return reply.unpack()[0]

    def _from_networkmanager(self, info: WifiInfo) -> bool:
        bus = self._system_bus()
        if bus is None:
            return False
        try:
            from gi.repository import GLib

            reply = bus.call_sync(
                NM_BUS, NM_PATH, NM_IFACE, "GetDeviceByIpIface",
                GLib.Variant("(s)", (info.iface,)), GLib.VariantType("(o)"), 0, 1000, None,
            )
            device = reply.unpack()[0]
            wireless = self._get_props(device, NM_WIRELESS)
            ap_path = wireless.get("ActiveAccessPoint")
            bitrate = wireless.get("Bitrate")  # kbit/s
            if bitrate:
                info.bitrate_mbps = round(bitrate / 1000, 1)
            if not ap_path or ap_path == "/":
                info.source = "networkmanager"
                return True
            ap = self._get_props(ap_path, NM_AP)
            ssid = ap.get("Ssid")
            if ssid:
                info.ssid = bytes(ssid).decode("utf-8", "replace")
            info.frequency_mhz = ap.get("Frequency") or None
            strength = ap.get("Strength")
            info.signal_pct = int(strength) if strength is not None else None
            max_rate = ap.get("MaxBitrate")
            if max_rate:
                info.max_bitrate_mbps = round(max_rate / 1000, 1)
            info.source = "networkmanager"
            return True
        except Exception as exc:
            log.debug("NetworkManager query failed for %s: %s", info.iface, exc)
            return False

    def _from_iw(self, info: WifiInfo, max_age: float) -> None:
        """Fill gaps from `iw dev IFACE link` (cached: spawning a process is not free)."""
        if not self._iw:
            return
        cached = self._iw_cache.get(info.iface)
        if cached and time.monotonic() - cached[0] < max_age:
            parsed = cached[1]
        else:
            try:
                out = subprocess.run(
                    [self._iw, "dev", info.iface, "link"], capture_output=True, text=True,
                    timeout=2, check=False,
                ).stdout
            except (OSError, subprocess.SubprocessError):
                return
            parsed = parse_iw_link(out)
            self._iw_cache[info.iface] = (time.monotonic(), parsed)
        for key, value in parsed.items():
            if getattr(info, key, None) is None:
                setattr(info, key, value)
        if parsed and info.source == "none":
            info.source = "iw"

    def read(self, iface: str) -> WifiInfo:
        info = WifiInfo(iface=iface)
        have_nm = self._from_networkmanager(info)
        info.signal_dbm = read_proc_wireless(iface)
        # `iw` only adds the Wi-Fi generation when NetworkManager answered, so it
        # can be refreshed rarely; without NetworkManager it is the main source.
        self._from_iw(info, max_age=60.0 if have_nm else 10.0)
        info.band = band_for(info.frequency_mhz)
        info.channel = channel_for(info.frequency_mhz)
        info.quality = signal_quality(info.signal_dbm, info.signal_pct)
        return info
