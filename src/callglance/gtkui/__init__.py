"""GTK 3 fallback interface: tray icon, details window and desktop widget."""

from __future__ import annotations

UI_EXIT_NO_TOOLKIT = 3


def main(show: bool = False) -> int:
    try:
        from callglance.gtkui.app import run
    except (ImportError, ValueError) as exc:
        import sys

        print(f"callglance ui: GTK 3 / AyatanaAppIndicator not available: {exc}", file=sys.stderr)
        return UI_EXIT_NO_TOOLKIT
    return run(show=show)
