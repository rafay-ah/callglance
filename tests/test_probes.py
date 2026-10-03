import socket
import struct
import threading
import time

import pytest

from callglance.loop import Loop
from callglance.probes import (
    CLASS_CHAOS,
    DNS_TXT,
    IP_RECVERR,
    SOL_IP,
    DnsQuery,
    IcmpEcho,
    TcpConnect,
    build_dns_query,
    kernel_timestamp,
    parse_dns_response,
    parse_errqueue,
    parse_txt_answer,
    ping_sockets_allowed,
)


def test_dns_query_layout():
    q = build_dns_query(0x1234, "zoom.us")
    assert q[:2] == b"\x12\x34"
    assert q[12:] == b"\x04zoom\x02us\x00\x00\x01\x00\x01"
    chaos = build_dns_query(1, "id.server", DNS_TXT, CLASS_CHAOS)
    assert chaos.endswith(b"\x00\x00\x10\x00\x03")


def test_dns_response_parsing():
    assert parse_dns_response(b"\x00" * 5) is None
    assert parse_dns_response(build_dns_query(7, "a.b")) is None  # a query, not a response
    reply = struct.pack("!HHHHHH", 7, 0x8183, 1, 0, 0, 0)
    assert parse_dns_response(reply) == (7, 3, 0)


def _txt_reply(text: bytes) -> bytes:
    question = b"\x02id\x06server\x00" + struct.pack("!HH", 16, 3)
    answer = b"\xc0\x0c" + struct.pack("!HHIH", 16, 3, 0, len(text) + 1) + bytes([len(text)]) + text
    return struct.pack("!HHHHHH", 1, 0x8400, 1, 1, 0, 0) + question + answer


def test_txt_answer_parsing():
    assert parse_txt_answer(_txt_reply(b"AMS")) == "AMS"
    assert parse_txt_answer(struct.pack("!HHHHHH", 1, 0x8004, 0, 0, 0, 0)) is None


def test_errqueue_parsing():
    ee = struct.pack("=IBBBBII", 113, 2, 11, 0, 0, 0, 0)
    offender = struct.pack("=HH", socket.AF_INET, 0) + socket.inet_aton("100.64.0.1") + b"\0" * 8
    err = parse_errqueue(b"payload", [(SOL_IP, IP_RECVERR, ee + offender)], ("1.1.1.1", 33434))
    assert err.origin == 2 and err.icmp_type == 11 and err.offender == "100.64.0.1"
    assert parse_errqueue(b"", [], None) is None


def test_kernel_timestamp_parsing():
    data = struct.pack("@qq", 1700000000, 500_000_000)
    assert kernel_timestamp([(socket.SOL_SOCKET, 35, data)]) == pytest.approx(1700000000.5)
    assert kernel_timestamp([]) is None


def run_probe(probe, sends=3, wait=1.0):
    loop = Loop()
    replies = []
    probe.attach(loop, replies.append)
    for seq in range(1, sends + 1):
        loop.call_later(0.01 * seq, probe.send, seq)
    loop.call_later(wait, loop.stop)
    thread = threading.Thread(target=loop.run)
    thread.start()
    thread.join(5)
    probe.detach()
    loop.close()
    return replies


def test_tcp_connect_to_a_closed_port_counts_as_an_answer():
    # Grab a free port and close it: the kernel answers SYN with RST.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    replies = run_probe(TcpConnect("127.0.0.1", port))
    assert {r.seq for r in replies} == {1, 2, 3}
    assert all(0 <= r.rtt_ms < 100 for r in replies)


def test_tcp_connect_to_a_listening_port():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    try:
        replies = run_probe(TcpConnect("127.0.0.1", server.getsockname()[1]))
    finally:
        server.close()
    assert len(replies) == 3


def test_dns_query_against_a_local_responder():
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    stop = threading.Event()

    def respond():
        server.settimeout(0.1)
        while not stop.is_set():
            try:
                data, peer = server.recvfrom(512)
            except TimeoutError:
                continue
            reply = bytearray(data)
            reply[2] |= 0x80  # QR: response
            server.sendto(bytes(reply), peer)

    t = threading.Thread(target=respond)
    t.start()
    try:
        replies = run_probe(DnsQuery("127.0.0.1", port=port))
    finally:
        stop.set()
        t.join()
        server.close()
    assert {r.seq for r in replies} == {1, 2, 3}


@pytest.mark.skipif(not ping_sockets_allowed(), reason="unprivileged ICMP not allowed here")
def test_icmp_echo_to_localhost():
    replies = run_probe(IcmpEcho("127.0.0.1"))
    assert {r.seq for r in replies} == {1, 2, 3}
    assert all(r.rtt_ms < 50 for r in replies)


def test_loop_timers_and_threadsafe_calls():
    loop = Loop()
    seen = []
    loop.call_later(0.05, seen.append, "timer")
    handle = loop.call_later(0.02, seen.append, "cancelled")
    handle.cancel()
    t = threading.Thread(target=loop.run)
    t.start()
    loop.call_soon_threadsafe(seen.append, "threadsafe")
    time.sleep(0.15)
    loop.stop()
    t.join(2)
    loop.close()
    assert seen == ["threadsafe", "timer"]
