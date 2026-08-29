# Local Coding-Agent Harness Standard

**Status:** repository standard and staged implementation plan
**Research snapshot:** 2026-08-28
**Applies to:** any future Cortex feature that lets a model inspect, modify, or verify a local software workspace

## Decision summary

Cortex is a local-first Windows desktop assistant with a strong bounded-execution
foundation. It is not currently a general-purpose coding agent. The existing
execution plan deliberately excludes an arbitrary shell, direct workspace
mutation, and autonomous coding loops. That boundary is correct and must not be
eroded by adding a convenient tool call to the chat path.

The target is an **opt-in, local coding harness** with the following properties:

- a durable run and tool ledger rather than an in-memory loop;
- a read-only discovery and planning phase before any mutation;
- semantic repository tools plus a stateful, bounded terminal where a terminal is
  actually needed;
- explicit, capability-scoped approval enforced by the host, not by prompt text;
- an OS/container boundary that fails closed when it cannot be qualified;
- conflict-safe workspace transactions, checkpoints, rewind, and crash resume;
- an iterative observe -> act -> verify loop with hard budgets and steering;
- evidence-backed verification and an independent final diff review; and
- isolated worktrees for parallel work, with complete trajectories that can be
  inspected and replayed.

This document is normative for the architecture and release gates. The root
[`AGENTS.md`](../AGENTS.md) is the shorter operating contract that agents and
contributors should apply to ordinary repository work now. The existing
[`CODING_AGENT_HARDENING_ROADMAP.md`](CODING_AGENT_HARDENING_ROADMAP.md) remains
the detailed delivery backlog; this document adds the cross-harness capability
standard and the evidence required before expanding Cortex's authority.

### What this change does not claim

This standard does not claim that the current chat product has reached feature
parity with every coding product. “1:1 with the leading harnesses” is not a
meaningful security or engineering target when the products make different
trust assumptions: for example, some run on Linux containers, some rely on
best-effort classifiers, and some explicitly offer an unsandboxed local mode.
The useful target is capability-complete behavior with a stricter, inspectable
trust model on Cortex's supported Windows path.

This patch also does not turn on arbitrary shell or workspace mutation. Those
capabilities are only eligible after the qualification gates in this document
and the hardening roadmap pass.

## Research method and primary-source comparison

The comparison was performed against current official documentation or source
repositories, not recollection. The sources were reviewed for the operating
contract behind the UI: context discovery, tool loops, permissions, isolation,
recovery, verification, review, and extensibility.

