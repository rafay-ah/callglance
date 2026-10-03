"""Unprivileged network probes.

Nothing here needs root or extra capabilities:

* ``IcmpEcho`` uses Linux "ping sockets" (``SOCK_DGRAM`` + ``IPPROTO_ICMP``). The
  kernel allows them for groups inside ``net.ipv4.ping_group_range``; systemd's
  default (``0 2147483647``) allows everyone, which covers Ubuntu, Fedora, Arch...
* ``DnsQuery`` times a plain UDP DNS query. Public resolvers and most home routers
  answer them, so they are the first fallback when ping sockets are not allowed.
* ``TcpConnect`` times a TCP handshake (SYN -> SYN/ACK or RST).
* ``IcmpTtl`` / ``UdpTtl`` send TTL-limited packets and read the router's
  "time exceeded" reply from the socket error queue (``IP_RECVERR``), which is how
  ``tracepath`` discovers hops without privileges.

Every probe reports replies through a callback with the round-trip time in
milliseconds. Kernel receive timestamps (``SO_TIMESTAMPNS``) are used when
available so that Python's own scheduling noise does not show up as jitter.
"""

from __future__ import annotations

import errno
import os
import socket
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

SOL_IP = getattr(socket, "SOL_IP", 0)
IP_RECVERR = getattr(socket, "IP_RECVERR", 11)
MSG_ERRQUEUE = getattr(socket, "MSG_ERRQUEUE", 0x2000)
SO_TIMESTAMPNS = getattr(socket, "SO_TIMESTAMPNS", 35)
SCM_TIMESTAMPNS = SO_TIMESTAMPNS
SO_EE_ORIGIN_ICMP = 2

ICMP_ECHO_REPLY = 0
ICMP_DEST_UNREACH = 3
ICMP_ECHO_REQUEST = 8
ICMP_TIME_EXCEEDED = 11

TRACE_BASE_PORT = 33434
PAYLOAD = b"CallGlance probe:" + bytes(range(15))  # 32 bytes, recognisable in captures

ANC_BUFSIZE = 512


@dataclass
class Reply:
    seq: int
    rtt_ms: float
    kind: str = "reply"  # "reply" (target answered) or "ttl" (a router on the way answered)
    addr: str | None = None


ReplyCallback = Callable[[Reply], None]


