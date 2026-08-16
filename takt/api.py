"""The application core, and the object exposed to the web UI via pywebview.

Every method here is callable from JavaScript as `window.pywebview.api.<name>()`.
It ties together: source -> db -> engine -> hash chain -> UI payloads, plus
settings, day-off overrides, and encrypted export/import.
"""
from __future__ import annotations
import json
import calendar
import datetime as dt

from . import config, crypto, backup
from .db import DB
from .engine import build_day
from .config import Settings


def _dow(day: str) -> str:
    d = dt.date.fromisoformat(day)
    return d.strftime("%a %-d") if hasattr(d, "strftime") else day


def _label(day: str) -> str:
    d = dt.date.fromisoformat(day)
    return d.strftime("%A %-d %b")


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
        wide_start = (dt.date.today() - dt.timedelta(days=400)).isoformat()
        wide_end = dt.date.today().isoformat()
        days = self.db.days_in_range(wide_start, wide_end)
        prev = None
        for day in days:
            summary = build_day(day, self.db.events_for_day(day),
                                self.db.samples_for_day(day),
                                self.db.day_type(day), self.settings)
            payload = json.dumps(summary, sort_keys=True)
            h = crypto.chain_hash(prev, payload)
            self.db.upsert_daily(day, payload, prev, h)
            prev = h
        return "ok"

    # ---- period ranges ---------------------------------------------------
    def _range(self, period: str):
        today = dt.date.today()
        if period == "week":
            return today - dt.timedelta(days=6), today, "Last 7 days"
        if period == "month":
            first = today.replace(day=1)
            last = today.replace(day=calendar.monthrange(today.year, today.month)[1])
            return first, last, today.strftime("%B %Y")
        if period == "year":
            return dt.date(today.year, 1, 1), today, str(today.year)
        return today - dt.timedelta(days=13), today, "Last 14 days"

    # ---- overview payload ------------------------------------------------
    def get_overview(self, period: str = "range") -> str:
        start, end, plabel = self._range(period)
        rows = [json.loads(r["payload"]) for r in self.db.daily_rows()
                if start.isoformat() <= r["day"] <= end.isoformat()]
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
            "low": len([r for r in worked if r["confidence"] == "low"]),
            "nodata": len([r for r in rows if r["state"] == "nodata"]),
        }

        for r in rows:
            r["dow"] = dt.date.fromisoformat(r["day"]).strftime("%a %-d")
            r["label"] = dt.date.fromisoformat(r["day"]).strftime("%A %-d %b")
            r["delta_str"] = (_delta_str(r["delta_minutes"])
                              if r.get("delta_minutes") is not None else None)

        return json.dumps({
            "period": period, "period_label": plabel,
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
                return json.dumps(summary)
        return json.dumps({"day": day, "state": "nodata", "events": []})

    def set_day_off(self, day: str, off: bool) -> str:
        self.db.set_day_type(day, "dayoff" if off else None)
        self.rebuild()
        return "ok"

    # ---- security status -------------------------------------------------
    def integrity(self) -> str:
        rows = self.db.daily_rows()
        ok = crypto.verify_chain(rows)
        return json.dumps({"chain_ok": ok, "days": len(rows),
                           "source": self.source.name})

    # ---- export / import (uses pywebview dialogs when available) ---------
    def export(self, period: str = "range") -> str:
        start, end, plabel = self._range(period)
        ev, sm = [], []
        for day in self.db.days_in_range(start.isoformat(), end.isoformat()):
            ev += self.db.events_for_day(day)
            sm += self.db.samples_for_day(day)
        blob = backup.export_bytes(self.key, ev, sm,
                                   {"period": period, "label": plabel})
        path = self._save_dialog(f"takt-{period}.takt")
        if not path:
            return json.dumps({"ok": False, "reason": "cancelled"})
        with open(path, "wb") as f:
            f.write(blob)
        return json.dumps({"ok": True, "path": path, "bytes": len(blob)})

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
    def _save_dialog(self, suggested: str):
        try:
            import webview
            w = webview.windows[0]
            r = w.create_file_dialog(webview.SAVE_DIALOG, save_filename=suggested)
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
