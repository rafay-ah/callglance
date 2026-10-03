#!/usr/bin/env python3
"""A tiny DNS responder for tests: answers every A query with 192.0.2.10."""

import socket
import struct
import sys


IDENTITY = None  # CHAOS id.server answer ("SIM" makes us look like Cloudflare)


def answer(query: bytes) -> bytes | None:
    if len(query) < 12:
        return None
    qid, flags, qdcount = struct.unpack("!HHH", query[:6])
    end = 12
    while end < len(query) and query[end] != 0:
        end += query[end] + 1
    question = query[12:end + 5]
    qtype, qclass = struct.unpack("!HH", query[end + 1:end + 5])
    rd = flags & 0x0100
    if qclass == 3:  # CHAOS: answer id.server like Cloudflare does, refuse the rest
        if IDENTITY and qtype == 16:
            txt = IDENTITY.encode()
            header = struct.pack("!HHHHHH", qid, 0x8400 | rd, 1, 1, 0, 0)
            record = b"\xc0\x0c" + struct.pack("!HHIH", 16, 3, 0, len(txt) + 1) + bytes(
                [len(txt)]) + txt
            return header + question + record
        return struct.pack("!HHHHHH", qid, 0x8004 | rd, 1, 0, 0, 0) + question  # NOTIMP
    header = struct.pack("!HHHHHH", qid, 0x8180 | rd, 1, 1, 0, 0)
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
    args = sys.argv[1:]
    if args and args[0].startswith("--identity="):
        IDENTITY = args.pop(0).split("=", 1)[1]
    main(args)
