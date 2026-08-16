"""Demo source: fabricates a fortnight of realistic activity so the app runs and
demonstrates the smart logic anywhere — including the two tricky cases:

  * Phantom evening reconnect: an office day where the laptop rejoins home Wi-Fi
    at night with NO input after. The engine must ignore it (time-out unchanged).
  * WFH continues: an office session, a commute gap, then a genuine home session
    with real input. The engine must keep both, tagged office/home.
"""
from __future__ import annotations
import datetime as dt
from .base import EventSource

OFFICE = "OFFICE-WIFI-5G"
HOME = "home-net"


def _iso(d: dt.date, hh: int, mm: int, ss: int = 0) -> str:
    return dt.datetime(d.year, d.month, d.day, hh, mm, ss).isoformat()


def _session_samples(d: dt.date, start_m: int, end_m: int, step: int = 60) -> list[str]:
    """One 'active' sample every `step` seconds between start and end minutes."""
    out = []
    t = start_m * 60
    while t <= end_m * 60:
        out.append(_iso(d, t // 3600, (t % 3600) // 60, t % 60))
        t += step
    return out


def _wifi(d: dt.date, on_m: int, off_m: int | None, ssid: str) -> list[dict]:
    evs = [{"ts": _iso(d, on_m // 60, on_m % 60), "kind": "device", "code": "8001",
            "detail": f"Joined Wi-Fi {ssid}", "ssid": ssid, "source": "demo"}]
    if off_m is not None:
        evs.append({"ts": _iso(d, off_m // 60, off_m % 60), "kind": "device", "code": "8003",
                    "detail": "Left Wi-Fi", "ssid": ssid, "source": "demo"})
    return evs


def _logon(d: dt.date, m: int) -> dict:
    return {"ts": _iso(d, m // 60, m % 60), "kind": "human", "code": "4624",
            "detail": "Signed in (interactive) - type 2", "source": "demo"}


def _logoff(d: dt.date, m: int) -> dict:
    return {"ts": _iso(d, m // 60, m % 60), "kind": "human", "code": "4634",
            "detail": "Signed out", "source": "demo"}


class DemoSource(EventSource):
    name = "demo"

    def sync(self, start: dt.date, end: dt.date):
        events: list[dict] = []
        samples: list[str] = []

        # Build a fixed, illustrative fortnight regardless of the asked range,
        # anchored to the most recent 12 days ending "yesterday".
        base = end - dt.timedelta(days=1)
        days = [base - dt.timedelta(days=i) for i in range(11, -1, -1)]

        plans = [
            # (in_m, out_m, wifi_ssid, note)
            (522, 1058, OFFICE, "normal"),      # 08:42-17:38
            (535, 1092, OFFICE, "normal"),      # 08:55-18:12
            (547, 1049, OFFICE, "lowconf"),     # 09:07-17:29  events only, no samples
            (511, 1144, OFFICE, "long"),        # 08:31-19:04
            (560, 1008, OFFICE, "short"),       # 09:20-16:48
            (None, None, None, "weekend"),
            (None, None, None, "weekend"),
            (518, 1072, OFFICE, "normal"),      # 08:38-17:52
            (529, 1100, OFFICE, "phantom"),     # 08:49-18:20 + night reconnect
            (None, None, None, "wfh_continues"),
            (None, None, None, "dayoff"),
            (506, 1061, OFFICE, "normal"),      # 08:26-17:41
        ]
        self._days = days
        self._plans = plans

        for d, (in_m, out_m, ssid, note) in zip(days, plans):
            if note in ("weekend", "dayoff"):
                continue

            if note == "lowconf":
                # only discrete events, no idle samples -> engine uses the
                # event-only fallback and flags the day low-confidence.
                events += _wifi(d, in_m - 2, out_m + 2, ssid)
                events.append(_logon(d, in_m))
                events.append(_logoff(d, out_m))
                continue

            if note == "wfh_continues":
                # office 08:40-17:10, commute gap, home 19:30-21:00 (real input)
                events += _wifi(d, 520, 1035, OFFICE)
                events.append(_logon(d, 520))
                samples += _session_samples(d, 520, 1030)
                events += _wifi(d, 1170, 1265, HOME)
                samples += _session_samples(d, 1170, 1260)
                events.append(_logoff(d, 1262))
                continue

            # office day
            events += _wifi(d, in_m - 2, out_m + 2, ssid)
            events.append(_logon(d, in_m))
            samples += _session_samples(d, in_m, out_m)
            events.append(_logoff(d, out_m))
            # a lunch lock/unlock in the middle (should NOT split a bridged day)
            events.append({"ts": _iso(d, 12, 30), "kind": "device", "code": "4800",
                           "detail": "Workstation locked", "source": "demo"})
            events.append({"ts": _iso(d, 13, 15), "kind": "human", "code": "4801",
                           "detail": "Workstation unlocked", "source": "demo"})

            if note == "phantom":
                # laptop rejoins HOME wifi at 18:50 with NO input afterward.
                events += _wifi(d, 1130, 1180, HOME)  # 18:50-19:40, device-only

        return events, samples

    def day_overrides(self, start, end):
        # mark the planned "dayoff" slot in the fabricated fortnight
        base = end - dt.timedelta(days=1)
        days = [base - dt.timedelta(days=i) for i in range(11, -1, -1)]
        plans = ["normal", "normal", "lowconf", "long", "short", "weekend",
                 "weekend", "normal", "phantom", "wfh_continues", "dayoff", "normal"]
        return {d.isoformat(): "dayoff" for d, n in zip(days, plans) if n == "dayoff"}
