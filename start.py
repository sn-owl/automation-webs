"""Start the dashboard and optional inbox runner with readiness evidence."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_REQUIRED_FILES = ("dashboard.py", "inbox_runner.py", "run_pipeline.py")


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _status(name: str, state: str, detail: str, next_action: str) -> dict[str, str]:
    return {
        "name": name,
        "state": state,
        "detail": detail,
        "next_action": next_action,
        "checked_at": _timestamp(),
    }


def _safe_mkdir(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".startup-write-check"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return False, f"{type(exc).__name__}"
    return True, str(path)


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _streamlit_health(port: int) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/_stcore/health", timeout=1
        ) as response:
            return response.status == 200 and response.read().strip() == b"ok"
    except (OSError, urllib.error.URLError):
        return False


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _read_pid(path: Path) -> int | None:
    try:
        value = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    os.replace(temporary, path)


def _check_environment(
    repo_root: Path,
    runtime_root: Path,
    downloads_root: Path,
    inbox_root: Path,
    *,
    with_runner: bool,
) -> list[dict[str, str]]:
    statuses = [
        _status("python", "normal", sys.executable, "없음"),
    ]
    streamlit = importlib.util.find_spec("streamlit")
    if streamlit is None:
        statuses.append(
            _status("streamlit", "blocked", "모듈이 설치되지 않았습니다.", "현재 Python 환경에 streamlit을 설치하세요.")
        )
    else:
        statuses.append(
            _status("streamlit", "normal", "설치된 Streamlit 모듈을 확인했습니다.", "없음")
        )

    missing = [name for name in _REQUIRED_FILES if not (repo_root / name).is_file()]
    if missing:
        statuses.append(
            _status("repository", "blocked", f"필수 파일 없음: {', '.join(missing)}", "저장소 경로를 확인하세요.")
        )
    else:
        statuses.append(_status("repository", "normal", str(repo_root), "없음"))

    writable, detail = _safe_mkdir(runtime_root / "state")
    if writable:
        _safe_mkdir(inbox_root)
        statuses.append(_status("runtime", "normal", str(runtime_root), "없음"))
    else:
        statuses.append(_status("runtime", "blocked", detail, "Runtime 경로 쓰기 권한을 확인하세요."))

    if with_runner:
        if downloads_root.is_dir():
            statuses.append(_status("collection", "normal", str(downloads_root), "없음"))
        else:
            statuses.append(
                _status(
                    "collection",
                    "blocked",
                    f"Downloads Bundle 경로가 없습니다: {downloads_root}",
                    "Chrome Extension의 upmuzadong-inbox 경로를 만들거나 --downloads-root를 지정하세요.",
                )
            )
    else:
        statuses.append(
            _status("collection", "not_applicable", "runner를 시작하지 않았습니다.", "--with-runner로 수집 경로를 함께 시작하세요.")
        )

    if importlib.util.find_spec("win32com") is None:
        statuses.append(
            _status("restarea-executor", "blocked", "pywin32(win32com)가 없습니다.", "restarea-converter의 의존성을 설치하세요.")
        )
    else:
        statuses.append(
            _status(
                "restarea-executor",
                "unverifiable",
                "win32com 모듈은 있으나 Excel 설치·COM 응답은 시작 점검에서 확인하지 않았습니다.",
                "실행 전 Excel 설치와 COM 응답을 확인하세요.",
            )
        )
    return statuses


def _creation_flags() -> int:
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    if os.name == "nt":
        # Keep the child independent of the invoking PowerShell/batch process
        # without using DETACHED_PROCESS, which can be reclaimed with the
        # parent console/job when start.py exits.
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return flags


def _launch_dashboard(repo_root: Path, runtime_root: Path, port: int) -> tuple[str, int | None]:
    if _streamlit_health(port):
        return "reused", None
    if _port_open(port):
        raise RuntimeError(f"port {port} is already occupied by a non-Streamlit process")
    environment = os.environ.copy()
    environment["UPMU_DASHBOARD_RUNTIME"] = str(runtime_root)
    environment["UPMU_DASHBOARD_DOWNLOADS"] = str(runtime_root / "downloads")
    environment["UPMU_DASHBOARD_INBOX"] = str(runtime_root / "inbox")
    log_path = runtime_root / "state" / "dashboard.log"
    log = log_path.open("a", encoding="utf-8")
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(repo_root / "dashboard.py"),
        "--server.headless",
        "true",
        "--server.port",
        str(port),
    ]
    process = subprocess.Popen(
        command,
        cwd=repo_root,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        env=environment,
        creationflags=_creation_flags(),
    )
    for _ in range(40):
        if _streamlit_health(port):
            return "started", process.pid
        if process.poll() is not None:
            break
        time.sleep(0.25)
    raise RuntimeError(f"dashboard did not become ready on port {port}")


def _launch_runner(
    repo_root: Path,
    runtime_root: Path,
    downloads_root: Path,
    inbox_root: Path,
    interval: float,
    scope: str | None = None,
    connector_id: str | None = None,
) -> tuple[str, int | None]:
    pid_path = runtime_root / "state" / "inbox_runner.pid"
    existing = _read_pid(pid_path)
    if existing is not None and _pid_alive(existing):
        return "reused", existing
    if pid_path.exists():
        pid_path.unlink()
    log = (runtime_root / "state" / "inbox_runner.log").open("a", encoding="utf-8")
    runner_python = Path(sys.executable).with_name("pythonw.exe")
    if not runner_python.is_file():
        runner_python = Path(sys.executable)
    command = [
        str(runner_python),
        str(repo_root / "inbox_runner.py"),
        "--downloads",
        str(downloads_root),
        "--inbox",
        str(inbox_root),
        "--output-root",
        str(runtime_root),
        "--repository-root",
        str(repo_root),
        "--interval",
        str(interval),
    ]
    if scope is not None and connector_id is not None:
        command += ["--scope", scope, "--connector-id", connector_id]
    process = subprocess.Popen(
        command,
        cwd=repo_root,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        env=os.environ.copy(),
        creationflags=_creation_flags(),
    )
    time.sleep(0.25)
    if process.poll() is not None:
        raise RuntimeError(f"inbox runner exited with code {process.returncode}")
    pid_path.write_text(str(process.pid), encoding="utf-8")
    return "started", process.pid


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--runtime-root", type=Path, default=None)
    parser.add_argument("--downloads-root", type=Path, default=Path.home() / "Downloads" / "upmuzadong-inbox")
    parser.add_argument("--inbox-root", type=Path, default=None)
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--with-runner", action="store_true")
    parser.add_argument(
        "--scope",
        default=None,
        help="owner scope this runtime collects into; requires --connector-id. "
        "Without both, collection keeps the legacy unscoped task identity.",
    )
    parser.add_argument("--connector-id", default=None, dest="connector_id")
    parser.add_argument("--runner-interval", type=float, default=60.0)
    args = parser.parse_args(argv)
    if args.port < 1 or args.port > 65535:
        parser.error("--port must be between 1 and 65535")
    if args.runner_interval <= 0:
        parser.error("--runner-interval must be positive")
    if (args.scope is None) != (args.connector_id is None):
        parser.error("--scope and --connector-id must be given together")

    repo_root = args.repository_root.expanduser().resolve()
    runtime_root = (args.runtime_root or repo_root / "runtime").expanduser().resolve()
    inbox_root = (args.inbox_root or runtime_root / "inbox").expanduser().resolve()
    downloads_root = args.downloads_root.expanduser().resolve()
    statuses = _check_environment(
        repo_root,
        runtime_root,
        downloads_root,
        inbox_root,
        with_runner=args.with_runner,
    )
    blocked = any(item["state"] == "blocked" for item in statuses)
    result: dict[str, Any] = {
        "checked_at": _timestamp(),
        "repository_root": str(repo_root),
        "runtime_root": str(runtime_root),
        "dashboard": {"state": "not_started", "port": args.port},
        "runner": {"state": "not_started"},
        "statuses": statuses,
    }
    if not blocked:
        try:
            dashboard_state, dashboard_pid = _launch_dashboard(repo_root, runtime_root, args.port)
            result["dashboard"] = {"state": dashboard_state, "port": args.port, "pid": dashboard_pid}
            if args.with_runner:
                runner_state, runner_pid = _launch_runner(
                    repo_root,
                    runtime_root,
                    downloads_root,
                    inbox_root,
                    args.runner_interval,
                    args.scope,
                    args.connector_id,
                )
                result["runner"] = {"state": runner_state, "pid": runner_pid, "interval": args.runner_interval}
            else:
                result["runner"] = {"state": "not_applicable"}
        except (OSError, RuntimeError) as exc:
            result["dashboard"] = {"state": "blocked", "port": args.port}
            result["error"] = f"{type(exc).__name__}: {exc}"
            blocked = True
    _write_json(runtime_root / "state" / "startup.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 2 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
