"""Where Cortex keeps its data and its caches, and what it will never touch.

The data directory (chats, settings, memories) stays where every earlier
release put it. The large, reproducible folders -- the llama.cpp runtime,
downloaded GGUF models, the WebView profile -- are new-install data that goes
to ``%LOCALAPPDATA%``; an install that already has them keeps using them in
place. Nothing here may move, copy over or delete an existing folder.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import app_factory
import main as launcher_main
from cortex_backend.core import paths as paths_module
from cortex_backend.core.paths import AppPathError, AppPaths

IDENTITY = Path("ChatLLM") / "ChatLLM-Assistant"
CACHE_FOLDERS = ("webview", "llamacpp_runtime", "gguf_models")


def _environ(tmp_path: Path, *, local: bool = True) -> dict[str, str]:
    environ = {"APPDATA": str(tmp_path / "Roaming")}
    if local:
        environ["LOCALAPPDATA"] = str(tmp_path / "Local")
    return environ


def _cache_paths(paths: AppPaths) -> dict[str, Path]:
    return {
        "webview": paths.webview_profile,
        "llamacpp_runtime": paths.llamacpp_runtime_dir,
        "gguf_models": paths.default_gguf_models_dir,
    }


def test_a_normal_profile_puts_new_caches_under_local_appdata(tmp_path: Path) -> None:
    paths = AppPaths.for_windows(_environ(tmp_path))

    data_dir = (tmp_path / "Roaming" / IDENTITY).resolve()
    cache_dir = (tmp_path / "Local" / IDENTITY).resolve()
    assert paths.data_dir == data_dir
    assert paths.cache_dir == cache_dir
    assert _cache_paths(paths) == {name: cache_dir / name for name in CACHE_FOLDERS}
    # The small, precious stores stay exactly where they were.
    assert paths.database == data_dir / "cortex_db.sqlite"
    assert paths.settings_database == data_dir / "cortex_settings.sqlite"
    assert paths.permanent_memory == data_dir / "memory_bank.json"
    assert paths.execution_database == data_dir / "execution.sqlite"
    assert not paths.local_fallback


def test_without_localappdata_every_cache_stays_with_the_data(tmp_path: Path) -> None:
    paths = AppPaths.for_windows(_environ(tmp_path, local=False))

    data_dir = (tmp_path / "Roaming" / IDENTITY).resolve()
    assert paths.cache_dir == data_dir
    assert _cache_paths(paths) == {name: data_dir / name for name in CACHE_FOLDERS}


def test_an_explicit_data_directory_keeps_everything_together(tmp_path: Path) -> None:
    paths = AppPaths.from_data_dir(tmp_path / "isolated")

    root = (tmp_path / "isolated").resolve()
    assert paths.cache_dir == root
    assert _cache_paths(paths) == {name: root / name for name in CACHE_FOLDERS}


def test_existing_cache_folders_are_used_in_place_and_never_touched(tmp_path: Path) -> None:
    paths = AppPaths.for_windows(_environ(tmp_path))
    sentinels: dict[str, Path] = {}
    for name in CACHE_FOLDERS:
        folder = paths.data_dir / name
        folder.mkdir(parents=True)
        sentinel = folder / "keep.bin"
        sentinel.write_bytes(b"existing user data")
        sentinels[name] = sentinel

    assert _cache_paths(paths) == {name: paths.data_dir / name for name in CACHE_FOLDERS}
    # Resolving where things are must not create, move or copy anything.
    assert not paths.cache_dir.exists()
    for sentinel in sentinels.values():
        assert sentinel.read_bytes() == b"existing user data"


def test_each_cache_folder_is_decided_on_its_own(tmp_path: Path) -> None:
    paths = AppPaths.for_windows(_environ(tmp_path))
    (paths.data_dir / "gguf_models").mkdir(parents=True)

    assert paths.default_gguf_models_dir == paths.data_dir / "gguf_models"
    assert paths.llamacpp_runtime_dir == paths.cache_dir / "llamacpp_runtime"
    assert paths.webview_profile == paths.cache_dir / "webview"


def test_a_cache_folder_already_under_local_appdata_wins(tmp_path: Path) -> None:
    """The answer must not flip once a new install has started filling the cache."""
    paths = AppPaths.for_windows(_environ(tmp_path))
    (paths.data_dir / "gguf_models").mkdir(parents=True)
    (paths.cache_dir / "gguf_models").mkdir(parents=True)

    assert paths.default_gguf_models_dir == paths.cache_dir / "gguf_models"


def test_ensure_cache_dir_creates_only_the_root_and_secures_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secured: list[tuple[Path, bool]] = []
    monkeypatch.setattr(
        paths_module,
        "secure_private_path",
        lambda path, *, directory: secured.append((Path(path), directory)) or Path(path),
    )
    paths = AppPaths.for_windows(_environ(tmp_path))

    assert paths.ensure_cache_dir() == paths.cache_dir
    assert paths.cache_dir.is_dir()
    assert secured == [(paths.cache_dir, True)]
    assert [child.name for child in paths.cache_dir.iterdir()] == []


def test_ensure_cache_dir_does_nothing_when_caches_share_the_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        paths_module,
        "secure_private_path",
        lambda *_args, **_kwargs: pytest.fail("no separate cache root to secure"),
    )
    paths = AppPaths.from_data_dir(tmp_path / "isolated")

    assert paths.ensure_cache_dir() == paths.data_dir
    assert not paths.data_dir.exists()


def test_a_failed_acl_on_the_cache_root_is_reported_not_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> Path:
        raise AppPathError("Cortex could not secure its private data permissions.")

    monkeypatch.setattr(paths_module, "secure_private_path", refuse)
    paths = AppPaths.for_windows(_environ(tmp_path))

    with pytest.raises(AppPathError, match="secure"):
        paths.ensure_cache_dir()


def test_a_redirected_appdata_falls_back_to_local_appdata_for_the_whole_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paths_module.sys, "platform", "win32")
    environ = {
        "APPDATA": r"\\fileserver\profiles\someone\AppData\Roaming",
        "LOCALAPPDATA": str(tmp_path / "Local"),
    }

    paths = AppPaths.for_windows(environ)

    root = (tmp_path / "Local" / IDENTITY).resolve()
    assert paths.data_dir == root
    assert paths.database == root / "cortex_db.sqlite"
    assert _cache_paths(paths) == {name: root / name for name in CACHE_FOLDERS}
    assert paths.local_fallback is True


def test_a_redirected_appdata_with_no_usable_local_folder_still_fails_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paths_module.sys, "platform", "win32")
    redirected = r"\\fileserver\profiles\someone\AppData\Roaming"

    with pytest.raises(AppPathError, match="network path"):
        AppPaths.for_windows({"APPDATA": redirected})
    with pytest.raises(AppPathError, match="network path"):
        AppPaths.for_windows(
            {"APPDATA": redirected, "LOCALAPPDATA": r"\\fileserver\local\AppData\Local"}
        )


def test_an_explicit_unc_data_directory_is_still_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fallback is for the default location only; --data-dir keeps its guard."""
    monkeypatch.setattr(paths_module.sys, "platform", "win32")

    with pytest.raises(AppPathError, match="UNC"):
        AppPaths.from_data_dir(r"\\fileserver\share\cortex")


