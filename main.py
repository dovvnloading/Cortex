"""Cortex's single Windows-first native web application entry point."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import socket
import signal
import sys
import tempfile
import time
import re
import secrets


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))

import uvicorn  # noqa: E402

from Cortex_Preview import build_preview_app  # noqa: E402
from cortex_backend.core.paths import AppPathError, AppPaths  # noqa: E402
from cortex_backend.launcher import (  # noqa: E402
    DesktopWindowConfig,
    DesktopWindowError,
    FrontendBuildError,
    InstanceLock,
    WebViewRuntimeError,
    activate_process_window,
    ensure_frontend,
    ensure_webview2_runtime,
    run_desktop_window,
)
from cortex_backend.launcher.supervisor import (  # noqa: E402
    ChildProcessSupervisor,
    DEV_SERVER_ID_HEADER,
    ServerSupervisor,
    wait_for_http,
)


# Normal launches must coexist with other loopback development servers.
# Port 0 means "ask the OS for an available port"; an explicitly supplied
# --port value remains strict and will still fail if that port is occupied.
DEFAULT_PORT = 0
FRONTEND_PORT = 5173
CORTEX_VERSION = "0.1.0"
STARTUP_LOG_NAME = "startup.log"
MAX_STARTUP_LOG_BYTES = 64 * 1024
_last_startup_log_path: Path | None = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Cortex locally.")
    parser.add_argument(
        "--dev",
        action="store_true",
        help="run the backend and a supervised Vite development server",
    )
    parser.add_argument(
        "--headless",
        "--no-browser",
        dest="headless",
        action="store_true",
        help="start only the loopback backend (the --no-browser name is deprecated)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="loopback backend port (default: automatically choose a free port)",
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
    return parser


def _validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.port != 0 and not 1024 <= args.port <= 65535:
        parser.error("--port must be 0 or between 1024 and 65535")
    if args.dev and args.build_frontend:
        parser.error("--dev and --build-frontend cannot be combined")


def _resolve_paths(data_dir: Path | None) -> AppPaths:
    paths = AppPaths.from_data_dir(data_dir) if data_dir else AppPaths.for_current_user()
    paths.ensure_data_dir()
    return paths


def _is_packaged() -> bool:
    return bool(getattr(sys, "frozen", False) or getattr(sys, "_MEIPASS", None))


def _frontend_root() -> Path:
    if _is_packaged():
        return _resource_root() / "frontend"
    return ROOT / "frontend"


def _resource_root() -> Path:
    if _is_packaged():
        return Path(sys._MEIPASS)
    return ROOT / "packaging" / ".runtime"


def _app_asset_root() -> Path:
    """Resolve assets from the source tree or PyInstaller's bundled root."""
    return Path(sys._MEIPASS) if _is_packaged() else ROOT


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _requested_port(value: int) -> int:
    return _free_port() if value == 0 else value


def _desktop_url(port: int, token: str, handoff_secret: str | None = None) -> str:
    from urllib.parse import quote

    fragment = f"bootstrap={quote(token, safe='')}"
    if handoff_secret:
        fragment += f"&handoff={quote(handoff_secret, safe='')}"
    return f"http://127.0.0.1:{port}/#{fragment}"


def _startup_log_path(data_dir: Path | None) -> Path:
    """Choose a user-writable diagnostic path without touching chat data."""

    if data_dir is not None:
        try:
            return AppPaths.from_data_dir(data_dir).data_dir / STARTUP_LOG_NAME
        except AppPathError:
            # A rejected custom root must not become a reason to write a log
            # through an untrusted junction; use the isolated temp fallback.
            return Path(tempfile.gettempdir()) / "Cortex" / STARTUP_LOG_NAME
    try:
        return AppPaths.for_current_user().data_dir / STARTUP_LOG_NAME
    except AppPathError:
        return Path(tempfile.gettempdir()) / "Cortex" / STARTUP_LOG_NAME


def _redact_startup_detail(value: object) -> str:
    """Keep startup diagnostics useful without recording credential-like text."""

    detail = str(value).replace("\r", " ").replace("\n", " ")
    detail = re.sub(
        r"(?i)\b(?:bootstrap(?:_token)?|token|secret|authorization|password|prompt)\b\s*[=:]\s*[^\s,;]+",
        lambda match: f"{match.group(0).split('=')[0].split(':')[0]}=<redacted>",
        detail,
    )
    return detail[:800]


def _write_startup_diagnostic(
    *,
    stage: str,
    error: BaseException,
    data_dir: Path | None,
) -> Path | None:
    """Append one bounded, privacy-safe startup record and return its path."""

    global _last_startup_log_path
    path = _startup_log_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = (
            f"{datetime.now(timezone.utc).isoformat()} "
            f"stage={_redact_startup_detail(stage)} "
            f"error_type={type(error).__name__} "
            f"detail={_redact_startup_detail(error)}\n"
        )
        try:
            current_size = path.stat().st_size if path.exists() else 0
        except OSError:
            current_size = 0
        mode = "w" if current_size + len(entry.encode("utf-8")) > MAX_STARTUP_LOG_BYTES else "a"
        with path.open(mode, encoding="utf-8") as handle:
            handle.write(entry)
        _last_startup_log_path = path
        return path
    except (OSError, UnicodeError):
        _last_startup_log_path = None
        return None


def _startup_dialog_message(log_path: Path | None) -> str:
    if log_path is None:
        return "Cortex could not start.\n\nCortex could not write its diagnostic log."
    return (
        "Cortex could not start.\n\n"
        "A privacy-safe diagnostic log was written to:\n"
        f"{log_path}\n\n"
        "Press Ctrl+C in this dialog to copy this message."
    )


