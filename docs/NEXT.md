# What's next

This is the one current list of forward-looking work. It replaces four
overlapping planning documents (`CODING_AGENT_HARDENING_ROADMAP.md`,
`OPEN_SOURCE_EXECUTION_PLAN.md`, `UI_MODERNIZATION_AUDIT.md`,
`FRONTEND_HARNESS_REFACTOR_PLAN.md`), now under [`docs/archive/`](archive/) as
historical record. `AGENTS.md` is the only normative contract; this file, like
everything else under `docs/`, is background.

The current, actively maintained plan is the sequenced milestone list (M1-M7)
in [`docs/audits/2026-09-03-ownership-review.md`](audits/2026-09-03-ownership-review.md).
Track completion there. What follows is what remains once that plan's near-term
milestones land.

## Execution boundary (after the M3 consolidation)

The target shape, once the dormant broker/attestation/native-launcher modules
are removed: one child-process launch path (create-suspended, attach Job
Object, resume) shared by the three live profiles (`scratch.auto.v1`,
`code.exec.v1`, `recipe.image.v1`), with the boundary documented in one
paragraph in the README instead of implied by 25 ADRs describing a worker that
never launches.

## Coding-harness stages (not started; do not begin before M1-M4 land)

`docs/LOCAL_CODING_AGENT_HARNESS_STANDARD.md` records the researched target for
an opt-in local coding harness (durable run ledger, workspace transactions,
qualified sandbox, an observe-act-verify loop, verification and review). None
of its six stages are implemented. Per `AGENTS.md`, no stage here may be
blocked on a qualification or attestation step the maintainer cannot actually
run, and none should begin while the execution package still carries dormant
code from the abandoned signed-worker approach.

## Frontend (after the M5 hook extraction)

- Transcript export (Markdown and JSON) -- the cheapest user-facing win once
  `ChatPage` no longer needs the extraction to fight prop drilling for state.
- Finish moving `LocalModelMenu` onto `shared/ui/Select`.
- Add `jsx-a11y` and type-checked ESLint rules (`no-floating-promises`,
  `no-misused-promises`).
- A bundle-size and accessibility pass once the state/SoC refactor in the
  ownership review's section 6.5 is in place -- doing it before would mean
  auditing code about to move.

## Expansion candidates

See the ownership review, section 8, for the full list and rationale. In
order: context-usage indicator, message search (SQLite FTS5), prompt
templates, a memory-review surface for proposed-but-unconfirmed memos, PDF
attachment text extraction. Do not build the coding-harness stages, revive
semantic vector memory, or add a plugin/MCP surface ahead of these -- each adds
a trust boundary or dormant surface the project does not need yet.
