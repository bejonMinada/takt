"""The activity-session engine — where the accuracy lives.

Two ideas make this smarter than "first event = time in, last event = time out":

1. A Wi-Fi connection (or any device-on signal) NEVER sets your time-out on its
   own. If you get home and the laptop rejoins home Wi-Fi but you never touch it,
   that reconnect is ignored. Only *human* activity bounds a working session.

2. When the tray app was running, it sampled real input-idle time. We cluster
   those active samples into sessions, bridging short breaks and splitting on
   long idle gaps. Worked hours = the sum of session lengths, so an idle lunch
   is naturally excluded and a phantom evening reconnect can't inflate the day.

If a day has no idle samples (the app wasn't running), we fall back to the
event log: time-in/out from the first/last *human* events only, flagged
low-confidence.

Each day is tagged Office / Home / Unknown from the Wi-Fi SSID active during the
session, and compared to the configured shift for under/overtime.
"""
from __future__ import annotations
import datetime as dt
from .config import Settings

HUMAN_CODES = {"4624", "4801", "4803"}          # logon(interactive), unlock, screensaver-off
DEVICE_CODES = {"8001", "8003", "4800", "4634",  # wifi conn/disc, lock, logoff
                "kp42", "pt1", "4802"}           # sleep, wake, screensaver-on


def _t(ts: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts)


def _hhmm(d: dt.datetime) -> str:
    return d.strftime("%H:%M")


def _wifi_intervals(events: list[dict]) -> list[tuple[dt.datetime, dt.datetime | None, str]]:
    """Reconstruct (start, end, ssid) intervals from 8001/8003 events."""
    intervals, open_ssid, open_ts = [], None, None
    for e in events:
        if e["code"] == "8001":
            open_ssid, open_ts = e.get("ssid"), _t(e["ts"])
        elif e["code"] == "8003" and open_ts is not None:
            intervals.append((open_ts, _t(e["ts"]), open_ssid))
            open_ssid = open_ts = None
    if open_ts is not None:
        intervals.append((open_ts, None, open_ssid))
    return intervals


def _ssid_at(when: dt.datetime, intervals) -> str | None:
    for s, e, ssid in intervals:
        if s <= when and (e is None or when <= e):
            return ssid
    return None


def _location(ssid: str | None, st: Settings) -> str:
    if not ssid:
        return "unknown"
    return "office" if ssid in st.office_ssids else "home"


def build_day(day: str, events: list[dict], samples: list[str],
              day_type: str | None, st: Settings) -> dict:
    """Return the per-day summary dict consumed by the UI."""
    if day_type == "dayoff":
        return {"day": day, "state": "dayoff", "sessions": [],
                "in": None, "out": None, "confidence": "off", "location": None,
                "worked_minutes": 0, "delta_minutes": None}

    wifi = _wifi_intervals(events)
    human_events = [e for e in events if e["kind"] == "human"]

    # Active points = idle-poll samples that saw input, plus human events.
    pts = sorted({_t(s) for s in samples} | {_t(e["ts"]) for e in human_events})

    sessions: list[dict] = []
    used_fallback = False

    if len(pts) >= 2 and samples:
        bridge = dt.timedelta(minutes=st.engine.bridge_gap_minutes)
        idle_end = dt.timedelta(minutes=st.engine.idle_end_minutes)
        # gap that closes a session = the larger of the two, so long idle wins
        close = max(bridge, idle_end)
        cur_start, cur_last = pts[0], pts[0]
        for p in pts[1:]:
            if p - cur_last <= close:
                cur_last = p
            else:
                sessions.append((cur_start, cur_last))
                cur_start = cur_last = p
        sessions.append((cur_start, cur_last))
        # drop noise-length sessions
        minlen = dt.timedelta(minutes=st.engine.min_session_minutes)
        sessions = [(s, e) for (s, e) in sessions if (e - s) >= minlen]
    else:
        # ---- fallback: event-only bookends, HUMAN events only ----
        used_fallback = True
        if human_events:
            first = _t(human_events[0]["ts"])
            last = _t(human_events[-1]["ts"])
            if last > first:
                sessions = [(first, last)]

    if not sessions:
        return {"day": day, "state": "nodata", "sessions": [], "in": None,
                "out": None, "confidence": "nodata", "location": None,
                "worked_minutes": 0, "delta_minutes": None}

    out_sessions, worked = [], dt.timedelta()
    for s, e in sessions:
        worked += (e - s)
        out_sessions.append({
            "start": _hhmm(s), "end": _hhmm(e),
            "location": _location(_ssid_at(s, wifi), st),
        })

    day_in, day_out = sessions[0][0], sessions[-1][1]
    worked_min = round(worked.total_seconds() / 60)

    # Overall location: office wins if any office session, else home, else unknown.
    locs = {os_["location"] for os_ in out_sessions}
    location = "office" if "office" in locs else ("home" if "home" in locs else "unknown")

    delta = worked_min - st.shift.required_minutes
    confidence = "low" if used_fallback else "normal"

    return {
        "day": day, "state": "worked", "sessions": out_sessions,
        "in": _hhmm(day_in), "out": _hhmm(day_out),
        "worked_minutes": worked_min,
        "delta_minutes": None if used_fallback else delta,
        "confidence": confidence, "location": location,
    }
