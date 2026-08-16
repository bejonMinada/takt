"""Encrypted export / import of records by day-range, week, month, or year.

Exports are AES-256-GCM encrypted (see crypto.py) so the file is private and
casual edits are blocked. Imported records are tagged source='import' and are
kept separate from collected data — never counted as evidence.
"""
from __future__ import annotations
import json
import datetime as dt
from . import crypto


def export_bytes(key: bytes, events: list[dict], samples: list[str],
                 meta: dict) -> bytes:
    payload = {"format": "takt-backup/1", "meta": meta,
               "exported_at": dt.datetime.now().isoformat(timespec="seconds"),
               "events": events, "samples": samples}
    raw = json.dumps(payload).encode()
    return crypto.encrypt(key, raw)


def import_bytes(key: bytes, blob: bytes) -> dict:
    raw = crypto.decrypt(key, blob)          # raises if wrong key / not ours
    data = json.loads(raw)
    if not str(data.get("format", "")).startswith("takt-backup"):
        raise ValueError("File is not a Takt backup.")
    # re-tag everything as user-supplied
    for e in data.get("events", []):
        e["source"] = "import"
    return {"events": data.get("events", []),
            "samples": data.get("samples", []),
            "sha256": crypto.sha256_bytes(blob),
            "rows": len(data.get("events", [])) + len(data.get("samples", []))}