def test_a_local_appdata_behind_a_link_is_not_used_for_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = tmp_path / "Local"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")
    monkeypatch.setattr(paths_module.sys, "platform", "win32")

    paths = AppPaths.for_windows(_environ(tmp_path))

    # The caches stay with the data rather than following the link or stopping startup.
    assert paths.cache_dir == paths.data_dir
    assert paths.webview_profile == paths.data_dir / "webview"


def test_the_composition_root_places_caches_where_the_launcher_resolved_them(
    tmp_path: Path,
) -> None:
    paths = AppPaths(
        data_dir=(tmp_path / "data").resolve(),
        cache_root=(tmp_path / "local").resolve(),
    )
    paths.data_dir.mkdir()

    app = app_factory.build_app(paths=paths, serve_frontend=False)

    assert app.state.default_gguf_models_dir == paths.cache_dir / "gguf_models"
    assert app.state.required_paths[0] == paths.data_dir
    assert paths.database.is_file()
    assert not (paths.data_dir / "gguf_models").exists()


def test_a_cache_folder_that_cannot_be_prepared_does_not_stop_the_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(_self: AppPaths) -> Path:
        raise AppPathError("Cortex could not secure its private data permissions.")

    monkeypatch.setattr(AppPaths, "ensure_cache_dir", refuse)
    paths = AppPaths(
        data_dir=(tmp_path / "data").resolve(), cache_root=(tmp_path / "local").resolve()
    )

    with caplog.at_level("WARNING", logger="cortex.launcher"):
        chosen = launcher_main._prepare_cache_dir(paths)

    assert chosen.cache_dir == paths.data_dir
    assert chosen.webview_profile == paths.data_dir / "webview"
    assert "caches stay in the data folder" in caplog.text
    assert not (tmp_path / "local").exists()


def test_a_redirected_profile_is_noted_in_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = (tmp_path / "local").resolve()
    paths = AppPaths(data_dir=root, cache_root=root, local_fallback=True)

    with caplog.at_level("WARNING", logger="cortex.launcher"):
        launcher_main._prepare_cache_dir(paths)

    assert "network path" in caplog.text
