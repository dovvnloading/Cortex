"""Quality artifacts survive "Re-run failed jobs" (issue #721).

A re-run uploads under the same artifact name in the same workflow run. The
pinned upload action refuses an existing name unless ``overwrite`` is set, so
a retried shard whose tests pass would still fail on its upload, and the
required Quality gate would stay red.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _upload_steps(text: str) -> list[list[str]]:
    """Each upload-artifact step's ``with:`` lines, in order."""

    lines = text.splitlines()
    steps: list[list[str]] = []
    for index, line in enumerate(lines):
        if "uses: actions/upload-artifact@" not in line:
            continue
        indent = len(line) - len(line.lstrip())
        block: list[str] = []
        for following in lines[index + 1 :]:
            if following.strip() and len(following) - len(following.lstrip()) <= indent - 2:
                break
            block.append(following.strip())
        steps.append(block)
    return steps


def test_every_quality_upload_can_be_replaced_by_a_rerun() -> None:
    steps = _upload_steps((WORKFLOWS / "quality.yml").read_text(encoding="utf-8"))

    assert len(steps) == 3
    for block in steps:
        assert "overwrite: true" in block, block


def test_shard_artifacts_are_still_downloaded_by_pattern() -> None:
    # A partial re-run keeps the shards that passed the first time, so the
    # aggregate must keep collecting every shard by name pattern.
    text = (WORKFLOWS / "quality.yml").read_text(encoding="utf-8")

    assert "pattern: backend-shard-*" in text
    assert "name: backend-shard-${{ matrix.shard }}" in text
