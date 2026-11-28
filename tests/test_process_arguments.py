"""Start-Process receives every argument intact (issues #707, #708).

Start-Process -ArgumentList joins its array with spaces and quotes nothing, so
a data directory or checkout path containing a space reached the child as
several arguments. scripts/process_arguments.ps1 quotes each argument; these
tests launch a real child through Start-Process and compare its argv.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "process_arguments.ps1"

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("pwsh") is None,
    reason="Windows command-line quoting, run through PowerShell 7",
)

VALUES = [
    r"C:\Users\Jane Doe\AppData\Local\Temp\cortex-package-smoke-1",
    "plain",
    r"C:\path with space\ends with backslash" + "\\",
    r"C:\no-space\ends-with-backslash" + "\\",
    'has "inner" quotes',
    r'back\"slash quote',
    "tab\there",
    "",
]


def test_every_argument_reaches_the_child_unchanged(tmp_path: Path) -> None:
    output = tmp_path / "argv.json"
    script = tmp_path / "argv.py"
    script.write_text(
        "import json, sys\n"
        "open(sys.argv[1], 'w', encoding='utf-8').write(json.dumps(sys.argv[2:]))\n",
        encoding="utf-8",
    )
    command = (
        "$ErrorActionPreference = 'Stop'\n"
        f". '{HELPER}'\n"
        "$values = $env:ARGV_VALUES | ConvertFrom-Json\n"
        "$arguments = @($env:ARGV_SCRIPT, $env:ARGV_OUTPUT) + @($values) | "
        "ForEach-Object { ConvertTo-ProcessArgument $_ }\n"
        "$process = Start-Process -FilePath $env:ARGV_PYTHON -ArgumentList $arguments "
        "-NoNewWindow -Wait -PassThru\n"
        "exit $process.ExitCode\n"
    )
    env = {
        **os.environ,
        "ARGV_VALUES": json.dumps(VALUES),
        "ARGV_SCRIPT": str(script),
        "ARGV_OUTPUT": str(output),
        "ARGV_PYTHON": sys.executable,
    }
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, env=env, timeout=60, check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(output.read_text(encoding="utf-8")) == VALUES


def test_every_path_launch_quotes_its_arguments() -> None:
    sites = {
        ROOT / "packaging" / "build_windows.ps1": "$smokeArguments",
        ROOT / "tools" / "screenshots" / "tour.ps1": "$serverArgs",
        ROOT / ".github" / "workflows" / "release.yml": "$smokeArguments",
        ROOT / ".github" / "workflows" / "quality.yml": "$smokeArguments",
    }
    for path, variable in sites.items():
        text = path.read_text(encoding="utf-8")
        assert "process_arguments.ps1" in text, path
        assert "ConvertTo-ProcessArgument" in text, path
        assert f"-ArgumentList {variable}" in text, path
