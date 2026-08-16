# Takt — project context for Claude Code

This file is auto-loaded by Claude Code. Read it before making changes.

## What this is

An offline, personal attendance-corroboration app for Windows, plus a
Gantt/Kanban project-management module. Built as a pywebview desktop app
(Python backend, HTML/JS/CSS frontend, SQLite storage). No server, no
internet required at runtime.

**Why it exists:** the person's company badge system sometimes fails to
register in/out punches. Takt watches when the laptop was actually active
(logons, unlocks, sleep/wake, Wi-Fi) and derives daily time-in/time-out as a
**fallback corroboration signal** — never the primary record. This framing
drives real design decisions below; don't quietly relax it.

## How this project was built (important context)

Everything so far was built and tested in a **Linux sandbox**, not on the
target Windows machine. That means:

- `takt/engine.py`, `takt/db.py`, `takt/crypto.py`, `takt/backup.py`, the
  project/task CRUD in `takt/api.py`, and the demo source
  (`takt/sources/demo.py`) are **fully tested** — see "Known-good" below.
- `takt/sources/windows.py` (real Event Log reading via `win32evtlog`, and
  `GetLastInputInfo` idle polling) is **written but never executed** — pywin32
  isn't available on Linux. Treat this file as the first thing to validate
  once you're running on the actual Windows laptop.
- The frontend (`web/index.html`) was checked with a headless Chromium via
  Playwright, but `pywebview`'s actual embedded browser (Edge WebView2 on
  Windows) was never used. Watch for pywebview-specific quirks (file dialogs,
  the JS↔Python bridge, `window.pywebview.api` timing on load).
- CDN dependencies (Google Fonts, Chart.js from cdnjs) were unreachable from
  the sandbox's network egress, so the trend chart and wordmark font were
  never visually verified end-to-end. They should Just Work on a normal
  internet connection, but check first.

## Architecture

```
main.py              plain launcher (closing the window quits) — good for quick manual testing
tray.py               real launcher: closing the window HIDES it to a tray icon; Quit actually exits
setup_autostart.bat    ONE-TIME, run as Administrator: registers a Scheduled Task
                        ("Takt", ONLOGON, RL HIGHEST) so tray.py starts at every login
run.bat                creates a venv, installs requirements.txt, runs tray.py
takt/
  config.py            Settings dataclasses (Shift, Engine thresholds), data_dir()
                        resolves to %LOCALAPPDATA%\Takt on Windows
  db.py                 SQLite schema + access. Tables: raw_events, activity_samples,
                        daily (derived + hash chain), day_types, settings, import_log,
                        projects, tasks
  engine.py             THE CORE LOGIC — build_day(). See "Session engine" below.
  crypto.py             AES-256-GCM backups + DPAPI key sealing + hash chain
                        (chain_hash/verify_chain). Read the module docstring —
                        the tamper-evidence scope is deliberately limited and
                        documented; don't oversell it in UI copy.
  backup.py              encrypted export/import; imported rows tagged source='import'
  api.py                 Api class — every method is called from JS as
                        window.pywebview.api.<name>(...). This is the whole
                        surface between frontend and backend.
  sources/
    base.py               EventSource interface
    demo.py                fabricates a realistic fortnight, INCLUDING the two
                        trickiest cases on purpose (see below) — keep these
                        scenarios in the demo data if you touch this file
    windows.py             real Windows log reader + idle poller — UNTESTED, see above
web/
  index.html             single-file frontend (vanilla JS, no build step, no
                        framework). Falls back to demo.js + in-memory CRUD
                        when window.pywebview is absent, so it's previewable
                        in any browser.
  demo.js                 generated, not hand-written — see "Regenerating demo.js"
  assets/logo.png, mark.png   the Takt logo (clock+T mark); mark.png is the
                        square icon-only crop used in the titlebar
```

## Session engine (`takt/engine.py`) — the part that must stay correct

Two rules make this smarter than "first event of the day = time in, last
event = time out". Do not weaken these without understanding why they're
here — they were added in response to specific real scenarios:

1. **A Wi-Fi reconnect alone never sets time-out.** Device-only signals
   (Wi-Fi connect/disconnect, lock, logoff, sleep) can't end or extend a work
   session by themselves. Only human-active signals (interactive logon,
   unlock) or live input samples do that. This exists because: someone closes
   their laptop lid at the office and goes home; the laptop rejoins home
   Wi-Fi in the evening with nobody touching it. Without this rule, that
   phantom reconnect would silently inflate their recorded hours.

2. **Real work sessions come from idle-sampling, not just discrete events.**
   While the app runs, `sources/windows.py` polls `GetLastInputInfo` every
   `Engine.sample_seconds`. Consecutive active samples become a session;
   gaps longer than `idle_end_minutes` close it; short gaps under
   `bridge_gap_minutes` get bridged (so a bathroom break doesn't fragment the
   day). This is what correctly captures someone who genuinely resumes
   working from home in the evening as a **separate, correctly-located**
   session, while still filtering the phantom-reconnect case above.

