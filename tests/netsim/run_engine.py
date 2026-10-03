#!/usr/bin/env python3
"""Run the CallGlance engine for a while and print what it concluded (JSON lines).

Used by the network-simulation tests, inside the client namespace, as an
unprivileged user: `ip netns exec cgclient setpriv --reuid=65534 ... run_engine.py`.
"""

import argparse
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from callglance.config import Config  # noqa: E402
from callglance.engine import Engine  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--kind", default="wifi")
    parser.add_argument("--dns", default="192.168.1.1")
    parser.add_argument("--window", type=float, default=None)
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="cg-engine-"))
    config = Config(tmp / "config.json")
    if args.window:
        config.set("window", args.window, save=False)
        config.set("loss_window", args.window, save=False)
    engine = Engine(config, history=None, kind_reader=lambda iface: args.kind,
                    dns_reader=lambda: [args.dns])
    done = threading.Event()

    def on_snapshot(snap: dict) -> None:
        line = {
            "t": round(time.monotonic() - start, 1),
            "level": snap["level"],
            "culprit": snap["culprit"],
            "origin": snap["origin"],
            "headline": snap["headline"],
            "metrics": snap["metrics"],
            "segments": {s["id"]: {k: s.get(k) for k in ("latency_ms", "jitter_ms", "loss_pct",
                                                           "method", "address", "status")}
                         for s in snap["segments"]},
        }
        print(json.dumps(line), flush=True)

    engine.add_listener(on_snapshot)
    start = time.monotonic()
    engine.start()
    done.wait(args.seconds)
    final = engine.snapshot()
    engine.stop()
    print(json.dumps({"final": final}), flush=True)


if __name__ == "__main__":
    main()
