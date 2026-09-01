"""Focused regression coverage for the Windows recipe-worker build wrapper."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


SCRIPT = Path(__file__).parents[1] / "packaging" / "build_recipe_worker.ps1"


def _powershell_build(
    script: Path,
    *,
    fail_phase: str | None = None,
    signing_key: Path | None = None,
    skip_dependency_install: bool = True,
) -> subprocess.CompletedProcess[str]:
    script_literal = str(script).replace("'", "''")
    signing_arguments = ""
    if signing_key is not None:
        key_literal = str(signing_key).replace("'", "''")
        signing_arguments = (
            f" -SigningKeyPath '{key_literal}' -SigningKeyId 'test-key'"
            " -BundleVersion '1.0.0' -Sequence 1"
        )
    fail_phase_literal = (fail_phase or "").replace("'", "''")
    dependency_arguments = " -SkipDependencyInstall" if skip_dependency_install else ""
    move_override = ""
    if fail_phase == "rollback":
        move_override = r"""
        function Move-Item {
            [CmdletBinding()]
            param(
                [string]$LiteralPath,
                [string]$Destination
            )
            if ($LiteralPath -like '*\.recipe-runtime-build-*\dist\recipe-runtime' -or
                $LiteralPath -like '*\.recipe-runtime-backup-*\recipe-runtime') {
                throw 'injected move failure'
            }
            Microsoft.PowerShell.Management\Move-Item @PSBoundParameters
        }
        """
    mode = f"""
        if ($arguments -contains 'pip') {{
            if (('{fail_phase_literal}' -eq 'requirements' -and $arguments -contains '-r') -or
                ('{fail_phase_literal}' -eq 'pyinstaller-install' -and
                    ($arguments -contains 'pyinstaller>=6.14,<7' -or
                        $arguments -contains 'pyinstaller==6.14.2'))) {{
                $global:LASTEXITCODE = 22
                return
            }}
        }} elseif ($arguments -contains '-c') {{
            if ('{fail_phase_literal}' -eq 'version') {{
                $global:LASTEXITCODE = 25
                return
            }}
            Write-Output $(if ('{fail_phase_literal}' -eq 'wrong-version') {{ '6.14.1' }} else {{ '6.14.2' }})
        }} elseif ($arguments -contains 'PyInstaller') {{
            if ('{fail_phase_literal}' -eq 'build') {{
                $global:LASTEXITCODE = 23
                return
            }}
            if ('{fail_phase_literal}' -ne 'missing-output') {{
                $dist_index = [Array]::IndexOf($arguments, '--distpath')
                $package = Join-Path $arguments[$dist_index + 1] 'recipe-runtime'
                New-Item -ItemType Directory -Path $package -Force | Out-Null
                Set-Content -LiteralPath (Join-Path $package 'recipe_worker.exe') -Value 'fresh' -NoNewline -Encoding ascii
            }}
        }} elseif ($arguments -contains 'tools/sign_recipe_worker.py') {{
            if ('{fail_phase_literal}' -eq 'sign') {{
                $global:LASTEXITCODE = 24
                return
            }}
            $output_index = [Array]::IndexOf($arguments, '--output-manifest')
            Set-Content -LiteralPath $arguments[$output_index + 1] -Value '{{"fresh":true}}' -NoNewline -Encoding ascii
        }}
        $global:LASTEXITCODE = 0
    """
    command = f"""
        $ErrorActionPreference = 'Stop'
        function python {{
            $arguments = @($args)
            {mode}
        }}
        {move_override}
        & '{script_literal}'{dependency_arguments}{signing_arguments}
    """
    return subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.skipif(
    os.name != "nt" or shutil.which("pwsh") is None,
    reason="requires Windows PowerShell 7",
)
def test_failed_pyinstaller_does_not_replace_existing_worker(tmp_path: Path) -> None:
    script = tmp_path / "packaging" / "build_recipe_worker.ps1"
    script.parent.mkdir()
    script.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")

    executable = tmp_path / "dist" / "recipe-runtime" / "recipe_worker.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"old")

    built = _powershell_build(script)
    assert built.returncode == 0, built.stdout + built.stderr
    assert executable.read_bytes() == b"fresh"
    assert not list(executable.parents[1].glob(".recipe-runtime-*"))

    failed = _powershell_build(script, fail_phase="build")
    assert failed.returncode != 0
    assert "PyInstaller failed" in failed.stdout + failed.stderr
    assert executable.read_bytes() == b"fresh"
    assert not list(executable.parents[1].glob(".recipe-runtime-*"))


@pytest.mark.skipif(
    os.name != "nt" or shutil.which("pwsh") is None,
    reason="requires Windows PowerShell 7",
)
@pytest.mark.parametrize(
    "fail_phase", ["requirements", "pyinstaller-install", "version", "wrong-version", "missing-output"]
)
def test_failed_native_or_incomplete_build_preserves_existing_worker(
    tmp_path: Path,
    fail_phase: str,
) -> None:
    script = tmp_path / "packaging" / "build_recipe_worker.ps1"
    script.parent.mkdir()
    script.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")

    executable = tmp_path / "dist" / "recipe-runtime" / "recipe_worker.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"old")

    failed = _powershell_build(
        script,
        fail_phase=fail_phase,
        skip_dependency_install=fail_phase == "missing-output",
    )

    assert failed.returncode != 0
    assert executable.read_bytes() == b"old"
    assert "package ready" not in failed.stdout
    assert not list(executable.parents[1].glob(".recipe-runtime-*"))


@pytest.mark.skipif(
    os.name != "nt" or shutil.which("pwsh") is None,
    reason="requires Windows PowerShell 7",
)
def test_failed_signing_preserves_existing_package_and_manifest(tmp_path: Path) -> None:
    script = tmp_path / "packaging" / "build_recipe_worker.ps1"
    script.parent.mkdir()
    script.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")

    executable = tmp_path / "dist" / "recipe-runtime" / "recipe_worker.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"old")
    manifest = tmp_path / "dist" / "recipe-runtime.manifest.json"
    manifest.write_bytes(b"old manifest")
    signing_key = tmp_path / "signing.key"
    signing_key.write_bytes(b"test key")

    failed = _powershell_build(script, fail_phase="sign", signing_key=signing_key)

    assert failed.returncode != 0
    assert "signing gate failed" in failed.stdout + failed.stderr
    assert executable.read_bytes() == b"old"
    assert manifest.read_bytes() == b"old manifest"
    assert not list(executable.parents[1].glob(".recipe-runtime-*"))


@pytest.mark.skipif(
    os.name != "nt" or shutil.which("pwsh") is None,
    reason="requires Windows PowerShell 7",
)
def test_unsigned_promotion_removes_stale_signed_manifest(tmp_path: Path) -> None:
    script = tmp_path / "packaging" / "build_recipe_worker.ps1"
    script.parent.mkdir()
    script.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")

    executable = tmp_path / "dist" / "recipe-runtime" / "recipe_worker.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"old")
    manifest = tmp_path / "dist" / "recipe-runtime.manifest.json"
    manifest.write_bytes(b"stale manifest")

    built = _powershell_build(script)

    assert built.returncode == 0, built.stdout + built.stderr
    assert executable.read_bytes() == b"fresh"
    assert not manifest.exists()
    assert "intentionally unsigned" in built.stdout
    assert not list(executable.parents[1].glob(".recipe-runtime-*"))


@pytest.mark.skipif(
    os.name != "nt" or shutil.which("pwsh") is None,
    reason="requires Windows PowerShell 7",
)
def test_failed_rollback_retains_prior_package_for_manual_recovery(tmp_path: Path) -> None:
    script = tmp_path / "packaging" / "build_recipe_worker.ps1"
    script.parent.mkdir()
    script.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")

    executable = tmp_path / "dist" / "recipe-runtime" / "recipe_worker.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"old")

    failed = _powershell_build(script, fail_phase="rollback")

    assert failed.returncode != 0
    assert "backup was retained" in failed.stdout + failed.stderr
    backups = list(executable.parents[1].glob(".recipe-runtime-backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "recipe-runtime" / "recipe_worker.exe").read_bytes() == b"old"
