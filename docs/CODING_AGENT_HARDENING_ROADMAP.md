# Coding Agent Hardening Roadmap

Status: proposed target architecture
Last reviewed: 2026-08-23

## Purpose

Cortex currently has a durable, approval-gated runner for short, validated
Python programs. That runner is useful for bounded computation, but it is not a
workspace coding agent: it has no repository snapshot, edit transaction,
checkpoint, terminal session, iterative tool loop, steering boundary, or
proof-based completion record.

This roadmap defines the minimum reliability bar for adding that product. It
does not expand the authority of the existing runner and it is not a claim of
competitor parity. The completed scope in
`OPEN_SOURCE_EXECUTION_PLAN.md` remains the historical plan for bounded
compute and image transforms.

## Product invariant

A coding run is a durable transaction over a versioned workspace, not a chat
response that happens to invoke commands.

Every run must make these facts recoverable and auditable:

- the user request, accepted plan, limits, model, and policy mode;
- every tool call, approval, command, process, output cursor, and exit status;
- the workspace preimage, every proposed and committed change, and conflicts;
- checkpoints and restores, including changes made through shell commands;
- verification commands, results, unresolved risks, and the stopping reason;
- commits, pull requests, and the exact diff delivered to the user.

## Immediate safety baseline

Before a workspace agent exists, the bounded runner must fail closed:

- Process execution stays disabled until Windows can enforce a restricted
  token or AppContainer-style filesystem and network boundary. A Job Object
  limits resources and descendants; it does not remove the user's ambient
  authority.
- User approval is bound to validated source and capabilities. The primary UI
  must load, hash, and match the exact source before enabling approval.
- Cancellation and successful completion must have one atomic order. A
  cancellation that commits first wins; an already committed result is never
  relabeled as cancelled.
- A live recovery supervisor renews its ownership lease. A coordinator that
  encounters another live job lease leaves that job untouched.
- Pending approvals must be listed before terminal history so consent cannot
  disappear behind newer notifications.

## Observed gaps after the baseline

These are concrete follow-up items, not hypothetical feature ideas:

| Priority | Gap | Required correction |
| --- | --- | --- |
| Release blocker | The model can propose only a one-shot bounded program; execution results do not drive a resumable tool loop or repository edit transaction. | Implement the durable run, workspace, policy, and checkpoint milestones below before using the “coding agent” label. |
| P2 | Code-job creation and approval creation use separate transactions, so a crash between them can strand a queued job without approval. | Create the job, queued event, approval scope, and approval event atomically. |
| P2 | Restart recovery reads only the newest 200 nonterminal jobs and starts one waiting thread per code approval. | Replace it with paginated recovery and a bounded supervisor queue. |
| P2 | The task UI starts overlapping one-second polls; stale responses can roll visible state backward and idle polling causes repeated per-job SQLite reads. | Use a single-flight monitor, monotonic sequence merging, idle backoff, and batched list reads. |
| P2 | Execution SSE polls SQLite at 100 Hz, closes after roughly six idle seconds, and has no reconnecting UI consumer, while approvals may wait five minutes. | Add repository notifications or low-frequency heartbeats plus cursor-based reconnect and session-expiry handling. |
| P2 | Terminal code records are hidden after backend restart, summary output is truncated without a full-output action, and queued/cancelling states are labeled as running. | Preserve durable code history and expose exact state, progress, and lazy full output. |

## Delivery sequence

### 1. Durable run and tool ledger

Persist coding runs separately from chat generations. Add versioned records for
plans, tool calls, approval scopes, terminal processes, output chunks, budgets,
check results, and terminal run summaries. Recovery must reconcile every
nonterminal record after backend restart without duplicating a tool call.

Exit criteria:

- restart, session expiry, browser reload, and SSE reconnect tests;
- idempotency tests at every durable boundary;
- no unbounded polling or one-thread-per-pending-approval design;
- explicit time, tool-call, retry, output, and cost/effort budgets.

### 2. Conflict-safe workspace transactions

Introduce read-only workspace snapshots first. Mutations then use canonical
paths plus preimage hashes and fail visibly if the user or another agent changed
a target. Multi-file patches commit atomically where supported and report
partial application otherwise. Untracked files are user-owned by default.

Exit criteria:

