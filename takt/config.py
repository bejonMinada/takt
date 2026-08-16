"""Takt configuration: paths, shift definition, and the tunable thresholds
that make the activity-session engine behave sensibly.

Everything here is a *default*. Real values live in the SQLite `settings` table
and are edited from the app's Shift & schedule screen; this module just seeds
them and provides the data paths.
"""
from __future__ import annotations
import os
import sys
import json
from dataclasses import dataclass, asdict, field


def data_dir() -> str:
    """Per-user writable location for the database, key, and logs.

    TAKT_DATA_DIR overrides this — used by demo.py to keep presentation
    data fully isolated from the real captured data.
    """
    override = os.environ.get("TAKT_DATA_DIR")
    if override:
        os.makedirs(override, exist_ok=True)
        return override
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "Takt")
    else:
        d = os.path.join(os.path.expanduser("~"), ".takt")
    os.makedirs(d, exist_ok=True)
    return d


DB_PATH = os.path.join(data_dir(), "takt.db")
KEY_PATH = os.path.join(data_dir(), "takt.key")


@dataclass
class Shift:
    # Nominal shift window (local clock, "HH:MM").
    start: str = "08:00"
    end: str = "17:00"
    # Flexibility in minutes: how much earlier you may start / later you may end
    # and still be "on shift".
    flex_minutes: int = 120
    # Paid hours expected per day. Used for under/overtime.
    required_minutes: int = 480          # 8h
    # Break length subtracted from a raw span before comparing to required
    # (only used in the event-only fallback; the session engine excludes idle
    # breaks automatically).
    break_minutes: int = 60


@dataclass
class Engine:
    # Idle poll cadence (seconds). The tray app samples input this often.
    sample_seconds: int = 60
    # If input-idle exceeds this, the person is considered away → a session ends.
    idle_end_minutes: int = 20
    # Gaps between active points shorter than this are bridged into one session
    # (a short coffee break shouldn't split your day).
    bridge_gap_minutes: int = 10
    # Sessions shorter than this are dropped as noise.
    min_session_minutes: int = 5


@dataclass
class Settings:
    shift: Shift = field(default_factory=Shift)
    engine: Engine = field(default_factory=Engine)
    # SSIDs that mean "at the office". Anything else → Home. Empty/none → Unknown.
    office_ssids: list[str] = field(default_factory=lambda: ["OFFICE-WIFI", "OFFICE-WIFI-5G"])
    auto_wfh: bool = True
    # Which event source to use: "windows" (real logs) or "demo" (sample data).
    source: str = "demo" if sys.platform != "win32" else "windows"
    # Folder exports are saved to by default. None = ask every time.
    backup_dir: str | None = None
    # Calendar week start, Python date.weekday() convention: 0=Monday..6=Sunday.
    # Defaults to Sunday, the common convention in the Philippines.
    week_start: int = 6
    # Recurring weekly days off (same 0=Monday..6=Sunday convention), any
    # count from 1 to 7 — covers compressed workweeks as well as the usual
    # 2-day weekend. This is a *default*, not an override: a day with real
    # captured activity or an explicit day_types/leave entry always takes
    # precedence, so working an "off" day still shows up correctly.
    off_days: list[int] = field(default_factory=lambda: [5, 6])

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @staticmethod
    def from_json(s: str) -> "Settings":
        raw = json.loads(s)
        return Settings(
            shift=Shift(**raw.get("shift", {})),
            engine=Engine(**raw.get("engine", {})),
            office_ssids=raw.get("office_ssids", []),
            auto_wfh=raw.get("auto_wfh", True),
            source=raw.get("source", "demo"),
            backup_dir=raw.get("backup_dir"),
            week_start=raw.get("week_start", 6),
            off_days=raw.get("off_days", [5, 6]),
        )


def default_settings() -> Settings:
    return Settings()
