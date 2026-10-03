#!/usr/bin/env python3
"""Userspace Ethernet link emulator (a stand-in for `tc netem`).

Forwards frames between two interfaces and adds delay, jitter and loss to IPv4
traffic in both directions. The impairment is read from a JSON control file,
which is re-read whenever it changes, e.g. {"delay_ms": 2, "jitter_ms": 40,
"loss_pct": 3}. Needs CAP_NET_RAW (run it as root inside a test namespace).
"""

import heapq
import json
import os
import random
import select
import socket
import struct
import sys
import time

ETH_P_ALL = 0x0003
PACKET_OUTGOING = 4


def open_iface(name: str) -> socket.socket:
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    sock.bind((name, 0))
    sock.setblocking(False)
    return sock


def main(a: str, b: str, ctl: str) -> None:
    sa, sb = open_iface(a), open_iface(b)
    peer = {sa.fileno(): sb, sb.fileno(): sa}
    conf: dict = {}
    mtime = None
    queue: list = []
    counter = 0
    rng = random.Random(1234)
    while True:
        try:
            st = os.stat(ctl).st_mtime_ns
            if st != mtime:
                with open(ctl) as fh:
                    conf = json.load(fh)
                mtime = st
        except (OSError, ValueError):
            pass
        timeout = 0.05
        if queue:
            timeout = max(0.0, min(timeout, queue[0][0] - time.monotonic()))
        ready, _, _ = select.select([sa, sb], [], [], timeout)
        for sock in ready:
            for _ in range(256):
                try:
                    frame, addr = sock.recvfrom(65535)
                except BlockingIOError:
                    break
                if addr[2] == PACKET_OUTGOING:
                    continue
                delay = 0.0
                if len(frame) >= 14 and struct.unpack("!H", frame[12:14])[0] == 0x0800:
                    if rng.random() * 100.0 < float(conf.get("loss_pct", 0)):
                        continue
                    delay = float(conf.get("delay_ms", 0)) + rng.random() * float(
                        conf.get("jitter_ms", 0))
                due = time.monotonic() + delay / 1000.0
                heapq.heappush(queue, (due, counter, peer[sock.fileno()], frame))
                counter += 1
        now = time.monotonic()
        while queue and queue[0][0] <= now:
            _, _, out, frame = heapq.heappop(queue)
            try:
                out.send(frame)
            except OSError:
                pass


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