| Harness or standard | Primary source | Capability that matters to Cortex |
| --- | --- | --- |
| OpenAI Codex CLI | [CLI documentation](https://learn.chatgpt.com/docs/codex/cli), [latest-model guidance](https://developers.openai.com/api/docs/guides/latest-model) | Local repository inspection/edit/run, `AGENTS.md` initialization, explicit permissions and model/reasoning controls, review, repeatable `exec` workflows, and explicit success/stopping criteria. |
| Claude Code | [Permissions](https://code.claude.com/docs/en/permissions), [sandboxing](https://code.claude.com/docs/en/sandboxing), [how it works](https://code.claude.com/docs/en/how-claude-code-works), [sessions](https://code.claude.com/docs/en/sessions) | Harness-enforced deny/ask/allow policy, read-only plan mode, OS-level filesystem/network enforcement, an interruptible context/action/verify loop, and resumable transcripts. Native Windows sandbox support is a documented limitation, so Cortex must not copy a Linux assumption onto Windows. |
| Gemini CLI | [Policy engine](https://geminicli.com/docs/reference/policy-engine/), [Plan Mode](https://geminicli.com/docs/cli/plan-mode/), [checkpointing](https://geminicli.com/docs/cli/checkpointing/), [sessions](https://geminicli.com/docs/cli/session-management/) | Policy decisions can allow, ask, or deny; Plan Mode is read-only; approved mutations checkpoint before execution; sessions and worktrees support resume and parallel work. The documentation currently calls out a workspace-policy limitation, which reinforces the need for runtime enforcement rather than configuration alone. |
| Cursor | [Run modes and security](https://prod.cursor.com/docs/agent/security/run-modes), [Plan Mode](https://prod.cursor.com/docs/agent/plan-mode), [Agent Review](https://prod.cursor.com/docs/agent/agent-review) | Distinct review/allowlist/run-everything modes, project filesystem/network controls, protected paths, reviewable plans, and post-commit or manual diff review. Its classifier is explicitly not a security boundary. |
| GitHub Copilot CLI/agents | [CLI reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference), [session management](https://docs.github.com/en/copilot/how-tos/copilot-on-github/use-copilot-agents/manage-and-track-agents), [custom instructions](https://docs.github.com/en/copilot/customizing-copilot/adding-repository-custom-instructions-for-github-copilot), [firewall](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/customize-the-firewall) | Enforced read-only planning, sandbox and isolated worktree options, crash restoration, steer/stop behavior, session logs, OTel telemetry, and hierarchical `AGENTS.md`/instruction discovery. Firewall controls have documented scope limits, so they cannot substitute for a complete sandbox. |
| Aider | [Repository map](https://aider.chat/docs/repomap.html), [lint/test loop](https://aider.chat/docs/usage/lint-test.html), [Git integration](https://aider.chat/docs/git.html), [modes](https://aider.chat/docs/usage/modes.html) | Symbol-aware repository context, automatic lint/test feedback, visible diffs, commits and undo, plus separate ask/code/architect modes. Its handling of dirty files and hooks is a useful reminder that Git safety behavior must be explicit. |
| OpenHands | [Runtime architecture](https://docs.openhands.dev/openhands/usage/architecture/runtime), [architecture source](https://github.com/OpenHands/OpenHands/blob/main/docs/architecture.md) | Separates the agent UI/server from the execution runtime; uses Docker or another runtime boundary for arbitrary code, with reproducible images, mounts, copy-on-write options, and resource control. Local host access is treated as trusted rather than magically safe. |
| SWE-agent / mini-SWE-agent | [SWE-agent architecture](https://swe-agent.com/0.7/background/architecture/), [trajectory reference](https://swe-agent.com/latest/reference/agent/), [mini-SWE environments](https://mini-swe-agent.com/latest/advanced/environments/) | Separates agent logic from environments, supports stateful shell sessions, saves full trajectories, and makes the sandbox backend swappable. Its local backend is explicitly not isolation for untrusted work. |
| Cline | [Project source and CLI](https://github.com/cline/cline), [permission handling](https://github.com/cline/cline/blob/main/docs/sdk/guides/permission-handling.mdx) | Per-tool approval/auto-approval policy, CLI plan mode, rules, checkpoints/undo, local model support, MCP integration, and a clear requirement that denied tools return a result the agent can adapt to. |
| OpenCode | [Agents and permissions](https://opencode.ai/docs/agents/) | Separate Build/Plan/read-only subagents, configurable per-tool permissions, max-step limits, child sessions, and explicit controls for external directories, web access, skills, and loop detection. |
| Model Context Protocol | [Specification overview](https://modelcontextprotocol.io/specification/2025-06-18/index), [tasks](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks), [sampling security](https://modelcontextprotocol.io/specification/draft/client/sampling) | Capability negotiation, consent, authorization, task identity binding, rate/iteration limits, and the principle that tool descriptions and server-provided content are not automatically trusted. |
| NIST SSDF | [SP 800-218](https://csrc.nist.gov/pubs/sp/800/218/final) | Secure-development outcomes: protect the development environment, preserve provenance, verify changes, and retain evidence rather than relying on a model's assertion. |
| OWASP Agentic Security | [Excessive Agency](https://genai.owasp.org/llmrisk/llm062025-excessive-agency/), [threats and mitigations](https://genai.owasp.org/resource/agentic-ai-threats-and-mitigations/) | A threat model for excessive functionality, excessive permissions, excessive autonomy, prompt injection, tool misuse, and unexpected code execution. |

### Cross-harness findings

No single competitor provides the complete answer. The strongest common
denominator is an auditable control plane around a model, not a particular
prompt or UI:

1. **Context is engineered.** Leading systems discover repository instructions,
   use a bounded map or search strategy, and compact history without losing
   constraints or decisions.
2. **Plan and run are separate trust states.** Planning can inspect and reason
   without changing the workspace. Mutation is a deliberate transition with a
   reviewable scope.
3. **The loop is iterative.** A coding task is context -> tool action -> tool
   result -> verification -> next action, with user steering and hard stop
   conditions. One model response containing code is not a coding harness.
4. **Policy is enforced outside the model.** Prompt instructions, rules files,
   classifiers, and tool descriptions can guide behavior but cannot be the
   security boundary.
5. **Recovery is a product feature.** Checkpoints, undo, session resume, event
   cursors, leases, and trajectory export are required when a run can mutate a
   repository or consume substantial time.
6. **Verification is evidence.** Tests, lint, builds, diffs, and review are
   captured as structured results. A green-looking conversation is not proof.
7. **Parallelism requires isolation.** Separate worktrees or equivalent
   workspace ownership are needed before child agents can work concurrently.

## Current Cortex assessment

### Already present

- local model selection and local-first data ownership;
- externalized system and code-execution prompts;
- bounded scratch/image/attachment capabilities;
- approval-gated Python proposals with source and capability digests;
- out-of-process worker boundaries, resource/watchdog qualification, and
  cancellation/revoke behavior;
- durable SQLite execution jobs, events, leases, artifacts, and approvals;
- loopback API/native-session boundaries and ordered job event streams; and
- broad backend, frontend, contract, security, and lifecycle qualification.

### Missing for a real coding harness

- a versioned workspace checkout or snapshot with a defined base commit;
- repository map, symbol search, instruction discovery, and context budgeting;
- a persistent multi-step tool loop with a bounded context window;
- semantic read/search/edit tools and a stateful terminal session;
- per-tool, per-path, executable, network, and MCP permissions;
- a qualified Windows-capable sandbox or an explicit fail-closed refusal;
- preimage conflict detection and atomic workspace transactions;
- reversible checkpoints that include filesystem, Git, transcript, and tool
  state;
- crash-safe session resume and replayable event cursors;
- baseline, targeted, full, mutation/adversarial, and independent review stages;
- worktree ownership for parallel agents; and
- a structured run/evidence export suitable for support, review, and evaluation.

These are capability gaps, not newly discovered defects in the current bounded
execution implementation. The repository's existing execution roadmap already
names most of them. The correct next step is to implement them as a new,
explicitly gated capability tier rather than silently changing ordinary chat.

## Target operating model

### Run state machine

Every coding run must have a durable state and append-only transition record.
The UI may project a friendlier view, but it must not invent state that is not
in the run ledger.

```text
created
  -> intake
  -> trust_and_instructions
  -> baseline
  -> planning
  -> awaiting_approval
  -> executing <-> awaiting_steering
  -> verification
  -> independent_review
  -> delivery

Any active state -> cancelling -> cancelled
Any recoverable state -> recovering -> the last safe state
Any state -> blocked (with an exact reason and required user action)
```

The run is not complete merely because the model emits a final message. A
successful delivery requires a recorded diff, verification evidence, and the
requested delivery artifact (for example, a commit or patch). If the user
asked only for diagnosis, the run ends after evidence-backed findings and must
not write files.

### Durable records

The minimum durable model is:

| Record | Required fields | Why it exists |
| --- | --- | --- |
| `AgentRun` | run ID, thread ID, workspace ID, base revision, request digest, instruction digest, policy digest, model/reasoning, budgets, state, timestamps | Makes a run reproducible and binds it to the workspace and policy that authorized it. |
| `ToolInvocation` | invocation ID, parent/sequence, tool/version, structured input digest, capability, approval ID, sandbox identity, start/end, status, exit, output references, changed paths | Makes every action inspectable and retryable without trusting chat text. |
| `WorkspaceSnapshot` | root/checkout, tracked and untracked inventory, preimage hashes, ignored-path policy, base commit, ownership | Detects conflicts and prevents accidental loss of user work. |
| `Checkpoint` | sequence, pre/post workspace references, Git/worktree reference, transcript cursor, tool cursor, policy reference, restore status | Makes mutation reversible and recovery deterministic. |
| `VerificationEvidence` | command/tool, exact argv or structured request, environment digest, exit status, duration, output/artifact reference, test identifiers | Prevents unsupported “tests pass” claims. |
| `Approval` | approval ID, requested capability, exact source/plan digest, path/network/process scope, expiry, one-use/revocation status, decision actor | Prevents a broad or stale approval from being reused for a different action. |

Records should use content-addressed or immutable output references where
practical. Sensitive contents must be redacted or kept local with explicit
retention rules; the ledger still records that an output existed, its digest,
and the reason it was withheld.

### Tool surface

The initial tool surface should be small, typed, and composable. Tool names are
illustrative; the enforcement and result schema are the contract.

| Class | Initial tools | Default mode |
| --- | --- | --- |
| Repository read | `repo.list`, `repo.read`, `repo.search`, `repo.symbols`, `repo.instructions` | Allowed inside the selected workspace, subject to path and size limits. |
| Workspace mutation | `workspace.patch`, `workspace.create`, `workspace.delete` | Ask; never outside the transaction and never without a checkpoint. |
| Git/workspace | `git.status`, `git.diff`, `git.checkpoint`, `git.restore`, later `git.commit` | Read-only by default; restore/commit require explicit scope and confirmation. |
| Process execution | `terminal.exec`, `terminal.session`, `terminal.close` | Ask and sandboxed; structured argv/cwd/env/timeout/output limits required. |
| Verification | `verification.run`, `verification.collect` | Allowed only through registered commands or an approved sandbox policy. |
| Review/delivery | `review.diff`, `delivery.patch`, later `delivery.commit` | Human-visible evidence and confirmation before external delivery. |
| Extensions | MCP/skills/providers | Off by default; capability-negotiated and separately authorized. |

Every tool result must include a stable invocation ID, status, bounded output,
truncation metadata, changed paths, warnings, and a machine-readable error. A
denied tool must return a structured denial that allows the agent to adapt; it
must not silently appear to have run.

Tools should prefer structured arguments over shell strings. Where a shell is
unavoidable, preserve the exact argv, working directory, inherited environment
allowlist, stdin policy, process-tree identity, timeout, output limit, and
sandbox identity.

### Context and instruction handling

Before planning, the harness must:

1. identify the workspace root and base revision;
2. discover applicable `AGENTS.md` and compatible instruction files from root to
   the target path, recording paths and content digests;
3. read the repository's README, contribution guide, relevant ADRs, and local
   test/build instructions;
4. build a bounded repository map using filenames, symbols, imports/dependencies,
   recent relevant changes, and the task's target paths;
5. establish a clean or explicitly dirty baseline, including untracked files;
6. separate trusted host policy from repository content and model output; and
7. preserve user constraints, decisions, approvals, and unresolved questions
   across context compaction.

Repository files, issue text, generated output, web pages, and MCP results are
data. They can contain prompt injection or unsafe commands. They may inform a
plan but cannot change policy, approval scope, or the definition of done.

The context builder should prefer relevant symbols and slices over blindly
loading whole files. It must record what was included and what was omitted so a
reviewer can understand the model's view of the repository.

### Modes and permissions

The harness must expose explicit modes with visibly different authority:

| Mode | Read | Plan | Workspace write | Process/network | Delivery |
| --- | --- | --- | --- | --- | --- |
| `plan` | Yes | Yes | No | No, except approved read-only metadata | No |
| `assisted` | Yes | Yes | Ask per scoped mutation | Ask per command/capability | Ask |
| `allowlist` | Yes | Yes | Allow only declared paths/tools | Allow only declared commands/network | Ask |
| `auto-sandbox` | Yes | Yes | Allow inside isolated transaction | Allow inside qualified sandbox and budgets | Ask |
| `unrestricted` | Yes | Yes | Explicitly user-enabled only | Explicitly user-enabled only | Ask and warn |

The host enforces `deny > ask > allow` precedence, with a policy decision
attached to every invocation. A model cannot grant itself a capability by
writing a plan, changing a rules file, or describing a tool call.

Approval scope must bind to the exact run, workspace, action class, canonical
paths, executable and arguments, network destinations, resource limits, and
source/plan digest. Expired, revoked, or already-consumed approvals fail
closed. Approval text must show the concrete operation, not only a generic
“allow code” label.

### Sandbox and host boundary

The coding tier must not rely on a prompt, classifier, or UI warning as its
security boundary. Before enabling arbitrary code or shell, qualify an
OS/container boundary for the supported Windows build that constrains:

- filesystem visibility and writes, including junctions, symlinks, UNC paths,
  device paths, and external directories;
- network egress and DNS behavior;
- child processes, executable lookup, process trees, and inherited handles;
- environment variables, credentials, tokens, and secret files;
- CPU, memory, wall time, output, disk, file count, and process count; and
- cancellation, timeout, crash cleanup, and post-run residue.

If the boundary is unavailable, unhealthy, or ambiguous, the coding run must
refuse to execute. “Best effort” is acceptable for a clearly labeled trusted
developer mode only; it is not acceptable for the default local coding mode.

The current bounded worker contract is the baseline for this rule: no host
filesystem/network/shell authority is added until the new boundary is
qualified by adversarial tests and an independent review.

### Workspace transactions and recovery

At intake, capture the workspace state without rewriting it. By default,
pre-existing tracked changes and untracked files belong to the user and must
be preserved. The harness must not use destructive reset/checkout operations as
an implicit cleanup strategy.

Mutation should occur in a dedicated worktree, overlay, or equivalent
copy-on-write transaction. If direct mutation is ever supported, each changed
path needs:

- a canonical path and ownership decision;
- an expected preimage hash or an explicit “new path” marker;
- a checkpoint before the mutation batch;
- an atomic write/rename strategy and postimage hash; and
- a conflict result if the preimage no longer matches.

Checkpoints must restore the workspace and enough transcript/tool cursor state
to make the next proposal intelligible. A checkpoint that only copies files
but leaves the conversation claiming an action happened is incomplete.

The supervisor must use append-only events, idempotency keys, leases/heartbeats,
and replayable cursors. One durable run supervisor should own approvals and
recovery; a new unmanaged thread for each approval is not a recovery design.
The UI must reconnect from a cursor and distinguish queued, running,
cancelling, succeeded, failed, cancelled, recovering, and blocked states.

### Observe -> act -> verify loop

The execution loop is bounded and interruptible:

```text
discover context
  -> establish baseline
  -> produce plan and predicted files/commands
  -> obtain scoped approval when required
  -> observe current state
  -> perform one small tool action
  -> record result and changed paths
  -> run the cheapest relevant verification
  -> either continue, revise, ask the user, or stop
```

Hard limits apply to model turns, tool invocations, wall time, CPU/memory,
output bytes, changed files/bytes, subprocesses, network calls, and cost/token
budget. The loop also needs a no-progress detector for repeated equivalent
actions and a maximum retry count per failing verification.

The user can interrupt between tool calls and should be able to steer the next
step without discarding the durable run. Cancellation must stop the process
tree, release leases, prevent late commits, and leave a recoverable evidence
record.

### Verification and independent review

Each coding run starts with a baseline and ends with a verification manifest.
The manifest should distinguish:

1. baseline commands and their pre-change result;
2. targeted tests for the changed behavior;
3. package/module/type/lint checks;
4. broader integration or end-to-end checks;
5. adversarial and security checks for changed boundaries;
6. mutation testing for the relevant implementation and tests; and
7. independent final diff review against the base revision.

Mutation testing must mutate implementation behavior, not merely make the test
suite green by mutating assertions. For each mutant, retain whether it was
killed, survived, timed out, or was invalid, plus the responsible test and
command. Surviving mutants become either a new test or a documented, reviewed
limitation.

The final review should be performed with fresh context from the diff, not only
the model's own summary. It should check correctness, security, data loss,
compatibility, migration/rollback, observability, and whether tests exercise
the claimed edge cases. No completion message may claim a check ran unless the
manifest contains the exact command and result.

### Parallel work and extensibility

Parallel agents require isolated worktrees or equivalent workspace ownership.
The parent run assigns path/feature ownership, records child run IDs, and
requires review before merging. Shared mutable directories, a shared package
cache with untrusted build scripts, and concurrent direct edits to the same
checkout are not a parallelism strategy.

MCP servers, skills, hooks, and external providers are extensions with their
own trust and lifecycle. They must be capability-negotiated, versioned,
audited, rate-limited, and separately revocable. Their descriptions and output
are not trusted policy. Server-initiated sampling or tool loops must remain
associated with an originating request and have human approval controls.

## Staged delivery plan

The existing hardening roadmap provides the detailed engineering sequence. The
following gates define when a stage is actually complete.

### Stage 0 — operating contract (this change)

Deliverables:

- this cross-harness standard;
- root `AGENTS.md` with the day-to-day workflow;
- README and contribution-guide links;
- explicit product language that bounded execution is not yet a coding agent.

Exit evidence: agents can discover the contract, run the documented checks, and
report changes without granting new runtime authority.

### Stage 1 — durable run and tool ledger

Deliverables: a run aggregate, append-only tool events, approval records bound
to invocation scope, output references, leases, cursor replay, and crash
recovery. Replace any approval flow that can lose the owning run or depend on a
single in-memory waiter.

Exit evidence: kill/restart/reconnect tests prove no duplicate commit, lost
approval, stale status, or orphan process; event replay reconstructs the same
state and evidence.

### Stage 2 — workspace transaction

Deliverables: base revision, tracked/untracked snapshot, dedicated worktree or
copy-on-write transaction, preimage conflict detection, atomic patching, and
checkpoint/restore of every mutation batch.

Exit evidence: dirty-user-work preservation, concurrent-edit conflict, symlink
and path traversal, cancellation during write, crash during commit, restore,
and repeated retry tests all pass.

### Stage 3 — qualified execution boundary

Deliverables: a Windows-supported sandbox backend with explicit filesystem,
network, process, resource, and cleanup controls; health attestation; and a
hard fail-closed path.

Exit evidence: hostile repository fixtures, escape attempts, secret probes,
network probes, process-tree probes, resource exhaustion, and residue checks
pass on every supported packaging/runtime configuration.

### Stage 4 — bounded coding loop

Deliverables: plan mode, semantic repository tools, stateful terminal, context
map/budgeting, steering, no-progress detection, and policy instrumentation.

Exit evidence: deterministic tool-contract tests, denial/adaptation tests,
budget tests, prompt-injection fixtures, session resume, and human interrupt
tests pass. Ordinary chat still cannot acquire coding authority implicitly.

### Stage 5 — verification and delivery

Deliverables: baseline/targeted/full verification tiers, mutation and
adversarial qualification, independent diff review, evidence manifest, and
explicit patch/commit/PR delivery actions.

Exit evidence: no successful delivery without a clean evidence manifest; failed
verification blocks delivery unless the user explicitly accepts a recorded
exception; rollback is documented and tested.

### Stage 6 — parallel and extensible harness

Deliverables: isolated child runs/worktrees, merge ownership, MCP/skills/hooks
registries, telemetry, trajectory export/replay, and a benchmark/evaluation
suite.

Exit evidence: concurrent-run collision tests, extension revocation, telemetry
redaction, replay determinism, and representative coding-task evaluations pass
without weakening the Stage 3 boundary.

## Definition of done for a coding task

An agent may report a coding task complete only when all applicable items are
true:

- the request and success criteria are explicit;
- applicable instructions and relevant repository context were discovered;
- the baseline and pre-existing worktree state were recorded;
- every mutation was within the approved transaction and is represented in the
  diff/checkpoint ledger;
- targeted checks and the appropriate broader checks ran, with exact results;
- security, compatibility, and data-loss risks were considered;
- an independent final diff review found no unresolved blocker;
- the requested delivery artifact exists and is linked to the evidence; and
- the final report names remaining limitations instead of implying certainty.

If a required condition cannot be met, the correct status is `blocked` or
`incomplete`, with the exact missing evidence and the safest next action.

## Metrics and operational signals

The harness should expose at least:

- run completion, cancellation, recovery, and blocked rates;
- tool denial, approval latency, retry, no-progress, and budget-exhaustion
  rates;
- sandbox health and escape-test results by platform/build;
- checkpoint creation/restore and conflict rates;
- verification pass/fail/skip and mutation kill/survival rates;
- time to first useful diff and time to verified delivery;
- token/input-output volume and context-compaction frequency; and
- unhandled process, event-replay, and late-write incidents.

Metrics must not require sending prompts, source, secrets, or raw model output
off the local machine. Diagnostic exports should be opt-in, redacted, and
content-addressed where possible.

## Immediate agent checklist

Until the coding tier is implemented, agents working in this repository should
follow the root contract:

1. inspect branch, status, diff, instructions, and relevant tests;
2. preserve user changes and untracked files;
3. reproduce or establish a baseline before editing;
4. make the smallest scoped change with `apply_patch`;
5. add or strengthen a test for the behavior and edge case;
6. run targeted checks, then the appropriate repository tier;
7. review the final diff and generated artifacts independently;
8. use separate Conventional Commits for separate concerns; and
9. report exact evidence, limitations, and any follow-up rather than inventing
   completion.

For the current repository, the supported runtime remains bounded local
execution. Do not introduce a general shell, direct workspace agent, networked
tool, or autonomous commit path without implementing the staged gates above.
