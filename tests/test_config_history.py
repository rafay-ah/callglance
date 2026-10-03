import json
import time

from callglance.config import DEFAULTS, Config
from callglance.history import History


def test_defaults_and_merge(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"interval": 3, "widget": {"enabled": True}}))
    cfg = Config(path)
    assert cfg["interval"] == 3
    assert cfg["widget"]["enabled"] is True
    assert cfg["widget"]["on_top"] is DEFAULTS["widget"]["on_top"]
    assert cfg["public_targets"] == ["1.1.1.1", "8.8.8.8"]


def test_save_roundtrip_and_partial_dict_updates(tmp_path):
    cfg = Config(tmp_path / "c.json")
    assert cfg.first_run
    cfg.set("widget", {"x": 10})
    cfg.set("notifications", False)
    again = Config(tmp_path / "c.json")
    assert again["widget"]["x"] == 10 and again["widget"]["enabled"] is False
    assert again["notifications"] is False and not again.first_run


def test_corrupt_config_falls_back_to_defaults(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{nope")
    assert Config(path)["interval"] == DEFAULTS["interval"]


def test_history_memory_and_disk(tmp_path):
    db = tmp_path / "h.sqlite3"
    h = History(db, flush_every=3600)
    now = time.time()
    for i in range(10):
        h.add({"ts": now - 100 + i * 5, "level": "good", "net_ms": 10.0 + i})
    assert len(h.since(3600)) == 10
    h.close()
    reopened = History(db)
    points = reopened.since(3600)
    assert len(points) == 10 and points[-1]["net_ms"] == 19.0
    assert reopened.since(7 * 3600)[0]["level"] == "good"  # beyond memory: read from disk
    reopened.close()


def test_history_without_disk():
    h = History(None)
    h.add({"ts": time.time(), "level": "fair"})
    assert h.since(60)[0]["level"] == "fair"
