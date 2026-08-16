"""Tray-resident runner: this is what should actually run at login.

Behavior:
  - Opens the Takt window.
  - Closing the window HIDES it instead of quitting the process.
  - A tray icon (bottom-right, hidden icons area) gives Show / Sync now / Quit.
  - The idle poller thread keeps running whether the window is open or not, so
    activity is captured all day, not just while you're looking at the app.

Run this (not main.py) for real day-to-day use / at-login startup:
    pythonw tray.py
"""
from __future__ import annotations
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(__file__))

from takt.api import Api

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
ICON_SIZE = 64


def _make_icon_image():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([4, 4, ICON_SIZE - 4, ICON_SIZE - 4], radius=14, fill=(14, 124, 134, 255))
    d.ellipse([20, 20, ICON_SIZE - 20, ICON_SIZE - 20], outline=(255, 255, 255, 255), width=5)
    return img


def _idle_poll_loop(api: Api, stop_evt: threading.Event):
    while not stop_evt.is_set():
        try:
            ts = api.source.poll_active()
            if ts:
                api.add_sample(ts)
        except Exception:
            pass
        stop_evt.wait(api.settings.engine.sample_seconds)


def run():
    import webview
    import pystray

    api = Api()
    stop_evt = threading.Event()
    if api.source.name == "windows":
        threading.Thread(target=_idle_poll_loop, args=(api, stop_evt), daemon=True).start()

    window = webview.create_window(
        "Takt", os.path.join(WEB_DIR, "index.html"),
        js_api=api, width=1120, height=760, min_size=(860, 560), hidden=False,
    )

    def on_closing():
        window.hide()
        return False  # cancel the actual close; window is just hidden

    window.events.closing += on_closing

    def show(icon=None, item=None):
        window.show()

    def sync_now(icon=None, item=None):
        try:
            api.sync(30)
        except Exception:
            pass

    def quit_app(icon=None, item=None):
        stop_evt.set()
        icon.stop()
        window.destroy()

    icon = pystray.Icon(
        "takt", _make_icon_image(), "Takt",
        menu=pystray.Menu(
            pystray.MenuItem("Show Takt", show, default=True),
            pystray.MenuItem("Sync now", sync_now),
            pystray.MenuItem("Quit", quit_app),
        ),
    )
    threading.Thread(target=icon.run, daemon=True).start()
    webview.start()


if __name__ == "__main__":
    run()
