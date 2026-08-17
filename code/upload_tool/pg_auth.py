"""Resolve Postgres password without writing it to disk."""

from __future__ import annotations

import base64
import os
import sqlite3
from pathlib import Path


def resolve_password(form_password: str = "") -> str:
    if form_password:
        return form_password
    for key in ("SYNTH_LOG_PGPASSWORD", "PGPASSWORD"):
        value = os.environ.get(key)
        if value:
            return value
    return _pgadmin_password()


def _pgadmin_password() -> str:
    try:
        import ctypes
        from ctypes import wintypes

        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives.ciphers import Cipher
        from cryptography.hazmat.primitives.ciphers.algorithms import AES

        try:
            from cryptography.hazmat.decrepit.ciphers.modes import CFB8
        except ImportError:
            from cryptography.hazmat.primitives.ciphers.modes import CFB8
    except ImportError:
        return ""

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", wintypes.LPVOID),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    CredReadW = advapi32.CredReadW
    CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(CREDENTIAL)),
    ]
    CredReadW.restype = wintypes.BOOL
    ptr = ctypes.POINTER(CREDENTIAL)()
    if not CredReadW("pgAdmin4", 1, 0, ctypes.byref(ptr)):
        return ""
    cred = ptr.contents
    master = bytes(cred.CredentialBlob[: cred.CredentialBlobSize]).decode("utf-16-le").rstrip("\x00")
    advapi32.CredFree(ptr)
    db = Path.home() / "AppData/Roaming/pgAdmin/pgadmin4.db"
    if not db.exists():
        return ""
    hex_pw = sqlite3.connect(str(db)).execute("SELECT password FROM server WHERE id=1").fetchone()
    if not hex_pw:
        return ""
    raw = base64.b64decode(bytes.fromhex(hex_pw[0]).decode("ascii"))
    key = master.encode()[:32]
    if len(key) not in (16, 24, 32):
        key = key.ljust(32, b"}")
    iv = AES.block_size // 8
    decryptor = Cipher(AES(key), CFB8(raw[:iv]), default_backend()).decryptor()
    return (decryptor.update(raw[iv:]) + decryptor.finalize()).decode("utf-8")
