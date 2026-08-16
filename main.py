"""Takt entry point.

Launches a native window (pywebview) around web/index.html, exposes Api as
window.pywebview.api, and — on Windows — starts a background thread that polls
GetLastInputInfo so real activity sessions can be built while the app is open.

Run:
    python main.py
"""
from __future__ import annotations
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))

from takt.api import Api

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")


def _idle_poll_loop(api: Api, stop_evt: threading.Event):
    """Background thread: sample real input every N seconds (Windows only)."""
    while not stop_evt.is_set():
        try:
            ts = api.source.poll_active()
            if ts:
                api.add_sample(ts)
        except Exception:
            pass
        stop_evt.wait(api.settings.engine.sample_seconds)


def main():
    import webview

    api = Api()
    stop_evt = threading.Event()

    if api.source.name == "windows":
        t = threading.Thread(target=_idle_poll_loop, args=(api, stop_evt), daemon=True)
        t.start()

    window = webview.create_window(
        "Takt", os.path.join(WEB_DIR, "index.html"),
        js_api=api, width=1120, height=760, min_size=(860, 560),
    )

    def on_closing():
        # Hide instead of quit, matching "won't exit unless quit from tray".
        # Only wired up when the tray module is running this process; a plain
        # `python main.py` run just exits normally so it's easy to test.
        return True

    webview.start(private_mode=False)
    stop_evt.set()


if __name__ == "__main__":
    main()
