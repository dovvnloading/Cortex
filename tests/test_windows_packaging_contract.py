"""Contract checks for the maintainer's local Windows release packaging path."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_default_windows_package_uses_the_release_hash_locked_runtime() -> None:
    build = (ROOT / "packaging" / "build_windows.ps1").read_text(encoding="utf-8")
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    quality = (ROOT / ".github" / "workflows" / "quality.yml").read_text(
        encoding="utf-8"
    )

    assert 'python -m pip install --disable-pip-version-check --require-hashes -r requirements.lock.txt' in build
    assert "python -m pip install -r requirements.lock.txt" in release
    assert "python -m pip install -r requirements.lock.txt" in quality
    assert '($PythonVersion -join "").Trim() -ne "3.11"' in build
    assert "Locked Cortex Windows packages require Python 3.11" in build
    assert "python -m pip check" in build

    # Loose dependency resolution must be opt-in, never the normal path.
    assert "if ($UnlockedDependencies) {" in build
    assert build.index("if ($UnlockedDependencies) {") < build.index(
        "python -m pip install --disable-pip-version-check -r requirements.txt"
    )
    assert "Write-Warning \"UNLOCKED DEVELOPMENT BUILD" in build


def test_local_windows_package_smokes_the_built_launcher() -> None:
    build = (ROOT / "packaging" / "build_windows.ps1").read_text(encoding="utf-8")

    # The final success message is behind executable, WebView2 signature, and
    # packaged-health checks, as in CI; a non-starting binary cannot pass.
    assert "Get-AuthenticodeSignature" in build
    assert "Start-Process -FilePath $executable" in build
    assert "/api/v1/health/ready" in build
    assert "if (-not $smokeReady)" in build
    assert "Stop-Process -Id $smokeProcess.Id" in build
    assert build.index('if (-not $smokeReady)') < build.index(
        'Write-Host "Cortex Windows package ready: $executable"'
    )
