"""Read the local network setup without root: default route, link type, DNS servers."""

from __future__ import annotations

import ipaddress
import os
import socket
import struct
from dataclasses import dataclass

RTF_UP = 0x0001
RTF_GATEWAY = 0x0002

KNOWN_RESOLVERS = {
    "1.1.1.1", "1.0.0.1",          # Cloudflare
    "8.8.8.8", "8.8.4.4",          # Google
    "9.9.9.9", "149.112.112.112",  # Quad9
    "208.67.222.222", "208.67.220.220",  # OpenDNS
}

CGNAT = ipaddress.ip_network("100.64.0.0/10")


@dataclass(frozen=True)
class Route:
    iface: str
    gateway: str | None
    metric: int


def _hex_to_ipv4(value: str) -> str:
    # /proc/net/route prints the raw __be32 as a host-order integer.
    return socket.inet_ntoa(struct.pack("=I", int(value, 16)))


def default_route(path: str = "/proc/net/route") -> Route | None:
    """The IPv4 default route with the lowest metric, if any."""
    best: Route | None = None
    try:
        with open(path) as fh:
            lines = fh.read().splitlines()[1:]
    except OSError:
        return None
    for line in lines:
        fields = line.split()
        if len(fields) < 8:
            continue
        iface, dest, gateway, flags_hex, _ref, _use, metric, mask = fields[:8]
        if dest != "00000000" or mask != "00000000":
            continue
        flags = int(flags_hex, 16)
        if not flags & RTF_UP:
            continue
        gw = _hex_to_ipv4(gateway) if flags & RTF_GATEWAY else None
        route = Route(iface, gw, int(metric))
        if best is None or route.metric < best.metric:
            best = route
    return best


def _read(path: str) -> str | None:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def iface_kind(iface: str, sysfs: str = "/sys/class/net") -> str:
    """Classify an interface: wifi, ethernet, vpn, mobile or other."""
    base = os.path.join(sysfs, iface)
    if os.path.isdir(os.path.join(base, "wireless")) or os.path.exists(
        os.path.join(base, "phy80211")
    ):
        return "wifi"
    if iface.startswith(("wwan", "ww")) or os.path.isdir(os.path.join(base, "device", "wwan")):
        return "mobile"
    if os.path.exists(os.path.join(base, "tun_flags")) or iface.startswith(
        ("tun", "tap", "wg", "ppp", "vpn", "ipsec", "nordlynx", "tailscale", "zt")
    ):
        return "vpn"
    if _read(os.path.join(base, "type")) == "1" and os.path.exists(os.path.join(base, "device")):
        return "ethernet"
    if iface.startswith(("en", "eth")):
        return "ethernet"
    return "other"


def iface_is_up(iface: str, sysfs: str = "/sys/class/net") -> bool:
    state = _read(os.path.join(sysfs, iface, "operstate"))
    # "unknown" is what many virtual and some USB links report while working.
    return state in ("up", "unknown", None)


def _nameservers(path: str) -> list[str]:
    servers: list[str] = []
    try:
        with open(path) as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    servers.append(parts[1].split("%")[0])
    except OSError:
        pass
    return servers


def dns_servers(
    paths: tuple[str, ...] = ("/run/systemd/resolve/resolv.conf", "/etc/resolv.conf"),
) -> list[str]:
    """Upstream DNS servers, skipping local stubs such as systemd-resolved's 127.0.0.53.

    systemd-resolved writes the real upstream servers to
    ``/run/systemd/resolve/resolv.conf`` (world readable), which is what we want to
    time: a lookup through the stub costs the same plus a cache hit.
    """
    for path in paths:
        upstream = [s for s in _nameservers(path) if not _is_loopback(s) and _is_ipv4(s)]
        if upstream:
            return upstream
    return []


def _is_ipv4(addr: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(addr), ipaddress.IPv4Address)
    except ValueError:
        return False


def _is_loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def is_private(addr: str | None) -> bool:
    """RFC 1918 / link-local / loopback: addresses that live inside a home network."""
    if not addr:
        return False
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if ip in CGNAT:
        return False  # carrier-grade NAT space belongs to the ISP
    return ip.is_private or ip.is_link_local or ip.is_loopback


def is_cgnat(addr: str | None) -> bool:
    try:
        return addr is not None and ipaddress.ip_address(addr) in CGNAT
    except ValueError:
        return False


def pick_isp_hop(hops: list[tuple[int, str | None]], gateway: str | None) -> tuple[int, str] | None:
    """Choose the ISP's first router from a traceroute-style hop list.

    ``hops`` is ``[(ttl, address or None), ...]`` toward a public target. The first
    hop is normally the home router. A second private hop is usually still at home
    (an ISP modem in front of your own router), so prefer the first hop that is
    public or in carrier-grade NAT space; fall back to the first responding hop
    after the gateway when everything we saw is private.
    """
    beyond = [(ttl, addr) for ttl, addr in sorted(hops) if addr and addr != gateway and ttl >= 2]
    for ttl, addr in beyond:
        if not is_private(addr):
            return ttl, addr
    return beyond[0] if beyond else None
