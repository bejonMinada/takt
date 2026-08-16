"""SQLite storage.

Two kinds of captured data:
  raw_events       - discrete Windows events (logon, logoff, lock, unlock,
                     wifi connect/disconnect, sleep/wake). Append-only.
  activity_samples - timestamps where real keyboard/mouse input was seen, taken
                     by the tray poller. This is what makes accurate sessions
                     possible.

`daily` is derived by the engine and is safe to rebuild from the two tables
above. `settings` holds the single JSON settings blob. `day_types` records
manual overrides (day off). `import_log` records every backup import for audit.
"""
from __future__ import annotations
import sqlite3
import datetime as dt
from contextlib import contextmanager


SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT    NOT NULL,          -- ISO local timestamp
    day        TEXT    NOT NULL,          -- YYYY-MM-DD
    kind       TEXT    NOT NULL,          -- 'human' | 'device'
    code       TEXT    NOT NULL,          -- '4624','4801','8001',...
    detail     TEXT,                      -- e.g. logon type, description
    ssid       TEXT,                      -- for wifi events
    source     TEXT    NOT NULL DEFAULT 'winlog',  -- 'winlog'|'demo'|'import'
    UNIQUE(ts, code, source)
);
CREATE TABLE IF NOT EXISTS activity_samples (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    day    TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'poller',
    UNIQUE(ts, source)
);
CREATE TABLE IF NOT EXISTS daily (
    day        TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,             -- JSON summary produced by engine
    prev_hash  TEXT,                      -- hash chain (tamper-evidence)
    hash       TEXT,
    computed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS day_types (
    day  TEXT PRIMARY KEY,
    type TEXT NOT NULL                    -- 'dayoff'
);
CREATE TABLE IF NOT EXISTS leave_blocks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    day        TEXT NOT NULL,             -- YYYY-MM-DD
    full_day   INTEGER NOT NULL DEFAULT 0,
    start_time TEXT,                      -- HH:MM, NULL when full_day
    end_time   TEXT,                      -- HH:MM, NULL when full_day
    leave_type TEXT NOT NULL DEFAULT 'leave', -- 'leave' | 'holiday' | 'sick' | 'other'
    note       TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_leave_day ON leave_blocks(day);
CREATE TABLE IF NOT EXISTS settings (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS import_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    filename TEXT,
    sha256   TEXT,
    rows     INTEGER
);
CREATE TABLE IF NOT EXISTS projects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    color      TEXT NOT NULL DEFAULT '#0E7C86',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    name       TEXT NOT NULL,
    start_date TEXT NOT NULL,          -- YYYY-MM-DD
    end_date   TEXT NOT NULL,          -- YYYY-MM-DD, inclusive
    status     TEXT NOT NULL DEFAULT 'todo',   -- 'todo' | 'doing' | 'done'
    sort_order INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(project_id) REFERENCES projects(id)
);
CREATE INDEX IF NOT EXISTS ix_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS ix_raw_day ON raw_events(day);
CREATE INDEX IF NOT EXISTS ix_samp_day ON activity_samples(day);
"""


class DB:
    def __init__(self, path: str):
        self.path = path
        # timeout=30: if a second process (e.g. autostart + a manual launch
        # both running) holds the write lock, wait instead of failing fast —
        # the default 5s timeout was silently dropping writes under exactly
        # that contention, since several callers (the idle-poll thread) swallow
        # exceptions. WAL also lets readers keep working while one writer holds
        # the lock, instead of blocking everything on every write.
        self._c = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._c.execute("PRAGMA journal_mode=WAL")
        self._c.row_factory = sqlite3.Row
        self._c.executescript(SCHEMA)
        # Migration: leave_blocks predates leave_type. CREATE TABLE IF NOT
        # EXISTS above is a no-op on an existing table, so add the column
        # directly for installs that already have the table.
        try:
            self._c.execute("ALTER TABLE leave_blocks ADD COLUMN leave_type TEXT NOT NULL DEFAULT 'leave'")
        except sqlite3.OperationalError:
            pass  # column already exists
        self._c.commit()

    @contextmanager
    def cur(self):
        c = self._c.cursor()
        try:
            yield c
            self._c.commit()
        finally:
            c.close()

    # ---- events & samples -------------------------------------------------
    def add_events(self, events: list[dict]) -> int:
        n = 0
        with self.cur() as c:
            for e in events:
                try:
                    c.execute(
                        "INSERT OR IGNORE INTO raw_events(ts,day,kind,code,detail,ssid,source)"
                        " VALUES(?,?,?,?,?,?,?)",
                        (e["ts"], e["ts"][:10], e["kind"], e["code"],
                         e.get("detail"), e.get("ssid"), e.get("source", "winlog")),
                    )
                    n += c.rowcount
                except sqlite3.Error:
                    pass
        return n

    def add_samples(self, timestamps: list[str], source: str = "poller") -> int:
        n = 0
        with self.cur() as c:
            for ts in timestamps:
                c.execute("INSERT OR IGNORE INTO activity_samples(ts,day,source) VALUES(?,?,?)",
                          (ts, ts[:10], source))
                n += c.rowcount
        return n

    def events_for_day(self, day: str) -> list[dict]:
        with self.cur() as c:
            c.execute("SELECT * FROM raw_events WHERE day=? ORDER BY ts", (day,))
            return [dict(r) for r in c.fetchall()]

    def samples_for_day(self, day: str) -> list[str]:
        with self.cur() as c:
            c.execute("SELECT ts FROM activity_samples WHERE day=? ORDER BY ts", (day,))
            return [r["ts"] for r in c.fetchall()]

    def known_ssids(self) -> list[dict]:
        """Distinct Wi-Fi SSIDs actually seen in captured WLAN events, most
        recently-seen first — backs the office-network picker so a user
        selects from what's real instead of typing a name by hand."""
        with self.cur() as c:
            c.execute(
                "SELECT ssid, MAX(ts) AS last_seen, COUNT(*) AS n FROM raw_events"
                " WHERE code IN ('8001','8003') AND ssid IS NOT NULL AND ssid != ''"
                " GROUP BY ssid ORDER BY last_seen DESC"
            )
            return [dict(r) for r in c.fetchall()]

    def days_in_range(self, start: str, end: str) -> list[str]:
        with self.cur() as c:
            c.execute(
                "SELECT DISTINCT day FROM ("
                " SELECT day FROM raw_events WHERE day BETWEEN ? AND ?"
                " UNION SELECT day FROM activity_samples WHERE day BETWEEN ? AND ?"
                " UNION SELECT day FROM day_types WHERE day BETWEEN ? AND ?"
                " UNION SELECT day FROM leave_blocks WHERE day BETWEEN ? AND ?"
                ") ORDER BY day",
                (start, end, start, end, start, end, start, end),
            )
            return [r["day"] for r in c.fetchall()]

    # ---- day types --------------------------------------------------------
    def set_day_type(self, day: str, type_: str | None):
        with self.cur() as c:
            if type_ is None:
                c.execute("DELETE FROM day_types WHERE day=?", (day,))
            else:
                c.execute("INSERT OR REPLACE INTO day_types(day,type) VALUES(?,?)", (day, type_))

    def day_type(self, day: str) -> str | None:
        with self.cur() as c:
            c.execute("SELECT type FROM day_types WHERE day=?", (day,))
            r = c.fetchone()
            return r["type"] if r else None

    # ---- leave blocks -------------------------------------------------------
    def add_leave(self, day: str, full_day: bool, start_time: str | None,
                  end_time: str | None, note: str | None,
                  leave_type: str = "leave") -> int:
        with self.cur() as c:
            c.execute(
                "INSERT INTO leave_blocks(day,full_day,start_time,end_time,leave_type,note,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (day, 1 if full_day else 0, start_time, end_time, leave_type, note,
                 dt.datetime.now().isoformat(timespec="seconds")),
            )
            return c.lastrowid

    def delete_leave(self, leave_id: int):
        with self.cur() as c:
            c.execute("DELETE FROM leave_blocks WHERE id=?", (leave_id,))

    def leave_for_day(self, day: str) -> list[dict]:
        with self.cur() as c:
            c.execute("SELECT * FROM leave_blocks WHERE day=? ORDER BY id", (day,))
            return [dict(r) for r in c.fetchall()]

    # ---- derived daily + hash chain --------------------------------------
    def last_hash(self) -> str | None:
        with self.cur() as c:
            c.execute("SELECT hash FROM daily ORDER BY day DESC LIMIT 1")
            r = c.fetchone()
            return r["hash"] if r else None

    def upsert_daily(self, day: str, payload: str, prev_hash: str | None, hash_: str):
        with self.cur() as c:
            c.execute(
                "INSERT OR REPLACE INTO daily(day,payload,prev_hash,hash,computed_at)"
                " VALUES(?,?,?,?,?)",
                (day, payload, prev_hash, hash_, dt.datetime.now().isoformat(timespec="seconds")),
            )

    def daily_rows(self) -> list[dict]:
        with self.cur() as c:
            c.execute("SELECT * FROM daily ORDER BY day")
            return [dict(r) for r in c.fetchall()]

    # ---- settings ---------------------------------------------------------
    def get_settings(self) -> str | None:
        with self.cur() as c:
            c.execute("SELECT payload FROM settings WHERE id=1")
            r = c.fetchone()
            return r["payload"] if r else None

    def save_settings(self, payload: str):
        with self.cur() as c:
            c.execute("INSERT OR REPLACE INTO settings(id,payload) VALUES(1,?)", (payload,))

    def log_import(self, filename: str, sha256: str, rows: int):
        with self.cur() as c:
            c.execute("INSERT INTO import_log(ts,filename,sha256,rows) VALUES(?,?,?,?)",
                      (dt.datetime.now().isoformat(timespec="seconds"), filename, sha256, rows))

    # ---- projects & tasks (Gantt + Kanban) --------------------------------
    def add_project(self, name: str, color: str) -> int:
        with self.cur() as c:
            c.execute("INSERT INTO projects(name,color,created_at) VALUES(?,?,?)",
                      (name, color, dt.datetime.now().isoformat(timespec="seconds")))
            return c.lastrowid

    def list_projects(self) -> list[dict]:
        with self.cur() as c:
            c.execute("SELECT * FROM projects ORDER BY id")
            return [dict(r) for r in c.fetchall()]

    def delete_project(self, project_id: int):
        with self.cur() as c:
            c.execute("DELETE FROM tasks WHERE project_id=?", (project_id,))
            c.execute("DELETE FROM projects WHERE id=?", (project_id,))

    def add_task(self, project_id: int, name: str, start: str, end: str,
                status: str = "todo", sort_order: int = 0) -> int:
        with self.cur() as c:
            c.execute("INSERT INTO tasks(project_id,name,start_date,end_date,status,sort_order)"
                      " VALUES(?,?,?,?,?,?)", (project_id, name, start, end, status, sort_order))
            return c.lastrowid

    def list_tasks(self, project_id: int) -> list[dict]:
        with self.cur() as c:
            c.execute("SELECT * FROM tasks WHERE project_id=? ORDER BY sort_order, id", (project_id,))
            return [dict(r) for r in c.fetchall()]

    def update_task(self, task_id: int, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.cur() as c:
            c.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))

    def delete_task(self, task_id: int):
        with self.cur() as c:
            c.execute("DELETE FROM tasks WHERE id=?", (task_id,))
