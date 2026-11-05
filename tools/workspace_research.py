"""Explicit, read-only, ephemeral local repository research from the terminal.

Examples (PowerShell, from the Cortex repository root):
  python tools/workspace_research.py --root C:\src\project --allow-read-only --list
  python tools/workspace_research.py --root C:\src\project --allow-read-only --search "class Client"
  python tools/workspace_research.py --root C:\src\project --allow-read-only --read src/main.py
  python tools/workspace_research.py --root C:\src\project --allow-read-only --model qwen3:8b --question "Where is auth checked?"

The one-shot consent flag authorizes a temporary, bounded, in-memory copy of
eligible source files. The model receives snippets only from that copy, and
only through the loopback Ollama API. No repository file is edited, no test or
shell command is executed, and no grant is written to disk. The frontend does
not expose this experimental harness yet.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
from typing import Any

_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from cortex_backend.core.settings import DEFAULT_NUM_CTX  # noqa: E402
from cortex_backend.services.workspace_agent import WorkspaceReadAgent  # noqa: E402
from cortex_backend.services.workspace_context import (  # noqa: E402
    WorkspaceAccessError,
    WorkspaceSnapshotRegistry,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="Absolute local repository directory")
    parser.add_argument(
        "--allow-read-only", action="store_true",
        help="Explicitly consent to indexing eligible source files from --root once",
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--list", action="store_true", help="List indexed relative paths")
    modes.add_argument("--search", metavar="TEXT", help="Literal substring search")
    modes.add_argument("--read", metavar="PATH", help="Read a relative source file in the snapshot")
    modes.add_argument("--question", metavar="TEXT", help="Ask a local Ollama model using read-only tools")
    parser.add_argument("--model", help="Installed local Ollama model tag (required for --question)")
    parser.add_argument(
        "--preview-patch", action="store_true",
        help="Allow the local model to stage review-only in-memory replacements (never write to checkout)",
    )
    parser.add_argument(
        "--context", type=int, default=DEFAULT_NUM_CTX,
        help="Context window for local model calls",
    )
    parser.add_argument("--start-line", type=int, default=1)
    parser.add_argument("--max-lines", type=int, default=80)
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    return parser


def _local_client() -> Any:
    import ollama

    from cortex_backend.services.chat_client import OllamaChatClient

    host = "http://127.0.0.1:11434"
    return OllamaChatClient(
        ollama.Client(host=host),
        stream_client_factory=lambda: ollama.Client(host=host),
    )


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.allow_read_only:
        parser.error("--allow-read-only is required before any source is inspected")
    if args.question and not args.model:
        parser.error("--model is required for --question")
    if args.preview_patch and args.question is None:
        parser.error("--preview-patch only applies to --question")
    if args.context < 1024 or args.context > 65536:
        parser.error("--context must be between 1024 and 65536")

    registry = WorkspaceSnapshotRegistry()
    # The grant exists only within this process, for this single caller. The
    # random opaque identifier is never accepted as a host path.
    owner = "local-cli-session"
    try:
        snapshot = registry.capture(
            owner=owner, root_path=args.root, confirmed=True,
        )
        if args.list:
            result: Any = {
                "paths": registry.list_paths(
                    owner=owner, snapshot_id=snapshot.snapshot_id, limit=200
                ),
                "file_count": snapshot.file_count,
                "total_bytes": snapshot.total_bytes,
                "truncated": snapshot.truncated,
                "skipped_entries": snapshot.skipped_entries,
            }
        elif args.search is not None:
            result = {
                "matches": [
                    asdict(hit) for hit in registry.search(
                        owner=owner, snapshot_id=snapshot.snapshot_id, query=args.search
                    )
                ]
            }
        elif args.read is not None:
            result = asdict(registry.read(
                owner=owner,
                snapshot_id=snapshot.snapshot_id,
                path=args.read,
                start_line=args.start_line,
                max_lines=args.max_lines,
            ))
        else:
            agent = WorkspaceReadAgent(
                registry=registry,
                chat_client=_local_client(),
                model=args.model,
                options={"num_ctx": args.context},
                allow_patch_preview=args.preview_patch,
            )
            response = agent.run(
                owner=owner,
                snapshot_id=snapshot.snapshot_id,
                question=args.question,
            )
            result = asdict(response)

        if args.json or not args.question:
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(result.get("answer") or f"Read-only research did not complete ({result['status']}).")
            if args.preview_patch:
                diff = result.get("preview_diff")
                if diff:
                    print("\n--- Proposed patch (NOT applied to checkout) ---\n")
                    print(diff)
                else:
                    print("No patch was staged.", file=sys.stderr)
        succeeded = not args.question or (
            result["status"] == "completed"
            and (not args.preview_patch or bool(result.get("preview_diff")))
        )
        return 0 if succeeded else 1
    except WorkspaceAccessError as exc:
        # Paths and source contents are never carried into error text.
        print(f"Workspace operation unavailable ({exc.code}).", file=sys.stderr)
        return 2
    except Exception as exc:
        # Ollama errors may contain echoed prompt fragments or URLs. Keep only
        # a diagnostic class name, never the raw exception message.
        print(f"Local research failed ({type(exc).__name__}).", file=sys.stderr)
        return 1
    finally:
        registry.close()


if __name__ == "__main__":
    raise SystemExit(main())
