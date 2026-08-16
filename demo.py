"""Takt demo-mode entry point — presentation data, fully isolated from the
real captured data in %LOCALAPPDATA%\\Takt.

Points the database/key at a separate _demo_data folder next to this file
(via TAKT_DATA_DIR, read once at import time — must be set before takt.config
loads), forces the demo event source, seeds the two sample projects, and
syncs a realistic fabricated fortnight of attendance so Overview/Daily
records/Projects are all populated the moment the window opens.

The _demo_data folder is wiped and rebuilt from scratch on every launch.
DemoSource fabricates its fortnight relative to whatever day "today" is, so
without a fresh start, running this on two different calendar days (e.g.
rehearsing tonight, presenting tomorrow) would layer a second overlapping
fortnight on top of the first instead of replacing it.

Run:
    pythonw demo.py    (or python demo.py to see console output)
"""
from __future__ import annotations
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(__file__))
DEMO_DIR = os.path.join(os.path.dirname(__file__), "_demo_data")
os.environ["TAKT_DATA_DIR"] = DEMO_DIR

from takt.api import Api

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
ICO_PATH = os.path.join(WEB_DIR, "assets", "icon.ico")


def main():
    import webview

    # config.DB_PATH/KEY_PATH were already resolved at import time (which also
    # created DEMO_DIR as a side effect of data_dir()) — recreate the now-empty
    # folder so DB(config.DB_PATH) below has somewhere to put the fresh file.
    shutil.rmtree(DEMO_DIR, ignore_errors=True)
    os.makedirs(DEMO_DIR, exist_ok=True)

    api = Api()
    if api.settings.source != "demo":
        api.settings.source = "demo"
        api.db.save_settings(api.settings.to_json())
        api.source = api._make_source()
    if not api.db.list_projects():
        api._seed_demo_projects()
    api.sync(30)

    webview.create_window(
        "Takt", os.path.join(WEB_DIR, "index.html"),
        js_api=api, width=1120, height=760, min_size=(860, 560),
    )
    webview.start(private_mode=False, icon=ICO_PATH)


if __name__ == "__main__":
    main()
