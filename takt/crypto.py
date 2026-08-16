"""Local encryption for backups + a hash chain for tamper-evidence.

Honest scope (this is by design, not a shortcut): on a fully offline app that
runs on the user's own machine, encryption stops *casual* tampering and keeps
the backup private. It is NOT proof against the machine's owner, because the key
lives on that machine. The hash chain makes edits to past records *detectable*,
not impossible.

Key handling:
  - Windows: a random 32-byte key is sealed with DPAPI (CryptProtectData) and
    stored as an opaque blob, so it can't be reused on another machine/account.
  - Elsewhere (dev): the key is written to a 0600 file with a warning. The app
    targets Windows; this path only exists so it runs during development.
"""
from __future__ import annotations
import os
import sys
import json
import hashlib
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"TAKT1"


# ---- key storage ---------------------------------------------------------
def _seal(raw: bytes) -> bytes:
    if sys.platform == "win32":
        import win32crypt  # type: ignore
        return b"DPAPI" + win32crypt.CryptProtectData(raw, "takt", None, None, None, 0)
    return b"PLAIN" + raw


def _unseal(blob: bytes) -> bytes:
    tag, body = blob[:5], blob[5:]
    if tag == b"DPAPI":
        import win32crypt  # type: ignore
        return win32crypt.CryptUnprotectData(body, None, None, None, 0)[1]
    return body


def load_or_create_key(key_path: str) -> bytes:
    if os.path.exists(key_path):
        with open(key_path, "rb") as f:
            return _unseal(f.read())
    key = AESGCM.generate_key(bit_length=256)
    with open(key_path, "wb") as f:
        f.write(_seal(key))
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return key


# ---- encrypt / decrypt payloads -----------------------------------------
def encrypt(key: bytes, plaintext: bytes) -> bytes:
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext, MAGIC)
    return MAGIC + nonce + ct


def decrypt(key: bytes, blob: bytes) -> bytes:
    if blob[:len(MAGIC)] != MAGIC:
        raise ValueError("Not a Takt backup file.")
    nonce = blob[len(MAGIC):len(MAGIC) + 12]
    ct = blob[len(MAGIC) + 12:]
    return AESGCM(key).decrypt(nonce, ct, MAGIC)


# ---- hash chain ----------------------------------------------------------
def chain_hash(prev_hash: str | None, payload: str) -> str:
    h = hashlib.sha256()
    h.update((prev_hash or "").encode())
    h.update(payload.encode())
    return h.hexdigest()


def verify_chain(rows: list[dict]) -> bool:
    """rows: list of {'payload','prev_hash','hash'} ordered by day."""
    prev = None
    for r in rows:
        if r.get("prev_hash") != prev:
            return False
        if chain_hash(prev, r["payload"]) != r["hash"]:
            return False
        prev = r["hash"]
    return True


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()
