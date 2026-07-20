"""Cortex's single Windows-first application entry point.

The web application is the default runtime.  ``--legacy-qt`` remains an
explicit compatibility escape hatch while the migration is completed.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import signal
import sys
import time
import urllib.error
from urllib.parse import quote
from urllib.request import Request, urlopen
import webbrowser


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "Chat_LLM" / "Chat_LLM"))

import uvicorn  # noqa: E402

from Cortex_Preview import build_preview_app  # noqa: E402
from cortex_backend.core.paths import AppPathError, AppPaths  # noqa: E402
from cortex_backend.launcher import (  # noqa: E402
    FrontendBuildError,
    InstanceLock,
    ensure_frontend,
)
from cortex_backend.launcher.supervisor import (  # noqa: E402
    ChildProcessSupervisor,
    ServerSupervisor,
    wait_for_http,
)


DEFAULT_PORT = 8765
FRONTEND_PORT = 5173
CORTEX_VERSION = "0.1.0"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Cortex locally.")
    parser.add_argument(
        "--dev",
        action="store_true",
        help="run the backend and a supervised Vite development server",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="start the runtime without opening a browser",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="loopback backend port, or 0 to choose a free port",
    )
    parser.add_argument(
        "--build-frontend",
        action="store_true",
        help="force a source frontend build and exit",
    )
    parser.add_argument(
        "--skip-build-check",
        action="store_true",
        help="use the existing frontend bundle without rebuilding it",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=("critical", "error", "warning", "info", "debug", "trace"),
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="explicit local data directory (recommended for isolated runs)",
    )
    parser.add_argument(
        "--legacy-qt",
        action="store_true",
        help="run the temporary legacy PySide6 application",
    )
    return parser


def _validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.port != 0 and not 1024 <= args.port <= 65535:
        parser.error("--port must be 0 or between 1024 and 65535")
    if args.dev and args.build_frontend:
        parser.error("--dev and --build-frontend cannot be combined")
    if args.legacy_qt and (args.dev or args.build_frontend or args.skip_build_check):
        parser.error("--legacy-qt cannot be combined with web launcher options")


def _resolve_paths(data_dir: Path | None) -> AppPaths:
    paths = AppPaths.from_data_dir(data_dir) if data_dir else AppPaths.for_current_user()
    paths.ensure_data_dir()
    return paths


def _is_packaged() -> bool:
    return bool(getattr(sys, "frozen", False) or getattr(sys, "_MEIPASS", None))


def _frontend_root() -> Path:
    if _is_packaged():
        return Path(getattr(sys, "_MEIPASS")) / "frontend"
    return ROOT / "frontend"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _requested_port(value: int) -> int:
    return _free_port() if value == 0 else value


def _handoff(record_port: int, secret: str) -> str:
    request = Request(
        f"http://127.0.0.1:{record_port}/api/v1/session/handoff",
        method="POST",
        headers={"Host": "127.0.0.1", "X-Cortex-Handoff": secret},
    )
    try:
        with urlopen(request, timeout=3.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "A Cortex instance is recorded, but its authenticated handoff is unavailable."
        ) from exc
    token = payload.get("bootstrap_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("The running Cortex instance returned an invalid handoff.")
    return token


def _browser_url(port: int, token: str) -> str:
    return f"http://127.0.0.1:{port}/#bootstrap={quote(token, safe='')}"


def _announce_and_open(port: int, token: str, *, no_browser: bool) -> None:
    url = _browser_url(port, token)
    if no_browser:
        print(f"Cortex is ready. Open this one-time local URL: {url}")
    else:
        print(f"Cortex is ready at http://127.0.0.1:{port}")
        webbrowser.open(url, new=1, autoraise=True)


def _run_legacy_qt() -> int:
    legacy_entry = ROOT / "Chat_LLM" / "Chat_LLM" / "Chat_LLM.py"
    if not legacy_entry.is_file():
        print(f"Legacy Qt entry point is missing: {legacy_entry}", file=sys.stderr)
        return 2
    return subprocess.run([sys.executable, str(legacy_entry)], check=False).returncode


def _server_for_app(app, *, port: int, log_level: str) -> uvicorn.Server:
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level=log_level,
        access_log=False,
    )
    server = uvicorn.Server(config)
    app.state.shutdown_callback = lambda: setattr(server, "should_exit", True)
    return server


def _install_shutdown_signals(server: uvicorn.Server) -> None:
    """Translate console interrupts into the same owned graceful shutdown."""
    def request_shutdown(_signum: int, _frame: object) -> None:
        server.should_exit = True

    signal.signal(signal.SIGINT, request_shutdown)
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        signal.signal(sigbreak, request_shutdown)


def _run_web(args: argparse.Namespace) -> int:
    packaged = _is_packaged()
    frontend_root = _frontend_root()

    if args.build_frontend:
        try:
            dist = ensure_frontend(
                frontend_root,
                force=True,
                packaged=packaged,
                cortex_version=CORTEX_VERSION,
            )
        except FrontendBuildError as exc:
            print(f"Frontend build failed: {exc}", file=sys.stderr)
            return 2
        print(f"Frontend bundle ready at {dist}")
        return 0

    paths = _resolve_paths(args.data_dir)
    backend_port = _requested_port(args.port)

    with InstanceLock(paths.data_dir) as instance:
        record = instance.acquire(port=backend_port)
        if record is None:
            existing = instance.read_record()
            if existing is None:
                print(
                    "Cortex could not acquire its instance lock and no valid running-instance record exists.",
                    file=sys.stderr,
                )
                return 2
            secret = instance.read_secret(existing)
            if not secret:
                print("The running Cortex instance has no valid handoff secret.", file=sys.stderr)
                return 2
            try:
                token = _handoff(existing.port, secret)
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            _announce_and_open(existing.port, token, no_browser=args.no_browser)
            return 0

        try:
            if args.dev:
                dist = None
            else:
                dist = ensure_frontend(
                    frontend_root,
                    skip_check=args.skip_build_check,
                    packaged=packaged,
                    cortex_version=CORTEX_VERSION,
                )
        except FrontendBuildError as exc:
            print(f"Frontend preparation failed: {exc}", file=sys.stderr)
            return 2

        handoff_secret = instance.read_secret(record)
        if not handoff_secret:
            print("Cortex could not initialize its authenticated handoff secret.", file=sys.stderr)
            return 2

        app = build_preview_app(
            data_dir=paths.data_dir,
            frontend_dist=dist,
            serve_frontend=not args.dev,
            qt_default=False,
            handoff_secret=handoff_secret,
        )
        server = _server_for_app(app, port=backend_port, log_level=args.log_level)
        _install_shutdown_signals(server)
        backend = ServerSupervisor(server)
        frontend: ChildProcessSupervisor | None = None
        frontend_port = FRONTEND_PORT
        try:
            backend.start()
            if not wait_for_http(
                f"http://127.0.0.1:{backend_port}/api/v1/health/ready",
                timeout=30,
                is_alive=lambda: backend.running,
            ):
                if backend.error is not None:
                    raise RuntimeError("Cortex backend failed during startup.") from backend.error
                raise RuntimeError("Cortex backend did not become ready within 30 seconds.")

            browser_port = backend_port
            if args.dev:
                frontend_port = _free_port()
                environment = os.environ.copy()
                environment["CORTEX_BACKEND_PORT"] = str(backend_port)
                environment["CORTEX_FRONTEND_PORT"] = str(frontend_port)
                npm = "npm.cmd" if os.name == "nt" else "npm"
                frontend = ChildProcessSupervisor(
                    [npm, "run", "dev", "--", "--host", "127.0.0.1", "--strictPort"],
                    cwd=frontend_root,
                    env=environment,
                )
                frontend.start()
                if not wait_for_http(
                    f"http://127.0.0.1:{frontend_port}",
                    timeout=30,
                    is_alive=lambda: frontend.running,
                ):
                    raise RuntimeError("Vite did not become ready within 30 seconds.")
                browser_port = frontend_port

            token = app.state.session_manager.bootstrap_token
            _announce_and_open(browser_port, token, no_browser=args.no_browser)
            while backend.running:
                if backend.error is not None:
                    raise RuntimeError("Cortex backend stopped unexpectedly.") from backend.error
                if frontend is not None and not frontend.running:
                    raise RuntimeError(
                        f"Vite stopped unexpectedly with exit code {frontend.returncode}."
                    )
                time.sleep(0.1)
            return 0 if server.should_exit else 1
        except KeyboardInterrupt:
            print("Stopping Cortex…")
            return 0
        except (OSError, RuntimeError, TimeoutError) as exc:
            print(f"Cortex startup/runtime error: {exc}", file=sys.stderr)
            return 1
        finally:
            if frontend is not None:
                try:
                    frontend.stop()
                except TimeoutError as exc:
                    print(str(exc), file=sys.stderr)
            if backend.running:
                try:
                    backend.stop()
                except (RuntimeError, TimeoutError) as exc:
                    print(str(exc), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_args(args, parser)
    if args.legacy_qt:
        return _run_legacy_qt()
    try:
        return _run_web(args)
    except AppPathError as exc:
        print(f"Cortex data-path error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
