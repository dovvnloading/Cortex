# Security Policy

Cortex stores conversations and memories locally. Selecting a cloud chat
model sends that turn's assembled context, including any attachments and
enabled memories, to the selected provider. Cloud credentials come from the
backend environment or the cloud identity SDK. The model settings store the
selected provider and model ID.
The Python API binds to loopback and requires an expiring authenticated native-window
session. Prompts, responses, memories, and raw model output are excluded from
diagnostic logging.

## Supported versions

Cortex has a single maintainer. Security fixes land on `main` and ship in the
next tagged release; only the latest release is supported, so upgrade before
reporting an issue when possible. The rewritten application (declared version
2.0.0) has no published release yet, so for now `main` is the supported code.
The earlier Qt-era releases, v0.95.7 and v1.0.0, predate the rewrite and are
not covered.

## Reporting a vulnerability

Please do not disclose security issues in public GitHub issues. Use GitHub's
[private vulnerability reporting](https://github.com/dovvnloading/Cortex/security/advisories/new)
or contact the repository maintainers privately.

Include:

- the affected version and Windows version;
- a concise impact description;
- reproducible steps or a proof of concept;
- relevant redacted logs or screenshots.

Do not include real prompts, conversation history, memory contents, access
tokens, or local database files.

## Security boundaries

- The API is intended for loopback use only; do not expose it through a public
  interface or reverse proxy without adding an independently reviewed security
  boundary.
- Ollama endpoints should remain local unless remote access is intentional and
  trusted.
- User data is stored under `%APPDATA%\ChatLLM\ChatLLM-Assistant`; protect the
  Windows account and back up this directory before upgrades. Downloaded models,
  the llama.cpp runtime and the WebView profile of a new install are under
  `%LOCALAPPDATA%\ChatLLM\ChatLLM-Assistant` with the same per-user permissions.
- Cortex does not log prompts, responses, memories or credentials. As a second
  line of defence its runtime log (`logs\cortex.log`) and startup log redact
  values that follow credential-like or content-like names (`token=`, `prompt=`,
  `Authorization: Bearer ...` and similar, including compound names such as
  `session_token`), keep each record to one bounded line, and stay bounded in
  size. A traceback in the runtime log keeps its frames and the class of each
  exception, and never an exception message or a source line. For an ordinary
  record that filter matches names, not meaning: free-form text a future change
  logged carelessly would not be caught.
- External links and rendered model content are validated by the frontend.
- Model-produced memory actions are validated and destructive clears require
  explicit user confirmation. A memory the model proposes is only shown under
  its answer; it is stored when the user presses Save for that one fact, and
  nothing is written on the model's say-so. Proposals are bounded in number and
  length and are kept for the session only, so an unanswered one is lost when
  the page is reloaded.
- Model-proposed code runs only after the user approves that one run, in a
  short-lived worker process with source, time, memory, output and
  child-process limits. That worker clears the environment variables it
  inherited before it runs any source, but it is a bounded containment
  layer, not operating-system isolation; see the README's "How local execution
  is bounded" section before relying on it.

Dependency or packaging concerns that could affect these boundaries should be
reported privately as well.

## Agent mode

Agent mode uses the authenticated loopback API and a separate opt-in
setting. Each run needs project read/copy consent. A cloud model needs
separate consent to send the goal and eligible project context to its
provider. Ordinary chat gets no project grant from this setting.

File changes and local Git tools operate in a disposable work copy, not
the original project. The backend requires a separately confirmed selection
to accept changed files. It verifies source hashes and saves the original
bytes in `workspace_checkpoints.sqlite` before writing. Explicit undo or
restore checks current hashes and refuses unexpected changes. It never
runs automatically. The Agent page exposes reviewed changes, separately
confirmed acceptance and discard, and confirmed journal-backed undo.
Historical runs can be reopened and their events replayed.

The Windows AppContainer launcher uses a fresh low-privilege profile. It
grants write permission only to the work copy. Permitted toolchain folders
can receive temporary read/execute grants for that run. A user-installed
tool may be unavailable unless that grant can be made each time. Network
capability is off by default. A Windows Job Object limits memory, CPU time
and child processes. Closing the job kills the process tree. AppContainer
is not a VM or a complete boundary against every host resource.

If AppContainer profile creation fails, the job-only fallback is marked
**unsandboxed**. A Job Object does not isolate files or network access.
The fallback cannot enforce a network-off request. It requires fresh,
explicit approval before use; automatic approval does not apply. Dangerous
operations are refused.

The live registry grants different tools in each mode. All three expose
work-copy `list_dir`, `glob`, `grep`, `read_file`, plus `update_plan`
and `web_fetch`. **Plan only** exposes only these six; it has no file
mutation, Git, shell or project-check tools. **Ask before every command**
and **Auto-approve safe commands in the sandbox** also expose
`write_file`, `edit_file`, `delete_file`, `move_file`;
`git_status`, `git_diff`, `git_log`, `git_show`, `git_commit`;
`run_command`, `start_background`, `read_background`,
`stop_background`; and `run_checks`. Each file mutation and
`git_commit` requires its own one-time approval even in auto mode.
File operations validate work-copy-relative paths and enforce size and
read limits; they do not write to the original project.

The shell policy classifies PowerShell and cmd commands as inspection,
build/test, work-copy edits, network operations, dangerous actions or
unknown commands. A command chain uses its most restrictive class.
**Ask before every command** requires one-time approval for every
non-dangerous command that starts a process. **Auto-approve safe commands
in the sandbox** skips command prompts only for recognized inspection
and build/test commands in a verified AppContainer. Edits, network
operations and unknown commands need explicit approval. Dangerous
commands are blocked in both shell-enabled modes.

`run_command` and `start_background` run in the work copy with an
allowlisted environment. The model cannot choose another working
directory or forward host environment values. Network stays disabled
unless separately approved for that command. `run_command` has a
120-second maximum timeout. Its result retains up to the first and
last 4 KiB of output. The live stream is capped at 1 MiB per run.
More than 64 MiB of output kills the process tree. A timed-out,
cancelled or killed command never reports success.

At most two background processes may run at once. `read_background`
returns up to the last 8 KiB of buffered output and process status.
`stop_background` kills the process tree. All remaining processes
are killed at the end of the run.

When work-copy files change, pre-final verification detects pytest,
npm test/lint/build/typecheck scripts, Cargo, Go and .NET checks.
Each check follows the same shell approval and sandbox path, with
network disabled and a 60-second timeout. Failed checks may prompt
up to three repair-and-recheck rounds within the run budget. The
final result distinguishes passed, failed, timed-out and unrun
checks. It also reports unchanged files, missing test commands,
declined approvals and exhausted run budgets.

Git tools allow only local status, diff, log, show and commit operations
against the copy. They disable hooks, filters and external diff drivers.
They do not run network Git commands. Live `web_fetch` requires an
explicit, one-time **Allow once** for the exact requested URL (including
its query) on **every call in every mode**; it never auto-approves, not
even in auto-sandboxed mode. A denied or expired request fetches nothing.
The helper permits public HTTP(S) GETs only, rejects private addresses,
revalidates each redirect, and limits responses to supported text types,
1 MiB, five redirects and ten seconds total. Returned web content is
untrusted reference data, not instructions. Requested `web_fetch` URLs
(including query strings) and raw `web_fetch` arguments are not saved
in the run ledger.

`update_plan` records a bounded task plan, but does not execute it.
Its item text and statuses are persisted as JSON in the run ledger's
plan-step `result.summary` field.
When the latest recorded plan is unfinished, the live-loop final-answer
guard nudges the model once before completion, within the run budget.
Only a recommended per-model capability profile that passes single-call,
ordered-call and prompt-injection-resistance probes can select the
supported native-tools, constrained-JSON or plain-JSON path and narrow
the context budget. Missing, failing or corrupt profiles fall back
to conservative plain-JSON and context defaults. Probes use synthetic
tool responses, not real file or network access.

Tool calls are checked against registered names and argument schemas.
Model output, project text and tool results are data, not permission to
execute code. The run ledger at `agent/runs.sqlite` stores bounded
metadata, not prompts, model output, file bytes or approval details.
Work copies live under `agent_workcopies/`; operational logs use
`logs/cortex.log` and `startup.log`, all in the per-user data directory.
Temporary AppContainer grant journals use a separate per-user
`agent-sandbox` directory.

## Automated checks

CodeQL scans the Python, TypeScript and workflow code on every push and pull
request and on a weekly schedule; Dependabot alerts cover both Python locks
and the frontend lock; a dependency-review job fails a pull request that adds
a dependency with a known vulnerability; and the workflows themselves are
linted with `actionlint` and audited with `zizmor`, with every action pinned
to a commit. Findings are in the repository's Security tab.
