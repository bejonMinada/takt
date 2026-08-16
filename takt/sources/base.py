"""Event-source interface.

A source produces two things when asked to sync a date range:
  events  - list of dicts: {ts, kind, code, detail?, ssid?, source}
  samples - list of ISO timestamps where real input was seen (idle poller)

The Windows source reads real logs + polls GetLastInputInfo. The demo source
fabricates a realistic set so the app is usable anywhere.
"""
from __future__ import annotations
import datetime as dt


class EventSource:
    name = "base"

    def sync(self, start: dt.date, end: dt.date) -> tuple[list[dict], list[str]]:
        raise NotImplementedError

    def poll_active(self) -> str | None:
        """Return an ISO timestamp if a human is currently active, else None.
        Only the Windows source implements live polling."""
        return None

    def day_overrides(self, start: dt.date, end: dt.date) -> dict:
        """Optional {day -> type} overrides (e.g. planned day off)."""
        return {}