`takt/sources/demo.py` bakes in worked examples of both: a "phantom" day
(office day + a nighttime home Wi-Fi reconnect with no input after — must
NOT create a home session or move time-out) and a "wfh_continues" day
(office session, gap, then a real home session with input). If you touch
the engine, re-run these — they're the regression tests that matter most:

```bash
PYTHONPATH=. python3 -c "
from takt.api import Api
import json
api = Api(); api.sync(30)
ov = json.loads(api.get_overview('range'))
for d in ov['days']:
    if d['state']=='worked':
        print(d['dow'], d['sessions'])
"
```
Look for: the phantom day shows only office sessions and time-out unchanged;
the wfh_continues day shows two sessions with different `location` values.

## Honesty constraints (apply to any UI/copy changes too)

- Every screen carries a persistent disclaimer: device-activity record, not
  certified presence. Don't remove it or bury it.
- Backups are encrypted (AES-256-GCM, key sealed via DPAPI) which stops
  casual tampering and keeps the file private — but this is **not** proof
  against the machine's own owner, since the key lives on the same machine.
  The hash chain makes edits *detectable*, not impossible. Don't let UI copy
  imply stronger guarantees than that.
- Imported backups are tagged `source='import'` and must stay visually and
  logically separate from collected data — never counted as evidence.
- VPN/proxy client state (e.g. Zscaler) can't be read (its logs aren't in
  Event Viewer — verified by web search, not assumption). Don't name a
  specific vendor in UI copy — the software in use varies by user/employer
  and can change over time; keep it generic ("VPN/proxy client"). Location is
  inferred from Wi-Fi SSID only; no-Wi-Fi days should show "unknown", never a
  guess.

## Regenerating `web/demo.js`

It's generated from the real engine, not hand-typed, so the browser preview
has genuine fidelity. Regenerate after any backend/data-shape change:

```bash
cd takt   # repo root (contains web/, takt/, main.py...)
rm -rf /tmp/regen && mkdir /tmp/regen && HOME=/tmp/regen PYTHONPATH=. python3 -c "
import json
from takt.api import Api
api = Api(); api.sync(30)
payloads = {p: json.loads(api.get_overview(p)) for p in ['range','week','month','year']}
days = {d['day']: json.loads(api.get_day(d['day'])) for d in payloads['range']['days']}
settings = json.loads(api.get_settings())
integ = json.loads(api.integrity())
projects = json.loads(api.list_projects())
tasks = {p['id']: json.loads(api.list_tasks(p['id'])) for p in projects}
out = {'overview': payloads, 'day': days, 'settings': settings, 'integrity': integ,
       'projects': projects, 'tasks': tasks}
open('web/demo.js','w').write('window.TAKT_DEMO=' + json.dumps(out) + ';')
"
```

## Known TODOs (roughly priority order)

1. **Validate `sources/windows.py` for real** on the actual laptop: does
   `win32evtlog.EvtQuery` against the Security channel succeed, or does it
   need elevation / Event Log Readers group membership? Confirm the XML
   parsing (`_parse_xml`) matches real event schemas — it was written against
   documented formats, never against live output.
2. Visually verify Kanban board + task editor drawer in a real browser/
   pywebview (only Overview and Gantt were screenshot-checked so far).
3. Wire the Shift & schedule "Save changes" button all the way through on a
   real pywebview run — `saveShift()` calls `save_settings`, unit-tested at
   the API layer, but never exercised via an actual click in pywebview.
4. `main.py`'s `on_closing` hide-not-quit handler is defined but unused
   (that behavior actually lives in `tray.py`). Decide if `main.py` should be
   deleted in favor of always using `tray.py`, or kept as a deliberately
   simpler dev-mode entry point.
5. No installer yet. Natural next step: a PyInstaller build
   (`pyinstaller --onefile --windowed main.py` or `tray.py`, bundling
   `web/` as data) for a single distributable `.exe`.
6. Gantt bar text overflows/truncates on short-duration tasks — cosmetic,
   low priority.

## Conventions to keep

- No build step, no npm — the frontend is one HTML file with inline
  `<style>`/`<script>`. Keep it that way unless there's a strong reason not
  to; it's part of why this is easy to hand-run with `run.bat`.
- Palette/type system: near-black slate UI (`--ink`, `--paper`), teal accent
  (`--accent: #0E7C86`), monospace (`IBM Plex Mono`) for all clock times and
  dates, `Space Grotesk` for headings — matches the logo. Don't introduce a
  second accent color without a reason.
- Every backend method exposed to the frontend returns a JSON string (not a
  raw dict) — `Api` methods all do `return json.dumps(...)`, and the frontend
  always does `JSON.parse(await window.pywebview.api.fn(...))`. Keep this
  pattern for new methods.
- The frontend has a `HAS_API` fork throughout: real pywebview calls vs. an
  in-memory demo fallback (`DL` object for projects/tasks, direct
  `window.TAKT_DEMO` reads for attendance). New features should follow the
  same fallback pattern so the browser-preview workflow keeps working.
