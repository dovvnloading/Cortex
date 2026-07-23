"""Fixed recipe worker package entrypoint.

This entrypoint intentionally refuses to run until the native broker adapter is
provided by the reviewed launcher stage.  It exists so packaging, dependency
closure, and signed-bundle provenance can be qualified without creating an
unsafe stdio, shell, or host-process fallback.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """Refuse every launch until the native broker contract is wired in."""

    del argv
    # A non-zero deterministic exit is important: a packaged but unbound worker
    # must never look healthy to a caller that accidentally starts it directly.
    return 78


if __name__ == "__main__":  # pragma: no cover - exercised by the packaged exe.
    raise SystemExit(main(sys.argv[1:]))
