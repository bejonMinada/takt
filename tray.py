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
ICO_PATH = os.path.join(WEB_DIR, "assets", "icon.ico")
MARK_PATH = os.path.join(WEB_DIR, "assets", "mark.png")


def _make_icon_image():
    from PIL import Image
    return Image.open(MARK_PATH)


def _idle_poll_loop(api: Api, stop_evt: threading.Event):
    while not stop_evt.is_set():
        try:
            ts = api.source.poll_active()
            if ts:
                api.add_sample(ts)
        except Exception:
            pass
        stop_evt.wait(api.settings.engine.sample_seconds)


def _bring_existing_window_forward():
    try:
        import win32con
        import win32gui
        hwnd = win32gui.FindWindow(None, "Takt")
        if hwnd:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass


def run():
    import webview
    import pystray
    import win32api
    import win32event
    import winerror

    # The autostart scheduled task and a manual launch can easily race (e.g.
    # ONLOGON fires while the user also double-clicks run.bat). Two processes
    # writing to the same SQLite file at once is exactly the kind of
    # contention that made sync() silently drop writes before WAL/timeout
    # were added to db.py — so refuse to run a second instance at all rather
    # than relying on the DB layer alone to paper over it.
    mutex = win32event.CreateMutex(None, False, "TaktSingleInstanceMutex")
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        _bring_existing_window_forward()
        return

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
    webview.start(icon=ICO_PATH)


if __name__ == "__main__":
    run()
