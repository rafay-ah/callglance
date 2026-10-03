import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def pytest_configure(config):
    config.addinivalue_line("markers", "netsim: needs root and network namespaces (slow)")
    config.addinivalue_line("markers", "dbus: needs a D-Bus daemon and PyGObject")


def pytest_collection_modifyitems(config, items):
    # Keep the slow end-to-end simulation opt-in: `pytest -m netsim` or CALLGLANCE_NETSIM=1.
    if os.environ.get("CALLGLANCE_NETSIM") or "netsim" in (config.getoption("-m") or ""):
        return
    import pytest

    skip = pytest.mark.skip(reason="network simulation: run with -m netsim (needs root)")
    for item in items:
        if "netsim" in item.keywords:
            item.add_marker(skip)
