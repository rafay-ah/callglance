"""Static checks for the GNOME Shell extension (it cannot run outside GNOME Shell)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

EXT = Path(__file__).resolve().parents[1] / "gnome-shell" / "callglance@rafay-ah.github.io"


def test_metadata():
    meta = json.loads((EXT / "metadata.json").read_text())
    assert meta["uuid"] == EXT.name
    assert {"45", "46", "50"} <= set(meta["shell-version"])
    assert meta["name"] and meta["description"]


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
@pytest.mark.parametrize("name", sorted(p.name for p in EXT.glob("*.js")))
def test_javascript_parses_as_a_module(name, tmp_path):
    copy = tmp_path / (Path(name).stem + ".mjs")
    copy.write_text((EXT / name).read_text())
    result = subprocess.run(["node", "--check", str(copy)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _code(path: Path) -> str:
    return "\n".join(line for line in path.read_text().splitlines()
                     if not line.strip().startswith("//"))


def test_no_options_newer_shells_reject():
    # GNOME 50 throws on addChrome's affectsInputRegion; GNOME 51 on BoxLayout `vertical`,
    # which only ui.js may use, behind feature detection.
    for path in EXT.glob("*.js"):
        code = _code(path)
        assert "affectsInputRegion" not in code, path.name
        if path.name != "ui.js":
            assert "vertical:" not in code, path.name


def test_stylesheet_has_no_negative_margins():
    # St treats negative lengths as huge unsigned sizes, which breaks the popover layout.
    css = (EXT / "stylesheet.css").read_text()
    assert ": -" not in css.replace("-st-", "")
