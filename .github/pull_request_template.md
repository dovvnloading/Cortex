<!--
Fill in every section; write "None" or "Not applicable" instead of deleting one.
The headings follow the handoff list in AGENTS.md. Do not paste prompts,
responses, memories, tokens, or private notes, and do not stage local
databases or build output.
-->

## Problem

<!-- The user-visible or reliability effect, in plain terms. -->

## Root cause

<!-- Why it happens, and how you know: the baseline you reproduced. -->

## Change

<!-- What this pull request does. -->

## Compatibility and rollback

<!-- Data, settings, and API compatibility, and how to undo the change. -->

## Checks

<!--
The exact commands you ran, with counts and outcomes. Say which you did not run.
For example: `python -m pytest -q` -- 1234 passed.
-->

## Security, data loss and concurrency

<!-- What could be exposed, lost, or raced, and what guards against it. -->

## Limits

<!-- Known limits and follow-up work. A roadmap item is not "implemented". -->

## Before merging

- [ ] `./scripts/check.ps1` passes, or the failing step is explained above.
- [ ] `Change_Log.md` has an entry under `[Unreleased]` for any user-visible change (or none is needed).
- [ ] If API models changed, `python tools/generate_contracts.py --write` was run and both generated artifacts were reviewed (or no API model changed).
