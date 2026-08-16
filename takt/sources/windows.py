"""Windows event source — reads the real logs and polls input-idle time.

IMPORTANT: this module only works on Windows with pywin32 installed. It is
imported lazily so the rest of the app runs anywhere. The reads that matter:

  * Security log 4624 (interactive logon: types 2/10/11), 4634/4647 (logoff),
    4800 (lock), 4801 (unlock)         -> needs 'Event Log Readers' membership
  * System log Kernel-Power 42 (sleep), Power-Troubleshooter 1 (wake)
  * Microsoft-Windows-WLAN-AutoConfig/Operational 8001/8003 (Wi-Fi SSID)
  * GetLastInputInfo (user32) polled by the tray app -> real activity samples

If the Security log can't be read (no permission), the reader degrades: it uses
whatever logs it CAN read plus the live idle samples, and marks affected days
low-confidence rather than crashing.
"""
from __future__ import annotations
import datetime as dt
from .base import EventSource

# logon types we treat as a human being present at the machine
_INTERACTIVE_TYPES = {"2", "10", "11"}


def _now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


class WindowsSource(EventSource):
    name = "windows"

    def __init__(self):
        self._ok = False
        try:
            import win32evtlog  # noqa: F401
            import win32api     # noqa: F401
            self._ok = True
        except Exception:
            self._ok = False

    # ---- live idle sampling ---------------------------------------------
    def poll_active(self, idle_threshold_seconds: int = 300) -> str | None:
        """Return a timestamp if the user has given input recently."""
        try:
            import ctypes

            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

            info = LASTINPUTINFO()
            info.cbSize = ctypes.sizeof(info)
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
                return None
            millis_since = ctypes.windll.kernel32.GetTickCount() - info.dwTime
            if millis_since / 1000.0 <= idle_threshold_seconds:
                return _now_iso()
            return None
        except Exception:
            return None

    # ---- log reading -----------------------------------------------------
    def sync(self, start: dt.date, end: dt.date):
        if not self._ok:
            return [], []
        events: list[dict] = []
        events += self._read_security(start, end)
        events += self._read_system(start, end)
        events += self._read_wlan(start, end)
        events.sort(key=lambda e: e["ts"])
        return events, []   # live samples are added continuously by the poller

    # -- helpers below use win32evtlog; wrapped so failures are non-fatal --
    def _query(self, channel: str, ids: set[int], start: dt.date, end: dt.date):
        import win32evtlog
        results = []
        lo = dt.datetime(start.year, start.month, start.day)
        hi = dt.datetime(end.year, end.month, end.day, 23, 59, 59)
        try:
            q = win32evtlog.EvtQuery(
                channel, win32evtlog.EvtQueryReverseDirection, None, None)
        except Exception:
            return results
        while True:
            try:
                handles = win32evtlog.EvtNext(q, 64)
            except Exception:
                break
            if not handles:
                break
            for h in handles:
                try:
                    xml = win32evtlog.EvtRender(h, win32evtlog.EvtRenderEventXml)
                    parsed = self._parse_xml(xml)
                    if parsed and parsed["eid"] in ids:
                        t = parsed["ts"]
                        if lo <= t <= hi:
                            results.append(parsed)
                except Exception:
                    continue
        return results

    @staticmethod
    def _parse_xml(xml: str) -> dict | None:
        import xml.etree.ElementTree as ET
        try:
            ns = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
            root = ET.fromstring(xml)
            eid = int(root.findtext(".//e:EventID", default="0", namespaces=ns))
            ts_raw = root.find(".//e:TimeCreated", ns).attrib["SystemTime"]
            # Windows event timestamps are UTC; convert to local time then drop
            # tzinfo so this matches every other naive datetime in the app
            # (samples, engine.py's _t(), the day-range bounds in _query()
            # below). Leaving it tz-aware made lo <= t <= hi raise TypeError
            # on every single event, silently swallowed by the except below —
            # no event from any channel (Security/System/WLAN) ever actually
            # passed the date-range filter.
            ts = dt.datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
            data = {d.attrib.get("Name"): (d.text or "")
                    for d in root.findall(".//e:Data", ns)}
            return {"eid": eid, "ts": ts, "data": data}
        except Exception:
            return None

    def _read_security(self, start, end):
        out = []
        for p in self._query("Security", {4624, 4634, 4647, 4800, 4801}, start, end):
            eid, data = p["eid"], p["data"]
            if eid == 4624:
                if data.get("LogonType", "") not in _INTERACTIVE_TYPES:
                    continue
                out.append(self._ev(p, "human", "4624", "Interactive logon"))
            elif eid in (4634, 4647):
                out.append(self._ev(p, "human", "4634", "Signed out"))
            elif eid == 4800:
                out.append(self._ev(p, "device", "4800", "Workstation locked"))
            elif eid == 4801:
                out.append(self._ev(p, "human", "4801", "Workstation unlocked"))
        return out

    def _read_system(self, start, end):
        out = []
        for p in self._query("System", {42, 1}, start, end):
            if p["eid"] == 42:
                out.append(self._ev(p, "device", "kp42", "Entered sleep"))
            else:
                out.append(self._ev(p, "device", "pt1", "Resumed"))
        return out

    def _read_wlan(self, start, end):
        out = []
        chan = "Microsoft-Windows-WLAN-AutoConfig/Operational"
        for p in self._query(chan, {8001, 8003}, start, end):
            ssid = p["data"].get("SSID") or p["data"].get("ProfileName")
            code = "8001" if p["eid"] == 8001 else "8003"
            e = self._ev(p, "device", code, f"Wi-Fi {'connect' if code=='8001' else 'disconnect'}")
            e["ssid"] = ssid
            out.append(e)
        return out

    @staticmethod
    def _ev(p, kind, code, detail):
        return {"ts": p["ts"].isoformat(timespec="seconds"),
                "kind": kind, "code": code, "detail": detail, "source": "winlog"}
