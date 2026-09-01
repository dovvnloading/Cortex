"""Contract generation is explicit, atomic, and strict about schema support."""

from pathlib import Path
import shutil
import subprocess

import pytest

from tools import generate_contracts


def _specification(schemas: dict[str, object]) -> dict[str, object]:
    return {"components": {"schemas": schemas}}


def test_root_enum_is_rendered_as_a_typescript_type_alias():
    rendered = generate_contracts.render_typescript(
        _specification({"Colour": {"type": "string", "enum": ["red", "blue"]}})
    )

    assert "export type Colour = \"red\" | \"blue\";" in rendered


def test_unsupported_schema_keywords_fail_loudly():
    with pytest.raises(
        generate_contracts.ContractGenerationError,
        match="Unsupported JSON schema keywords.*allOf",
    ):
        generate_contracts.render_typescript(
            _specification({"Broken": {"allOf": [{"type": "string"}]}})
        )
    with pytest.raises(
        generate_contracts.ContractGenerationError,
        match="Unsupported JSON schema keywords.*allOf",
    ):
        generate_contracts.render_typescript(
            _specification(
                {
                    "BrokenObject": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "allOf": [{"type": "object"}],
                    }
                }
            )
        )

    with pytest.raises(
        generate_contracts.ContractGenerationError,
        match="Unsupported JSON schema keywords.*not_supported",
    ):
        generate_contracts._typescript_type(
            {"type": "string", "not_supported": True}, location="test"
        )


def test_check_reports_drift_without_mutating_outputs(tmp_path: Path):
    output_dir = tmp_path / "contracts"
    output_dir.mkdir()
    openapi = output_dir / "openapi.json"
    typescript = output_dir / "cortex-api.ts"
    openapi.write_text("old\n", encoding="utf-8")
    typescript.write_text("old\n", encoding="utf-8")
    before = {path: path.read_bytes() for path in (openapi, typescript)}

    stale = generate_contracts.check_contracts(
        _specification({"Colour": {"type": "string", "enum": ["red"]}}),
        output_dir,
    )

    assert stale == ["openapi.json", "cortex-api.ts"]
    assert {path: path.read_bytes() for path in (openapi, typescript)} == before
    assert not list(tmp_path.glob(".cortex-contract-*"))


def test_write_promotes_staged_outputs_and_leaves_no_staging_directory(tmp_path: Path):
    output_dir = tmp_path / "contracts"
    generate_contracts.write_contracts(
        _specification({"Colour": {"type": "string", "enum": ["red"]}}),
        output_dir,
    )

    assert (output_dir / "openapi.json").is_file()
    assert "export type Colour = \"red\";" in (
        output_dir / "cortex-api.ts"
    ).read_text(encoding="utf-8")
    assert not list(output_dir.glob(".cortex-contract-*"))


def test_cli_requires_an_explicit_mode():
    with pytest.raises(SystemExit):
        generate_contracts._parser().parse_args([])


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="requires PowerShell 7")
def test_quality_script_rejects_zero_check_invocation():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(root / "scripts" / "check.ps1"),
            "-SkipBackend",
            "-SkipFrontend",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "At least one check surface must be enabled" in (
        result.stdout + result.stderr
    )
