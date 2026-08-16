"""The application core, and the object exposed to the web UI via pywebview.

Every method here is callable from JavaScript as `window.pywebview.api.<name>()`.
It ties together: source -> db -> engine -> hash chain -> UI payloads, plus
settings, day-off overrides, and encrypted export/import.
"""
from __future__ import annotations
import csv
import io
import json
import calendar
import os
import re
import datetime as dt

from . import config, crypto, backup
from .db import DB
from .engine import build_day, _hhmm_to_min
from .config import Settings


def _profile_name() -> str:
    raw = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    return re.sub(r'[<>:"/\\|?*]', "_", raw)


def _find_own_window(title: str):
    """Find the HWND for `title` that belongs to THIS process, not just any
    window with that title. capture_attendance() needs this because the real
    app and demo.py deliberately share the exact window title "Takt" — a
    naive FindWindow() would silently grab whichever one the OS happens to
    enumerate first if both are running at once."""
    import win32gui
    import win32process

    own_pid = os.getpid()
    matches = []

    def _cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == title:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid == own_pid:
                matches.append(hwnd)
        return True

    win32gui.EnumWindows(_cb, None)
    return matches[0] if matches else None


def _visible_window_bounds(hwnd) -> tuple[int, int, int, int]:
    """GetWindowRect includes DWM's invisible resize-border padding on
    Windows 10/11, which leaks a sliver of whatever's behind the window into
    a screen-region grab. DWMWA_EXTENDED_FRAME_BOUNDS is the actual visible
    extent; fall back to GetWindowRect if DWM can't answer (e.g. no
    compositor)."""
    import ctypes
    from ctypes import wintypes
    import win32gui

    rect = wintypes.RECT()
    DWMWA_EXTENDED_FRAME_BOUNDS = 9
    hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS,
        ctypes.byref(rect), ctypes.sizeof(rect))
    if hr == 0:
        return (rect.left, rect.top, rect.right, rect.bottom)
    return win32gui.GetWindowRect(hwnd)


def _dow(day: str) -> str:
    # %-d (no zero-padding) is a glibc/Linux strftime extension; Windows'
    # CRT raises ValueError on it, so build the no-pad day manually instead.
    d = dt.date.fromisoformat(day)
    return f"{d.strftime('%a')} {d.day}" if hasattr(d, "strftime") else day


def _label(day: str) -> str:
    d = dt.date.fromisoformat(day)
    return f"{d.strftime('%A')} {d.day} {d.strftime('%b')}"


def _mins_to_hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def _delta_str(m: int) -> str:
    s = "−" if m < 0 else "+"
    a = abs(m)
    return f"{s}{a // 60}:{a % 60:02d}"


