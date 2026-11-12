"""Local user API token (ADR-017; Phase 3a ADR-026: DPAPI on Windows + rotation).

- POSIX: ``<data_dir>/api_token`` created with mode 0600 (unchanged behaviour).
- Windows: the token is stored as a DPAPI blob (CurrentUser scope) in
  ``<data_dir>/api_token.dpapi``. A pre-3a plaintext ``api_token`` is converted on first load
  and the plaintext file is removed only after the blob round-trips. **Windows behaviour is
  UNVERIFIED until run on a real Windows machine** (see WINDOWS_验收指南.md W21-W24).
- ``rotate_token`` writes a new token atomically. Phase 4 (L4, D10): the running server reads
  the token through ``TokenProvider``, which re-reads the file when its mtime/size changes,
  so a rotation takes effect on the next request without a restart (ADR-026 amended by C55).
The token is never logged.
"""

from __future__ import annotations

import hmac
import os
import secrets
import sys
from pathlib import Path
from typing import Any, Protocol

TOKEN_FILENAME = "api_token"
DPAPI_FILENAME = "api_token.dpapi"
_MIN_LEN = 32


class TokenProtector(Protocol):
    def protect(self, data: bytes) -> bytes: ...
    def unprotect(self, blob: bytes) -> bytes: ...


class DpapiProtector:  # pragma: no cover - Windows only, UNVERIFIED on a real machine
    """CryptProtectData / CryptUnprotectData via ctypes (no extra dependency)."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        self._ct = ctypes
        self._Blob = Blob
        # getattr keeps mypy --strict clean on both Linux (no windll) and Windows.
        windll: Any = getattr(ctypes, "windll")  # noqa: B009
        self._crypt = windll.crypt32
        self._kernel = windll.kernel32

    def _call(self, fn: object, data: bytes) -> bytes:
        ct = self._ct
        buf = ct.create_string_buffer(data, len(data))
        blob_in = self._Blob(len(data), ct.cast(buf, ct.POINTER(ct.c_char)))
        blob_out = self._Blob()
        CRYPTPROTECT_UI_FORBIDDEN = 0x01
        ok = fn(  # type: ignore[operator]
            ct.byref(blob_in), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN,
            ct.byref(blob_out),
        )  # fmt: skip
        if not ok:
            raise OSError("DPAPI call failed")
        try:
            return bytes(ct.string_at(blob_out.pbData, blob_out.cbData))
        finally:
            self._kernel.LocalFree(blob_out.pbData)

    def protect(self, data: bytes) -> bytes:
        return self._call(self._crypt.CryptProtectData, data)

    def unprotect(self, blob: bytes) -> bytes:
        return self._call(self._crypt.CryptUnprotectData, blob)


def default_protector() -> TokenProtector | None:
    if sys.platform == "win32":  # pragma: no cover - UNVERIFIED
        return DpapiProtector()
    return None


def token_path(data_dir: Path) -> Path:
    return data_dir / TOKEN_FILENAME


def dpapi_path(data_dir: Path) -> Path:
    return data_dir / DPAPI_FILENAME


def _write_new(p: Path, data: bytes) -> None:
    """Write via temp file + os.replace (atomic); POSIX mode 0600."""
    tmp = p.with_name(p.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)


def _valid(tok: str) -> str:
    if len(tok) < _MIN_LEN:
        raise ValueError("api_token is invalid; run `ame token rotate` to generate a new one")
    return tok


def _store(data_dir: Path, tok: str, protector: TokenProtector | None) -> None:
    if protector is None:
        _write_new(token_path(data_dir), tok.encode("utf-8"))
        return
    blob = protector.protect(tok.encode("utf-8"))
    if protector.unprotect(blob).decode("utf-8") != tok:
        raise OSError("token protector round-trip failed")
    _write_new(dpapi_path(data_dir), blob)
    plain = token_path(data_dir)
    if plain.exists():
        plain.unlink()  # plaintext removed only after the protected copy is verified


def load_or_create_token(data_dir: Path, protector: TokenProtector | None = None) -> str:
    """``protector=None`` -> POSIX 0600 file. Callers on Windows pass default_protector()."""
    data_dir.mkdir(parents=True, exist_ok=True)
    if protector is not None:
        dp = dpapi_path(data_dir)
        if dp.exists():
            return _valid(protector.unprotect(dp.read_bytes()).decode("utf-8").strip())
        plain = token_path(data_dir)
        if plain.exists():  # pre-3a plaintext token: migrate it, keep the same value
            tok = _valid(plain.read_text(encoding="utf-8").strip())
            _store(data_dir, tok, protector)
            return tok
        tok = secrets.token_urlsafe(32)
        _store(data_dir, tok, protector)
        return tok
    p = token_path(data_dir)
    if p.exists():
        return _valid(p.read_text(encoding="utf-8").strip())
    tok = secrets.token_urlsafe(32)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(tok)
    return tok


def rotate_token(data_dir: Path, protector: TokenProtector | None = None) -> str:
    """Replace the user token with a new random one. A running server picks it up on the next
    request via TokenProvider (no restart needed since Phase 4)."""
    data_dir.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    _store(data_dir, tok, protector)
    return tok


def token_matches(expected: str, presented: str | None) -> bool:
    if not presented:
        return False
    return hmac.compare_digest(expected.encode(), presented.encode())


class TokenProvider:
    """Current user token for a running server (Phase 4, L4).

    Caches the token keyed by (file, mtime_ns, size, inode) and re-reads it only when the file
    changed, e.g. after ``ame token rotate``. If the file is missing or unreadable the provider
    fails closed (returns None: every user-token request is rejected) and never falls back to
    an old token. The token value is never logged.
    """

    def __init__(self, data_dir: Path, protector: TokenProtector | None = None) -> None:
        self._dir = data_dir
        self._protector = protector
        self._key: tuple[str, int, int, int] | None = None
        self._token: str | None = load_or_create_token(data_dir, protector)
        self._key = self._stat()

    def _path(self) -> Path:
        return dpapi_path(self._dir) if self._protector is not None else token_path(self._dir)

    def _stat(self) -> tuple[str, int, int, int] | None:
        p = self._path()
        try:
            st = p.stat()
        except OSError:
            return None
        return (str(p), st.st_mtime_ns, st.st_size, st.st_ino)

    def get(self) -> str | None:
        key = self._stat()
        if key is None:
            self._key, self._token = None, None
            return None
        if key != self._key:
            try:
                raw = self._path().read_bytes()
                if self._protector is not None:
                    raw = self._protector.unprotect(raw)
                self._token = _valid(raw.decode("utf-8").strip())
            except (OSError, ValueError):
                self._token = None
            self._key = key
        return self._token