def ping_sockets_allowed() -> bool:
    """True if this process may open an unprivileged ICMP socket."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
    except OSError:
        return False
    sock.close()
    return True


def ping_group_range(path: str = "/proc/sys/net/ipv4/ping_group_range") -> tuple[int, int] | None:
    try:
        with open(path) as fh:
            low, high = fh.read().split()[:2]
        return int(low), int(high)
    except (OSError, ValueError):
        return None


def _enable_timestamps(sock: socket.socket) -> bool:
    try:
        sock.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
        return True
    except OSError:
        return False


def kernel_timestamp(ancdata: list[tuple[int, int, bytes]]) -> float | None:
    """Extract the SO_TIMESTAMPNS receive time (CLOCK_REALTIME seconds)."""
    for level, ctype, data in ancdata:
        if level == socket.SOL_SOCKET and ctype == SCM_TIMESTAMPNS:
            if len(data) >= 16:
                sec, nsec = struct.unpack("@qq", data[:16])
            elif len(data) >= 8:
                sec, nsec = struct.unpack("@ii", data[:8])
            else:
                continue
            return sec + nsec / 1e9
    return None


@dataclass
class QueuedError:
    origin: int
    icmp_type: int
    icmp_code: int
    offender: str | None
    data: bytes
    dest: Any
    timestamp: float | None


def parse_errqueue(data: bytes, ancdata: list[tuple[int, int, bytes]], dest: Any) -> QueuedError | None:
    """Decode a ``struct sock_extended_err`` (+ offender address) from IP_RECVERR."""
    stamp = kernel_timestamp(ancdata)
    for level, ctype, cdata in ancdata:
        if level == SOL_IP and ctype == IP_RECVERR and len(cdata) >= 16:
            _errno, origin, icmp_type, icmp_code, _pad, _info, _data = struct.unpack(
                "=IBBBBII", cdata[:16]
            )
            offender = None
            if len(cdata) >= 24:
                (family,) = struct.unpack("=H", cdata[16:18])
                if family == socket.AF_INET:
                    offender = socket.inet_ntoa(cdata[20:24])
            return QueuedError(origin, icmp_type, icmp_code, offender, data, dest, stamp)
    return None


class Probe:
    """Base class: book-keeping of send times and RTT computation."""

    kind = "probe"

    def __init__(self) -> None:
        self._sent: dict[int, tuple[float, float]] = {}
        self._loop: Any = None
        self._on_reply: ReplyCallback | None = None

    def attach(self, loop: Any, on_reply: ReplyCallback) -> None:
        self._loop = loop
        self._on_reply = on_reply

    def detach(self) -> None:
        self._sent.clear()
        self._on_reply = None

    def send(self, seq: int) -> bool:
        raise NotImplementedError

    def forget(self, seq: int) -> None:
        """Called by the owner when a probe timed out."""
        self._sent.pop(seq, None)

    def describe(self) -> str:
        return self.kind

    # helpers --------------------------------------------------------------
    def _stamp(self, seq: int) -> None:
        self._sent[seq] = (time.monotonic(), time.time())

    def _rtt(self, seq: int, kernel_ts: float | None) -> float | None:
        sent = self._sent.pop(seq, None)
        if sent is None:
            return None
        rtt = (time.monotonic() - sent[0]) * 1000.0
        if kernel_ts is not None:
            krtt = (kernel_ts - sent[1]) * 1000.0
            # The kernel stamped the packet before we woke up, so a sane kernel
            # RTT is never larger than the user-space one (allow clock skew noise).
            if 0.0 <= krtt <= rtt + 1.0:
                rtt = krtt
        return rtt

    def _emit(self, reply: Reply) -> None:
        if self._on_reply is not None:
            self._on_reply(reply)


class _SocketProbe(Probe):
    """A probe backed by a single non-blocking datagram socket."""

    sock: socket.socket

    def attach(self, loop: Any, on_reply: ReplyCallback) -> None:
        super().attach(loop, on_reply)
        loop.add_reader(self.sock, self._on_readable)

    def detach(self) -> None:
        if self._loop is not None:
            try:
                self._loop.remove_reader(self.sock)
            except (KeyError, ValueError, OSError):
                pass
        try:
            self.sock.close()
        except OSError:
            pass
        super().detach()

    def _on_readable(self) -> None:
        raise NotImplementedError

    def _read_errqueue(self) -> list[QueuedError]:
        errors = []
        for _ in range(64):
            try:
                data, anc, _flags, dest = self.sock.recvmsg(512, ANC_BUFSIZE, MSG_ERRQUEUE)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                break
            err = parse_errqueue(data, anc, dest)
            if err is not None:
                errors.append(err)
        return errors


class IcmpEcho(_SocketProbe):
    """ICMP echo through an unprivileged ping socket.

    With ``ttl`` set, the packet is TTL-limited and the "time exceeded" error
    from the router at that distance is reported instead (``expect`` filters on
    the router address so a path change does not mix up hops).
    """

    kind = "icmp"

    def __init__(self, dest: str, ttl: int | None = None, expect: str | None = None) -> None:
        super().__init__()
        self.dest = dest
        self.ttl = ttl
        self.expect = expect
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
        self.sock.setblocking(False)
        _enable_timestamps(self.sock)
        if ttl is not None:
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, ttl)
            self.sock.setsockopt(SOL_IP, IP_RECVERR, 1)

    def describe(self) -> str:
        return "icmp" if self.ttl is None else f"icmp-ttl{self.ttl}"

    def send(self, seq: int) -> bool:
        packet = struct.pack("!BBHHH", ICMP_ECHO_REQUEST, 0, 0, 0, seq & 0xFFFF) + PAYLOAD
        for attempt in range(2):
            self._stamp(seq)
            try:
                self.sock.sendto(packet, (self.dest, 0))
                return True
            except OSError as exc:
                # With IP_RECVERR a queued ICMP error is also reported by the next
                # syscall; drain the queue and retry once.
                if attempt == 0 and self.ttl is not None and exc.errno in _TRANSIENT_ERRNOS:
                    self._handle_errors(self._read_errqueue())
                    continue
                self.forget(seq)
                return False
        return False

    def _handle_errors(self, errors: list[QueuedError]) -> None:
        for err in errors:
            if err.origin != SO_EE_ORIGIN_ICMP or err.icmp_type != ICMP_TIME_EXCEEDED:
                continue
            if self.expect is not None and err.offender != self.expect:
                continue
            if len(err.data) < 8:
                continue
            (seq,) = struct.unpack("!H", err.data[6:8])
            rtt = self._rtt(seq, err.timestamp)
            if rtt is not None:
                self._emit(Reply(seq, rtt, "ttl", err.offender))

    def _on_readable(self) -> None:
        if self.ttl is not None:
            self._handle_errors(self._read_errqueue())
        for _ in range(64):
            try:
                data, anc, _flags, addr = self.sock.recvmsg(2048, ANC_BUFSIZE)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                if self.ttl is not None:
                    self._handle_errors(self._read_errqueue())
                break
            if len(data) < 8 or data[0] != ICMP_ECHO_REPLY:
                continue
            (seq,) = struct.unpack("!H", data[6:8])
            rtt = self._rtt(seq, kernel_timestamp(anc))
            if rtt is not None:
                self._emit(Reply(seq, rtt, "reply", addr[0] if addr else self.dest))


class UdpTtl(_SocketProbe):
    """TTL-limited UDP datagrams, timed by the router's ICMP "time exceeded".

    Works without ping sockets; the destination port encodes the sequence number
    because routers are only required to quote 8 bytes of the original datagram.
    """

    kind = "udp-ttl"

    def __init__(self, dest: str, ttl: int, expect: str | None = None) -> None:
        super().__init__()
        self.dest = dest
        self.ttl = ttl
        self.expect = expect
        self._ports: dict[int, int] = {}
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        _enable_timestamps(self.sock)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, ttl)
        self.sock.setsockopt(SOL_IP, IP_RECVERR, 1)

    def describe(self) -> str:
        return f"udp-ttl{self.ttl}"

    def send(self, seq: int) -> bool:
        port = TRACE_BASE_PORT + (seq % 128)
        self._ports[port] = seq
        for attempt in range(2):
            self._stamp(seq)
            try:
                self.sock.sendto(PAYLOAD, (self.dest, port))
                return True
            except OSError as exc:
                if attempt == 0 and exc.errno in _TRANSIENT_ERRNOS:
                    self._handle_errors(self._read_errqueue())
                    continue
                self.forget(seq)
                return False
        return False

    def _handle_errors(self, errors: list[QueuedError]) -> None:
        for err in errors:
            if err.origin != SO_EE_ORIGIN_ICMP:
                continue
            if err.icmp_type not in (ICMP_TIME_EXCEEDED, ICMP_DEST_UNREACH):
                continue
            if self.expect is not None and err.offender != self.expect:
                continue
            port = err.dest[1] if isinstance(err.dest, tuple) and len(err.dest) > 1 else None
            seq = self._ports.pop(port, None) if port is not None else None
            if seq is None:
                continue
            rtt = self._rtt(seq, err.timestamp)
            if rtt is not None:
                kind = "ttl" if err.icmp_type == ICMP_TIME_EXCEEDED else "reply"
                self._emit(Reply(seq, rtt, kind, err.offender))

    def _on_readable(self) -> None:
        self._handle_errors(self._read_errqueue())
        # Drain anything that arrived on the normal queue (nothing should).
        for _ in range(16):
            try:
                self.sock.recv(2048)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                self._handle_errors(self._read_errqueue())
                break


# -- DNS -----------------------------------------------------------------

def build_dns_query(query_id: int, name: str, qtype: int = 1) -> bytes:
    """A minimal recursive DNS query (RFC 1035) for ``name``."""
    header = struct.pack("!HHHHHH", query_id & 0xFFFF, 0x0100, 1, 0, 0, 0)
    labels = b"".join(
        bytes([len(part)]) + part.encode("idna") for part in name.strip(".").split(".") if part
    )
    return header + labels + b"\0" + struct.pack("!HH", qtype, 1)


def parse_dns_response(data: bytes) -> tuple[int, int, int] | None:
    """Return (id, rcode, answer_count) for a DNS response, None if not a response."""
    if len(data) < 12:
        return None
    query_id, flags, _qd, ancount, _ns, _ar = struct.unpack("!HHHHHH", data[:12])
    if not flags & 0x8000:  # QR bit: must be a response
        return None
    return query_id, flags & 0x000F, ancount


class DnsQuery(_SocketProbe):
    """Times a UDP DNS query. Any answer (even NXDOMAIN) counts as a reply."""

    kind = "dns"

    def __init__(self, server: str, name: str = "google.com", port: int = 53) -> None:
        super().__init__()
        self.server = server
        self.name = name
        self.port = port
        self.refused = False
        family = socket.AF_INET6 if ":" in server else socket.AF_INET
        self.sock = socket.socket(family, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        _enable_timestamps(self.sock)
        self.sock.connect((server, port))

    def describe(self) -> str:
        return "dns"

    def send(self, seq: int, name: str | None = None) -> bool:
        self._stamp(seq)
        try:
            self.sock.send(build_dns_query(seq, name or self.name))
            return True
        except OSError as exc:
            if exc.errno == errno.ECONNREFUSED:
                self.refused = True
            self.forget(seq)
            return False

    def _on_readable(self) -> None:
        for _ in range(64):
            try:
                data, anc, _flags, _addr = self.sock.recvmsg(4096, ANC_BUFSIZE)
            except (BlockingIOError, InterruptedError):
                break
            except OSError as exc:
                if exc.errno == errno.ECONNREFUSED:
                    self.refused = True  # nothing listens on port 53 there
                break
            parsed = parse_dns_response(data)
            if parsed is None:
                continue
            seq = parsed[0]
            rtt = self._rtt(seq, kernel_timestamp(anc))
            if rtt is not None:
                self._emit(Reply(seq, rtt, "reply", self.server))


# -- TCP -----------------------------------------------------------------

class TcpConnect(Probe):
    """Times a TCP handshake; a refused connection (RST) is also an answer."""

    kind = "tcp"

    def __init__(self, dest: str, port: int) -> None:
        super().__init__()
        self.dest = dest
        self.port = port
        self._socks: dict[int, socket.socket] = {}

    def describe(self) -> str:
        return f"tcp:{self.port}"

    def send(self, seq: int) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        # Close with RST: no TIME_WAIT pile-up and nothing for the far end to keep.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        self._stamp(seq)
        err = sock.connect_ex((self.dest, self.port))
        if err == 0:
            rtt = self._rtt(seq, None)
            sock.close()
            if rtt is not None:
                self._emit(Reply(seq, rtt, "reply", self.dest))
            return True
        if err not in (errno.EINPROGRESS, errno.EALREADY, errno.EWOULDBLOCK):
            sock.close()
            if err == errno.ECONNREFUSED:
                rtt = self._rtt(seq, None)
                if rtt is not None:
                    self._emit(Reply(seq, rtt, "reply", self.dest))
                return True
            self.forget(seq)
            return False
        self._socks[seq] = sock
        self._loop.add_writer(sock, lambda: self._on_connected(seq))
        return True

    def _on_connected(self, seq: int) -> None:
        sock = self._socks.pop(seq, None)
        if sock is None:
            return
        self._loop.remove_writer(sock)
        err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        sock.close()
        if err in (0, errno.ECONNREFUSED):
            rtt = self._rtt(seq, None)
            if rtt is not None:
                self._emit(Reply(seq, rtt, "reply", self.dest))
        else:
            self.forget(seq)

    def forget(self, seq: int) -> None:
        super().forget(seq)
        sock = self._socks.pop(seq, None)
        if sock is not None:
            if self._loop is not None:
                self._loop.remove_writer(sock)
            sock.close()

    def detach(self) -> None:
        for seq in list(self._socks):
            self.forget(seq)
        super().detach()


_TRANSIENT_ERRNOS = {
    errno.EHOSTUNREACH,
    errno.ENETUNREACH,
    errno.ECONNREFUSED,
    errno.EPROTO,
    errno.EMSGSIZE,
}


def local_address_for(dest: str) -> str | None:
    """The source address the kernel would use to reach ``dest`` (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((dest, 9))
            return sock.getsockname()[0]
    except OSError:
        return None


def resolve_ipv4(host: str) -> str | None:
    try:
        return socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
    except (OSError, IndexError):
        return None


def is_root() -> bool:
    return os.geteuid() == 0