class Api:
    def __init__(self):
        self.db = DB(config.DB_PATH)
        self.settings = self._load_settings()
        self.key = crypto.load_or_create_key(config.KEY_PATH)
        self.source = self._make_source()
        if self.source.name == "demo" and not self.db.list_projects():
            self._seed_demo_projects()

    # ---- settings --------------------------------------------------------
    def _load_settings(self) -> Settings:
        raw = self.db.get_settings()
        if raw:
            try:
                return Settings.from_json(raw)
            except Exception:
                pass
        st = config.default_settings()
        self.db.save_settings(st.to_json())
        return st

    def _make_source(self):
        if self.settings.source == "windows":
            try:
                from .sources.windows import WindowsSource
                return WindowsSource()
            except Exception:
                pass
        from .sources.demo import DemoSource
        return DemoSource()

    def get_settings(self) -> str:
        return self.settings.to_json()

    def save_settings(self, payload: str) -> str:
        self.settings = Settings.from_json(payload)
        self.db.save_settings(self.settings.to_json())
        self.source = self._make_source()
        self.rebuild()
        return "ok"

    def known_ssids(self) -> str:
        return json.dumps(self.db.known_ssids())

    # ---- capture + rebuild ----------------------------------------------
    def sync(self, days_back: int = 30) -> str:
        end = dt.date.today()
        start = end - dt.timedelta(days=days_back)
        events, samples = self.source.sync(start, end)
        n_e = self.db.add_events(events)
        n_s = self.db.add_samples(samples)
        for day, type_ in self.source.day_overrides(start, end).items():
            self.db.set_day_type(day, type_)
        self.rebuild()
        return json.dumps({"events": n_e, "samples": n_s})

    def add_sample(self, iso_ts: str) -> str:
        """Called by the live idle poller thread."""
        self.db.add_samples([iso_ts])
        return "ok"

    def rebuild(self) -> str:
        """Recompute every day's summary and the tamper-evidence hash chain."""
        # Looks both ways from today: back for captured history, forward so
        # leave marked ahead of time (the normal case) shows up immediately
        # instead of waiting for that date to arrive.
        wide_start = (dt.date.today() - dt.timedelta(days=400)).isoformat()
        wide_end = (dt.date.today() + dt.timedelta(days=400)).isoformat()
        days = self.db.days_in_range(wide_start, wide_end)
        prev = None
        for day in days:
            summary = build_day(day, self.db.events_for_day(day),
                                self.db.samples_for_day(day),
                                self.db.day_type(day), self.settings,
                                self.db.leave_for_day(day))
            payload = json.dumps(summary, sort_keys=True)
            h = crypto.chain_hash(prev, payload)
            self.db.upsert_daily(day, payload, prev, h)
            prev = h
        return "ok"

    # ---- period ranges ---------------------------------------------------
    @staticmethod
    def _range_label(s: dt.date, e: dt.date) -> str:
        if s == e:
            return f"{s.strftime('%b')} {s.day}, {s.year}"
        if s.year == e.year:
            return f"{s.strftime('%b')} {s.day} – {e.strftime('%b')} {e.day}, {e.year}"
        return f"{s.strftime('%b')} {s.day}, {s.year} – {e.strftime('%b')} {e.day}, {e.year}"

    def _range(self, period: str, start: str | None = None, end: str | None = None):
        if period == "range" and start and end:
            s, e = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
            if e < s:
                s, e = e, s
            return s, e, self._range_label(s, e)

        today = dt.date.today()
        if period == "week":
            # date.weekday(): Monday=0..Sunday=6, same convention week_start
            # is stored in. Days since the most recent occurrence of
            # week_start (today itself if today IS that weekday).
            since_start = (today.weekday() - self.settings.week_start) % 7
            first = today - dt.timedelta(days=since_start)
            last = first + dt.timedelta(days=6)
            return first, last, self._range_label(first, last)
        if period == "month":
            first = today.replace(day=1)
            last = today.replace(day=calendar.monthrange(today.year, today.month)[1])
            return first, last, today.strftime("%B %Y")
        if period == "year":
            return dt.date(today.year, 1, 1), today, str(today.year)
        return today - dt.timedelta(days=13), today, "Last 14 days"

    # ---- overview payload ------------------------------------------------
    def get_overview(self, period: str = "range", start: str | None = None, end: str | None = None) -> str:
        rstart, rend, plabel = self._range(period, start, end)
        rows = [json.loads(r["payload"]) for r in self.db.daily_rows()
                if rstart.isoformat() <= r["day"] <= rend.isoformat()]
        rows.sort(key=lambda r: r["day"])

        worked = [r for r in rows if r["state"] == "worked"]
        confident = [r for r in worked if r["confidence"] == "normal"]

        def avg_time(getter):
            vals = [getter(r) for r in worked if getter(r)]
            if not vals:
                return "—"
            mins = [int(v[:2]) * 60 + int(v[3:]) for v in vals]
            m = round(sum(mins) / len(mins))
            return _mins_to_hhmm(m)

        avg_in = avg_time(lambda r: r["in"])
        avg_out = avg_time(lambda r: r["out"])
        spans = [r["worked_minutes"] for r in worked if r["worked_minutes"]]
        avg_span = round(sum(spans) / len(spans)) if spans else 0
        net = sum(r["delta_minutes"] for r in confident
                  if r["delta_minutes"] is not None)

        cov = {
            "worked": len(worked),
            "home": len([r for r in worked if r["location"] == "home"]),
            "dayoff": len([r for r in rows if r["state"] == "dayoff"]),
            "leave": len([r for r in rows if r["state"] == "leave"]),
            "low": len([r for r in worked if r["confidence"] == "low"]),
            "nodata": len([r for r in rows if r["state"] == "nodata"]),
        }

        for r in rows:
            r["dow"] = _dow(r["day"])
            r["label"] = _label(r["day"])
            r["delta_str"] = (_delta_str(r["delta_minutes"])
                              if r.get("delta_minutes") is not None else None)

        return json.dumps({
            "period": period, "period_label": plabel,
            "range_start": rstart.isoformat(), "range_end": rend.isoformat(),
            "shift": {
                "start": self.settings.shift.start, "end": self.settings.shift.end,
                "flex_minutes": self.settings.shift.flex_minutes,
                "required_minutes": self.settings.shift.required_minutes,
            },
            "stats": {
                "avg_in": avg_in, "avg_out": avg_out,
                "avg_span": _mins_to_hhmm(avg_span) if avg_span else "—",
                "net_delta": _delta_str(net), "net_positive": net >= 0,
            },
            "coverage": cov,
            "days": rows,
        })

    def get_day(self, day: str) -> str:
        for r in self.db.daily_rows():
            if r["day"] == day:
                summary = json.loads(r["payload"])
                summary["events"] = self.db.events_for_day(day)
                summary["leave_records"] = self.db.leave_for_day(day)
                return json.dumps(summary)
        return json.dumps({"day": day, "state": "nodata", "events": [],
                           "leave_records": self.db.leave_for_day(day)})

    def set_day_off(self, day: str, off: bool) -> str:
        self.db.set_day_type(day, "dayoff" if off else None)
        self.rebuild()
        return "ok"

    # ---- leave --------------------------------------------------------
    LEAVE_TYPES = {"leave", "holiday", "sick", "offset", "other"}

    def add_leave(self, start_day: str, end_day: str, full_day: bool,
                  start_time: str | None = None, end_time: str | None = None,
                  note: str | None = None, leave_type: str = "leave") -> str:
        """Mark approved leave/holiday/etc. A whole-day block spans every day
        from start_day to end_day inclusive; an hour-range block applies to a
        single day only (start_day == end_day)."""
        if leave_type not in self.LEAVE_TYPES:
            leave_type = "leave"
        lo, hi = dt.date.fromisoformat(start_day), dt.date.fromisoformat(end_day)
        if hi < lo:
            return json.dumps({"ok": False, "reason": "End date is before start date"})
        if not full_day and lo != hi:
            return json.dumps({"ok": False, "reason": "An hour range can only apply to a single day"})

        new_s = new_e = None
        if not full_day:
            new_s, new_e = _hhmm_to_min(start_time), _hhmm_to_min(end_time)
            if new_e <= new_s:
                return json.dumps({"ok": False, "reason": "End time must be after start time"})

        # Validate every day in the range before writing anything, so a
        # conflict partway through doesn't leave the range half-applied.
        day = lo
        while day <= hi:
            for b in self.db.leave_for_day(day.isoformat()):
                if full_day or b["full_day"]:
                    return json.dumps({"ok": False,
                                       "reason": f"{day.isoformat()} already has leave recorded"})
                ex_s, ex_e = _hhmm_to_min(b["start_time"]), _hhmm_to_min(b["end_time"])
                if new_s < ex_e and ex_s < new_e:
                    return json.dumps({"ok": False,
                                       "reason": f"Overlaps existing leave on {day.isoformat()}"})
            day += dt.timedelta(days=1)

        day = lo
        while day <= hi:
            self.db.add_leave(day.isoformat(), full_day,
                              None if full_day else start_time,
                              None if full_day else end_time, note, leave_type)
            day += dt.timedelta(days=1)
        self.rebuild()
        return json.dumps({"ok": True})

    def remove_leave(self, leave_id: int) -> str:
        self.db.delete_leave(leave_id)
        self.rebuild()
        return "ok"

    # ---- security status -------------------------------------------------
    def integrity(self) -> str:
        rows = self.db.daily_rows()
        ok = crypto.verify_chain(rows)
        return json.dumps({"chain_ok": ok, "days": len(rows),
                           "source": self.source.name})

    # ---- human-readable report --------------------------------------------
    def download_report(self, period: str = "range", start: str | None = None, end: str | None = None) -> str:
        """Write a CSV summary of the period's daily records straight to the
        user's Downloads folder — no dialog, unlike export() (which produces
        the encrypted .takt backup and lets the user choose where it goes)."""
        ov = json.loads(self.get_overview(period, start, end))
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([f"Takt report — {ov['period_label']}"])
        w.writerow([f"Generated {dt.date.today().isoformat()}"])
        w.writerow([])
        w.writerow(["Day", "Location", "Time In", "Time Out", "Worked", "Leave", "vs Shift", "Status"])
        leave_type_labels = {"leave": "Leave", "holiday": "Holiday", "sick": "Sick", "offset": "Offset", "other": "Other"}
        for d in ov["days"]:
            worked = _mins_to_hhmm(d["worked_minutes"]) if d["state"] == "worked" else ""
            leave = _mins_to_hhmm(d["leave_minutes"]) if d.get("leave_minutes") else ""
            if d["state"] == "leave":
                status = leave_type_labels.get(d.get("leave_type"), "Leave")
            elif d["state"] == "dayoff":
                status = "Weekly day off" if d.get("recurring") else "Day off"
            elif d["state"] == "nodata":
                status = "No data"
            else:
                status = (d.get("confidence") or "worked").capitalize()
            w.writerow([d.get("label", d["day"]), (d.get("location") or "").capitalize(),
                       d.get("in") or "", d.get("out") or "", worked, leave,
                       d.get("delta_str") or "", status])

        downloads_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        try:
            os.makedirs(downloads_dir, exist_ok=True)
            filename = f"takt-report-{ov['range_start']}_to_{ov['range_end']}.csv"
            path = os.path.join(downloads_dir, filename)
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                f.write(buf.getvalue())
        except OSError as e:
            return json.dumps({"ok": False, "reason": str(e)})
        return json.dumps({"ok": True, "path": path, "filename": filename})

    def whoami(self) -> str:
        return json.dumps({"name": _profile_name()})

    def get_range_days(self, start_day: str, end_day: str) -> str:
        """Daily records for an arbitrary date range (not a preset period) —
        backs the attendance-capture screenshot, where the user can pick any
        span of at least one day."""
        rows = [json.loads(r["payload"]) for r in self.db.daily_rows()
                if start_day <= r["day"] <= end_day]
        rows.sort(key=lambda r: r["day"])
        for r in rows:
            r["dow"] = _dow(r["day"])
            r["label"] = _label(r["day"])
            r["delta_str"] = (_delta_str(r["delta_minutes"])
                              if r.get("delta_minutes") is not None else None)
        return json.dumps({"days": rows})

    def capture_attendance(self, start_day: str, end_day: str) -> str:
        """Screenshot the app window — for when the badge system fails or is
        unavailable and a screenshot is needed as fallback proof. The
        frontend renders a dedicated printable attendance view before
        calling this, so whatever's currently on screen is what gets saved."""
        try:
            import webview
            import win32gui
            from PIL import ImageGrab
        except Exception as e:
            return json.dumps({"ok": False, "reason": f"Screenshot isn't available: {e}"})

        try:
            webview.windows[0].show()
        except Exception:
            pass

        hwnd = _find_own_window("Takt")
        if not hwnd:
            return json.dumps({"ok": False, "reason": "Couldn't find the Takt window"})
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass

        try:
            rect = _visible_window_bounds(hwnd)
            img = ImageGrab.grab(bbox=rect)
        except Exception as e:
            return json.dumps({"ok": False, "reason": str(e)})

        stamp = start_day if start_day == end_day else f"{start_day}_to_{end_day}"
        filename = f"{_profile_name()}_{stamp}.png"
        downloads_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        try:
            os.makedirs(downloads_dir, exist_ok=True)
            path = os.path.join(downloads_dir, filename)
            img.save(path, "PNG")
        except OSError as e:
            return json.dumps({"ok": False, "reason": str(e)})
        return json.dumps({"ok": True, "path": path, "filename": filename})

    # ---- export / import (uses pywebview dialogs when available) ---------
    def export(self, period: str = "range", start: str | None = None, end: str | None = None) -> str:
        rstart, rend, plabel = self._range(period, start, end)
        ev, sm = [], []
        for day in self.db.days_in_range(rstart.isoformat(), rend.isoformat()):
            ev += self.db.events_for_day(day)
            sm += self.db.samples_for_day(day)
        blob = backup.export_bytes(self.key, ev, sm,
                                   {"period": period, "label": plabel})
        path = self._save_dialog(f"takt-{rstart.isoformat()}_to_{rend.isoformat()}.takt", self.settings.backup_dir)
        if not path:
            return json.dumps({"ok": False, "reason": "cancelled"})
        with open(path, "wb") as f:
            f.write(blob)
        return json.dumps({"ok": True, "path": path, "bytes": len(blob)})

    def choose_backup_dir(self) -> str:
        """Open a folder picker and remember the choice as the default export location."""
        try:
            import webview
            w = webview.windows[0]
            r = w.create_file_dialog(webview.FOLDER_DIALOG,
                                     directory=self.settings.backup_dir or "")
            picked = r[0] if r else None
        except Exception:
            picked = None
        if not picked:
            return json.dumps({"ok": False, "reason": "cancelled"})
        self.settings.backup_dir = picked
        self.db.save_settings(self.settings.to_json())
        return json.dumps({"ok": True, "path": picked})

    def import_backup(self) -> str:
        path = self._open_dialog()
        if not path:
            return json.dumps({"ok": False, "reason": "cancelled"})
        with open(path, "rb") as f:
            blob = f.read()
        try:
            res = backup.import_bytes(self.key, blob)
        except Exception as e:
            return json.dumps({"ok": False, "reason": str(e)})
        self.db.add_events(res["events"])
        self.db.add_samples(res["samples"], source="import")
        self.db.log_import(path, res["sha256"], res["rows"])
        self.rebuild()
        return json.dumps({"ok": True, "rows": res["rows"]})

    # ---- dialogs (no-op returns when running headless/browser) -----------
    def _save_dialog(self, suggested: str, directory: str | None = None):
        try:
            import webview
            w = webview.windows[0]
            r = w.create_file_dialog(webview.SAVE_DIALOG, directory=directory or "",
                                     save_filename=suggested)
            return r if isinstance(r, str) else (r[0] if r else None)
        except Exception:
            return None

    def _open_dialog(self):
        try:
            import webview
            w = webview.windows[0]
            r = w.create_file_dialog(webview.OPEN_DIALOG,
                                     file_types=("Takt backup (*.takt)",))
            return r[0] if r else None
        except Exception:
            return None

    # ---- projects & tasks (Gantt + Kanban) --------------------------------
    def _seed_demo_projects(self):
        today = dt.date.today()
        def day(offset): return (today + dt.timedelta(days=offset)).isoformat()

        p1 = self.db.add_project("Takt Launch", "#0E7C86")
        for i, (name, s, e, status) in enumerate([
            ("Define requirements", -13, -9, "done"),
            ("Build attendance engine", -10, -2, "done"),
            ("Design UI", -6, 1, "doing"),
            ("Add project management", 0, 6, "doing"),
            ("Package installer", 5, 10, "todo"),
            ("Team rollout", 10, 14, "todo"),
        ]):
            self.db.add_task(p1, name, day(s), day(e), status, i)

        p2 = self.db.add_project("Website Refresh", "#8C6BB1")
        for i, (name, s, e, status) in enumerate([
            ("Content audit", -8, -4, "done"),
            ("New homepage design", -4, 3, "doing"),
            ("Copywriting", -2, 4, "doing"),
            ("QA & launch", 5, 9, "todo"),
        ]):
            self.db.add_task(p2, name, day(s), day(e), status, i)

    def list_projects(self) -> str:
        projs = self.db.list_projects()
        for p in projs:
            tasks = self.db.list_tasks(p["id"])
            p["task_count"] = len(tasks)
            p["start"] = min((t["start_date"] for t in tasks), default=None)
            p["end"] = max((t["end_date"] for t in tasks), default=None)
            p["done"] = len([t for t in tasks if t["status"] == "done"])
        return json.dumps(projs)

    def create_project(self, name: str, color: str = "#0E7C86") -> str:
        pid = self.db.add_project(name, color)
        return json.dumps({"id": pid})

    def delete_project(self, project_id: int) -> str:
        self.db.delete_project(project_id)
        return "ok"

    def list_tasks(self, project_id: int) -> str:
        return json.dumps(self.db.list_tasks(project_id))

    def create_task(self, project_id: int, name: str, start_date: str,
                    end_date: str, status: str = "todo") -> str:
        n = len(self.db.list_tasks(project_id))
        tid = self.db.add_task(project_id, name, start_date, end_date, status, n)
        return json.dumps({"id": tid})

    def update_task(self, task_id: int, payload: str) -> str:
        fields = json.loads(payload)
        allowed = {"name", "start_date", "end_date", "status", "sort_order"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        self.db.update_task(task_id, **fields)
        return "ok"

    def delete_task(self, task_id: int) -> str:
        self.db.delete_task(task_id)
        return "ok"