- symlink, junction, case-folding, traversal, and concurrent-edit tests on
  Windows;
- deterministic diff preview before approval;
- shell-written files receive the same change tracking as built-in edit tools;
- rollback never overwrites user edits made after the checkpoint.

### 3. Deterministic policy and native isolation

Separate agent intent from enforcement. Support read-only plan, ask, allowlisted
auto-run, and explicitly unrestricted modes. Deny rules win. Policy scopes
include tool, executable and arguments, canonical path, network destination,
MCP server, and duration.

Exit criteria:

- restrictions are inherited by every subprocess and execution channel;
- process trees are attachable, bounded, cancellable, and killed on timeout;
- commands, cwd, redacted environment, start/end time, exit code, stdout,
  stderr, truncation, and changed paths are durable;
- sandbox setup fails closed; it never silently falls back to ambient authority.

### 4. Checkpoints, rewind, and audit artifacts

Create an automatic checkpoint before every mutating operation. Conversation
rewind and workspace rewind are independent. Restores create their own
checkpoint so they are reversible.

Exit criteria:

- turn, file, and hunk-level diff inspection;
- checkpoint coverage for editor tools, scripts, and shell commands;
- full run export with redacted logs, checks, final diff, commits, PR links, and
  unresolved risks.

### 5. Bounded agent loop and verification

Only after the previous boundaries are qualified should the model receive
workspace read/edit/search/terminal tools. Plans are editable and approved
before mutation. Steering is queued and delivered at safe tool boundaries;
hard interrupt remains available.

The completion loop is finite:

1. discover repository instructions;
2. reproduce or establish a baseline;
3. implement the accepted plan;
4. run targeted checks;
5. run proportional broader checks;
6. perform an independent final-diff review;
7. stop with evidence, or stop at the configured budget with an explicit
   blocker.

No run may continue merely to find “one more improvement.”

### 6. Parallel and remote execution

Editing agents use separate worktrees or equivalent isolated snapshots by
default. Shared-workspace mode requires path ownership and collision detection.
Remote runs add signed commits, secret redaction, network policy, resumable
logs, and PR delivery only after the same local invariants pass.

## Competitive reference points

The target combines documented primitives rather than copying one interface:

- [Claude Code permission modes](https://code.claude.com/docs/en/permission-modes),
  [sandboxing](https://code.claude.com/docs/en/sandboxing),
  [checkpoints](https://code.claude.com/docs/en/how-claude-code-works), and
  [resumable sessions](https://code.claude.com/docs/en/sessions);
- [Cursor plan mode](https://cursor.com/docs/agent/plan-mode),
  [run modes and sandbox policy](https://prod.cursor.com/docs/agent/security/run-modes),
  and [post-task Agent Review](https://cursor.com/docs/agent/agent-review);
- [GitHub Copilot agent session logs and controls](https://docs.github.com/en/copilot/how-tos/copilot-on-github/use-copilot-agents/manage-and-track-agents),
  [CLI rewind](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference),
  and [cloud-agent firewall](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/customize-the-firewall);
- [Gemini CLI policy engine](https://geminicli.com/docs/reference/policy-engine/),
  [plan mode](https://geminicli.com/docs/cli/plan-mode/),
  [sessions](https://geminicli.com/docs/cli/session-management/), and
  [checkpointing](https://geminicli.com/docs/cli/checkpointing/);
- [Devin Desktop / Cascade](https://docs.devin.ai/desktop/cascade/cascade) and
  its [terminal policy](https://docs.devin.ai/desktop/terminal).

Classifier-based auto-review is a convenience, not a security boundary.
Checkpointing should be automatic, network policy must cover setup and plugin
channels, and parallel writers must be isolated rather than allowed to race.

## Preview release gate

Cortex may label the feature a coding-agent preview only when all of the
following are true:

- durable runs resume after reload and restart;
- plan and approval modes are explicit;
- edit conflicts cannot silently overwrite user work;
- every mutation has a reversible checkpoint;
- native sandbox enforcement is qualified on supported platforms;
- process trees and output are observable and cancellable;
- verification evidence and the final diff are first-class UI artifacts;
- independent security, recovery, concurrency, and destructive-action suites
  pass in CI.

Until then, the UI and documentation should call the existing feature
“approval-gated local code execution,” not a coding agent.
