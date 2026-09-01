"""The standalone preview hands off credentials without logging them."""

import sys
from types import SimpleNamespace

import Cortex_Preview


def test_preview_browser_url_keeps_bootstrap_credential_in_fragment():
    assert Cortex_Preview._preview_browser_url(8765, "one time/token") == (
        "http://127.0.0.1:8765/#bootstrap=one%20time%2Ftoken"
    )


def test_open_preview_browser_registers_startup_handoff_without_logging(
    monkeypatch,
):
    app = SimpleNamespace(
        state=SimpleNamespace(session_manager=SimpleNamespace(bootstrap_token="secret")),
        router=SimpleNamespace(on_startup=[]),
    )
    opened = []
    monkeypatch.setattr(
        Cortex_Preview.webbrowser,
        "open",
        lambda url, new=0: opened.append((url, new)) or True,
    )

    Cortex_Preview._open_preview_browser(app, port=8765)

    assert len(app.router.on_startup) == 1
    app.router.on_startup[0]()
    assert opened == [("http://127.0.0.1:8765/#bootstrap=secret", 2)]


def test_open_preview_browser_does_not_surface_browser_errors(monkeypatch):
    app = SimpleNamespace(
        state=SimpleNamespace(session_manager=SimpleNamespace(bootstrap_token="secret")),
        router=SimpleNamespace(on_startup=[]),
    )
    monkeypatch.setattr(
        Cortex_Preview.webbrowser,
        "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("browser unavailable")),
    )

    Cortex_Preview._open_preview_browser(app, port=8765)
    app.router.on_startup[0]()


def test_preview_main_never_prints_the_bootstrap_token(monkeypatch, capsys):
    app = SimpleNamespace(
        state=SimpleNamespace(session_manager=SimpleNamespace(bootstrap_token="secret")),
    )
    monkeypatch.setattr(Cortex_Preview, "build_preview_app", lambda: app)
    monkeypatch.setattr(Cortex_Preview.uvicorn, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sys, "argv", ["Cortex_Preview.py", "--no-browser"])

    Cortex_Preview.main()

    output = capsys.readouterr().out
    assert "secret" not in output
    assert "bootstrap token" not in output.lower()
