"""Thin REST client used by the MCP adapter (D-Q4). Stdlib only (urllib), loopback only.

Writes carry an ``Idempotency-Key``: the caller's if given, else ``mcp-<uuid4>`` generated once per
tool call and reused on every retry (ADR-027/028). Retries: up to 2 on network error / 5xx, and
1 after 0.5 s on 409 IDEMPOTENCY_IN_PROGRESS. The key is sent only in the Authorization header and
never logged or returned.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode, urlparse

from ame.adapters.mcp_constants import MCP_USER_AGENT_PREFIX

VERSION = "0.1.0"
USER_AGENT = f"{MCP_USER_AGENT_PREFIX}{VERSION}"


@dataclass(frozen=True)
class RawResponse:
    status: int
    headers: dict[str, str]
    body: bytes


Transport = Callable[[str, str, dict[str, str], bytes | None], RawResponse]


class Unavailable(Exception):
    """AME REST service is not reachable."""


def ensure_background_server(base_url: str, wait_s: float = 5.0) -> bool:
    """Ensure the local AME REST service is running silently in the background."""
    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8765
    health_url = f"http://{host}:{port}/api/v1/health"
    try:
        with urllib.request.urlopen(health_url, timeout=0.8) as r:  # noqa: S310 - loopback only
            if int(r.status) == 200:
                return True
    except (urllib.error.URLError, OSError, ValueError):
        pass

    if os.environ.get("AME_MCP_AUTO_START", "true").lower() in {"0", "false", "no"}:
        return False

    env = dict(os.environ)
    env.setdefault("AME_MCP_ENABLED", "true")
    env.setdefault("AME_MCP_READ_ONLY", "false")
    env.setdefault("AME_AUTO_APPROVE_AGENT_WRITES", "true")
    env["AME_HOST"] = host
    env["AME_PORT"] = str(port)

    exe = sys.executable
    try:
        if os.name == "nt":
            pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
            if os.path.exists(pyw):
                exe = pyw
            flags = 0x00000008 | 0x00000200 | 0x01000000
            try:
                subprocess.Popen(
                    [exe, "-m", "ame.main"],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    creationflags=flags,
                )
            except OSError:
                subprocess.Popen(
                    [exe, "-m", "ame.main"],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    creationflags=0x00000008 | 0x00000200,
                )
        else:
            subprocess.Popen(
                [exe, "-m", "ame.main"],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
    except OSError:
        return False

    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=0.5) as r:  # noqa: S310
                if int(r.status) == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.15)
    return False


def urllib_transport(base_url: str, timeout: float = 30.0) -> Transport:
    def send(method: str, path: str, headers: dict[str, str], body: bytes | None) -> RawResponse:
        req = urllib.request.Request(base_url + path, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - loopback only
                return RawResponse(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
        except urllib.error.HTTPError as e:
            return RawResponse(e.code, {k.lower(): v for k, v in e.headers.items()}, e.read())
        except (urllib.error.URLError, OSError) as e:
            if ensure_background_server(base_url):
                try:
                    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
                        return RawResponse(
                            r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
                        )
                except urllib.error.HTTPError as he:
                    return RawResponse(
                        he.code, {k.lower(): v for k, v in he.headers.items()}, he.read()
                    )
                except (urllib.error.URLError, OSError):
                    pass
            raise Unavailable(str(getattr(e, "reason", e))) from None

    return send


@dataclass
class ApiResult:
    status: int
    data: Any
    replayed: bool = False
    error_code: str | None = None
    error_message: str = ""
    request_id: str | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def seg(v: str) -> str:
    return quote(v, safe="")


class RestClient:
    def __init__(
        self,
        transport: Transport,
        api_key: str,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 2,
    ) -> None:
        self._t = transport
        self._key = api_key
        self._sleep = sleep
        self._max = max_retries

    def get(self, path: str, query: dict[str, Any] | None = None) -> ApiResult:
        q = {k: v for k, v in (query or {}).items() if v is not None}
        full = f"{path}?{urlencode(q, doseq=False)}" if q else path
        return self._send("GET", full, None, None)

    def post_read(self, path: str, body: dict[str, Any]) -> ApiResult:
        return self._send("POST", path, body, None)

    def write(
        self, path: str, body: dict[str, Any], idempotency_key: str | None = None
    ) -> ApiResult:
        key = idempotency_key or f"mcp-{uuid.uuid4()}"
        return self._send("POST", path, body, key)

    def _send(
        self, method: str, path: str, body: dict[str, Any] | None, idem: str | None
    ) -> ApiResult:
        headers = {
            "Authorization": f"Bearer {self._key}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        raw_body = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if idem is not None:
            headers["Idempotency-Key"] = idem
        attempts, in_progress_retry = 0, False
        while True:
            try:
                r = self._t(method, "/api/v1" + path, headers, raw_body)
            except Unavailable:
                if attempts < self._max:
                    attempts += 1
                    self._sleep(0.2 * attempts)
                    continue
                msg = "AME service is not running on this computer"
                return ApiResult(0, None, error_code="AME_UNAVAILABLE", error_message=msg)
            res = _parse(r)
            if r.status >= 500 and attempts < self._max:
                attempts += 1
                self._sleep(0.2 * attempts)
                continue
            if res.error_code == "IDEMPOTENCY_IN_PROGRESS" and not in_progress_retry:
                in_progress_retry = True
                self._sleep(0.5)
                continue
            return res


def _parse(r: RawResponse) -> ApiResult:
    try:
        data: Any = json.loads(r.body.decode("utf-8")) if r.body else None
    except (ValueError, UnicodeDecodeError):
        data = None
    replayed = r.headers.get("idempotency-replayed", "").lower() == "true"
    rid = r.headers.get("x-request-id")
    if 200 <= r.status < 300:
        return ApiResult(r.status, data, replayed=replayed, request_id=rid)
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return ApiResult(r.status, None, replayed, str(err.get("code", "ERROR")),
                         str(err.get("message", "")), err.get("request_id") or rid)  # fmt: skip
    return ApiResult(r.status, None, replayed, f"HTTP_{r.status}", "request failed", rid)
