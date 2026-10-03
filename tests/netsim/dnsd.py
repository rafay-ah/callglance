#!/usr/bin/env python3
"""A tiny DNS responder for tests: answers every A query with 192.0.2.10."""

import socket
import struct
import sys


def answer(query: bytes) -> bytes | None:
    if len(query) < 12:
        return None
    qid, flags, qdcount = struct.unpack("!HHH", query[:6])
    end = 12
    while end < len(query) and query[end] != 0:
        end += query[end] + 1
    question = query[12:end + 5]
    header = struct.pack("!HHHHHH", qid, 0x8180 | (flags & 0x0100), 1, 1, 0, 0)
    record = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + socket.inet_aton("192.0.2.10")
    return header + question + record


def main(addresses: list[str]) -> None:
    socks = []
    for addr in addresses:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((addr, 53))
        socks.append(sock)
    import select

    while True:
        ready, _, _ = select.select(socks, [], [])
        for sock in ready:
            data, peer = sock.recvfrom(4096)
            reply = answer(data)
            if reply:
                sock.sendto(reply, peer)


if __name__ == "__main__":
    main(sys.argv[1:])