def _server_for_app(app, *, port: int, log_level: str) -> uvicorn.Server:
    # A PyInstaller windowed executable intentionally has no console streams.
    # Uvicorn's stock formatter probes ``sys.stderr.isatty()`` while it builds
    # its logging configuration, which otherwise prevents the desktop app from
    # starting before the native window can be created.  Keep normal console
    # logging for source/headless runs and omit only the console-oriented
    # configuration when that stream is unavailable.
    log_config = None if not callable(getattr(sys.stderr, "isatty", None)) else uvicorn.config.LOGGING_CONFIG
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level=log_level,
        access_log=False,
        log_config=log_config,
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


def _monitor_native_window(
    window,
    *,
    backend,
    frontend,
    server,
    readiness_url: str,
) -> None:
    """Close the shell only after sustained backend-liveness failure."""
    failed_probes = 0
    while not window.events.closed.is_set():
        ready = wait_for_http(
            readiness_url,
            timeout=0.25,
            is_alive=lambda: not window.events.closed.is_set(),
        )
        if ready:
            failed_probes = 0
        else:
            failed_probes += 1
        if backend.error is not None:
            try:
                window.destroy()
            except Exception:
                pass
            raise RuntimeError("Cortex backend stopped unexpectedly.") from backend.error
        if failed_probes >= 8:
            try:
                window.destroy()
            except Exception:
                pass
            if server.should_exit:
                return
            raise RuntimeError(
                "Cortex backend became unavailable after 8 consecutive liveness probes."
            )
        if frontend is not None and not frontend.running:
            try:
                window.destroy()
            except Exception:
                pass
            raise RuntimeError(
                f"Vite stopped unexpectedly with exit code {frontend.returncode}."
            )
        time.sleep(1.5)


def _run_headless(*, backend, frontend, server) -> int:
    print("Cortex's loopback backend is ready in headless mode.")
    while backend.running:
        if backend.error is not None:
            raise RuntimeError("Cortex backend stopped unexpectedly.") from backend.error
        if frontend is not None and not frontend.running:
            raise RuntimeError(
                f"Vite stopped unexpectedly with exit code {frontend.returncode}."
            )
        time.sleep(0.1)
    return 0 if server.should_exit else 1


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
            _write_startup_diagnostic(
                stage="frontend build",
                error=exc,
                data_dir=args.data_dir,
            )
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
            if args.headless:
                print(f"Cortex is already running on loopback port {existing.port}.")
                return 0
            if not activate_process_window(existing.pid):
                print(
                    "Cortex is already running, but its native window could not be activated.",
                    file=sys.stderr,
                )
                return 2
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
                is_alive=lambda: backend.accepting_startup,
            ):
                if backend.error is not None:
                    raise RuntimeError("Cortex backend failed during startup.") from backend.error
                raise RuntimeError("Cortex backend did not become ready within 30 seconds.")

            browser_port = backend_port
            if args.dev:
                frontend_port = _free_port()
                dev_server_nonce = secrets.token_urlsafe(32)
                environment = os.environ.copy()
                environment["CORTEX_BACKEND_PORT"] = str(backend_port)
                environment["CORTEX_FRONTEND_PORT"] = str(frontend_port)
                environment["CORTEX_DEV_SERVER_NONCE"] = dev_server_nonce
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
                    expected_headers={DEV_SERVER_ID_HEADER: dev_server_nonce},
                ):
                    raise RuntimeError("Vite did not become ready within 30 seconds.")
                browser_port = frontend_port

            if args.headless:
                return _run_headless(backend=backend, frontend=frontend, server=server)

            ensure_webview2_runtime(_resource_root())
            token = app.state.session_manager.bootstrap_token
            print("Cortex is ready in its native desktop window.")
            run_desktop_window(
                DesktopWindowConfig(
                    url=_desktop_url(browser_port, token, handoff_secret),
                    storage_path=paths.webview_profile,
                    icon_path=_app_asset_root() / "assets" / "cortex.ico",
                    debug=args.dev,
                ),
                monitor=lambda window: _monitor_native_window(
                    window,
                    backend=backend,
                    frontend=frontend,
                    server=server,
                    readiness_url=(
                        f"http://127.0.0.1:{backend_port}/api/v1/health/live"
                    ),
                ),
            )
            server.should_exit = True
            return 0
        except KeyboardInterrupt:
            print("Stopping Cortex…")
            return 0
        except (
            DesktopWindowError,
            OSError,
            RuntimeError,
            TimeoutError,
            WebViewRuntimeError,
        ) as exc:
            _write_startup_diagnostic(
                stage="desktop startup/runtime",
                error=exc,
                data_dir=args.data_dir,
            )
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
    try:
        result = _run_web(args)
    except AppPathError as exc:
        _write_startup_diagnostic(
            stage="data path resolution",
            error=exc,
            data_dir=args.data_dir,
        )
        print(f"Cortex data-path error: {exc}", file=sys.stderr)
        result = 2
    except Exception as exc:
        _write_startup_diagnostic(
            stage="uncaught startup",
            error=exc,
            data_dir=args.data_dir,
        )
        print(f"Cortex startup error: {exc}", file=sys.stderr)
        result = 1
    if result and _is_packaged() and os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None,
                _startup_dialog_message(_last_startup_log_path),
                "Cortex startup error",
                0x10,
            )
        except Exception:
            pass
    return result


if __name__ == "__main__":
    # The normal local image and compute profiles use short-lived, restricted
    # worker processes. PyInstaller requires this hand-off before it enters
    # the desktop application's main function.
    import multiprocessing

    multiprocessing.freeze_support()
    raise SystemExit(main())
