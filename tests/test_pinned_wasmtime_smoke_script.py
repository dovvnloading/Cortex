from pathlib import Path


SCRIPT = (
    Path(__file__).parents[1]
    / "tools"
    / "execution_spikes"
    / "run_pinned_wasmtime_smoke.ps1"
)


def test_wasmtime_smoke_uses_an_immutable_reviewed_pin() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "param(" not in source
    assert '$ApprovedWasmtimeVersion = "46.0.1"' in source
    assert (
        '$ApprovedWasmtimeSha256 = '
        '"559b0753e3ea311fd16000fe51c08592a625e61ebb8640601ae7173fc516e430"'
    ) in source


def test_wasmtime_smoke_fails_closed_and_runs_only_from_disposable_venv() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "$VenvPython = Join-Path $VenvRoot \"Scripts\\python.exe\"" in source
    assert "& python -I -m venv $VenvRoot" in source
    assert "& $VenvPython -I -m pip download" in source
    assert "& $VenvPython -I -m pip install" in source
    assert "if ($LASTEXITCODE -ne 0)" in source
    assert '"Wasmtime resolved outside the disposable environment."' in source
    assert "StartsWith($venvRootFull + [IO.Path]::DirectorySeparatorChar" in source
    assert '& $VenvPython -I (Join-Path $RepositoryRoot' in source
