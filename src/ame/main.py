"""Entry point: `ame-server` or `python -m ame.main`."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence

import uvicorn

from ame.adapters.rest.app import create_app
from ame.config import Settings


def is_server_healthy(host: str, port: int, timeout: float = 1.0) -> bool:
    url = f"http://{host}:{port}/api/v1/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - loopback only
            return int(resp.status) == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def stop_running_server(port: int) -> bool:
    """Stop any local process listening on ``port`` (Windows / POSIX)."""
    stopped = False
    if os.name == "nt":
        try:
            raw = subprocess.check_output(
                ["netstat", "-ano", "-p", "TCP"],
                stdin=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
            out = raw.decode("ascii", errors="ignore")
            pids: set[int] = set()
            target = f":{port}"
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[1].endswith(target) and parts[3] == "LISTENING":
                    if parts[4].isdigit() and int(parts[4]) != os.getpid():
                        pids.add(int(parts[4]))
            for pid in pids:
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    creationflags=0x08000000,
                )
                stopped = True
        except OSError:
            pass
    return stopped


def spawn_detached_server(host: str = "127.0.0.1", port: int = 8765, wait_s: float = 6.0) -> bool:
    """Start `ame.main` as a completely detached, windowless background daemon."""
    if is_server_healthy(host, port):
        return True
    env = dict(os.environ)
    env.setdefault("AME_MCP_ENABLED", "true")
    env.setdefault("AME_MCP_READ_ONLY", "false")
    env.setdefault("AME_AUTO_APPROVE_AGENT_WRITES", "true")
    env.setdefault("AME_HOST", host)
    env.setdefault("AME_PORT", str(port))

    exe = sys.executable
    if os.name == "nt":
        # Prefer pythonw.exe on Windows so no console window is ever allocated
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pyw):
            exe = pyw
        # Use WMI Win32_Process.Create first so the daemon is parented by WmiPrvSE.exe
        # completely outside any AI Agent's Job Object / process tree (preventing both
        # lingering "background task running" banners and job-close termination).
        ps_cmd = (
            "Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
            f"-Arguments @{{CommandLine='\"{exe}\" -m ame.main'}} | Out-Null"
        )
        res = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-Command",
                ps_cmd,
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=0x08000000,
        )
        if res.returncode != 0 or not _wait_healthy(host, port, wait_s):
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

    return _wait_healthy(host, port, wait_s)


def _wait_healthy(host: str, port: int, wait_s: float) -> bool:
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if is_server_healthy(host, port, timeout=0.5):
            return True
        time.sleep(0.15)
    return is_server_healthy(host, port, timeout=0.5)


def run(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ame-server", description="AME local REST server")
    parser.add_argument(
        "--daemon",
        "-d",
        action="store_true",
        help="run silently in the background and exit immediately",
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="restart background server and exit immediately",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="stop running background server and exit",
    )
    args = parser.parse_args(list(argv) if argv is not None else sys.argv[1:])
    settings = Settings()

    if args.stop:
        stop_running_server(settings.port)
        return
    if args.restart:
        stop_running_server(settings.port)
        time.sleep(0.3)
        ok = spawn_detached_server(settings.host, settings.port)
        if not ok:
            raise SystemExit(1)
        return
    if args.daemon:
        ok = spawn_detached_server(settings.host, settings.port)
        if not ok:
            raise SystemExit(1)
        return

    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    run()
