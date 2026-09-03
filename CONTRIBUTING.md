# Contributing to Cortex

Cortex is a Windows-first, local-first application with a React/Vite frontend
and a Python backend. Contributions should preserve local data compatibility,
loopback-only operation, and a clean launcher lifecycle.

Before changing code, read the repository [agent operating
contract](AGENTS.md). It defines the required inspect -> reproduce -> patch ->
verify -> review workflow, the current bounded-execution boundary, and the
evidence expected in a handoff. The researched [local coding-agent harness
standard](docs/LOCAL_CODING_AGENT_HARNESS_STANDARD.md) is the reference for
future workspace-agent capabilities.

## Development setup

Install Git, Python 3.10+, Node.js 22+, npm, and Ollama. Then:

```powershell
git clone https://github.com/dovvnloading/Cortex.git
cd Cortex
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python main.py --dev
```

Install a small local model for smoke checks, for example
`nemotron-3-nano:4b`. Do not use real prompts, responses, memories, or user
data in tests or logs.

`requirements.txt` and `requirements-dev.txt` intentionally carry loose
version ranges so your local environment isn't forced onto one exact set of
versions. CI installs from `requirements.lock.txt` /
`requirements-dev.lock.txt` instead -- hash-pinned, fully resolved lock files
for the same Python 3.11 target CI actually runs -- so a change is verified
against the same dependency versions every time. If you edit
`pyproject.toml`'s `dependencies` or `dev` extra, regenerate both locks and
commit the result, or CI's `fast` job will fail on a staleness check:

```powershell
python -m pip install uv
uv pip compile pyproject.toml --python-version 3.11 --generate-hashes -o requirements.lock.txt
uv pip compile pyproject.toml --extra dev --python-version 3.11 --generate-hashes -o requirements-dev.lock.txt
```

## Quality checks

The development requirements pin the same Ruff version used by CI. One script
runs the repository's fast quality gates on your machine:

```powershell
./scripts/check.ps1
```

That is the `quick` tier -- lint, backend tests, contract drift,
security/watchdog qualification, and frontend types/lint/unit tests. Before
opening a pull request, run the `full` tier, which adds `compileall`, the
Playwright browser installation and tests, and the bundle build:

```powershell
./scripts/check.ps1 -Tier full
```

Use `-SkipFrontend` or `-SkipBackend` to narrow the run while iterating.

Packaging (PyInstaller), the recipe-worker and coordinator qualification
spikes, and WebView2 signature verification are deliberately left out of both
tiers: they take 35+ minutes and need signing tooling. CI covers them.

### Run the checks automatically before a push

Point Git at the tracked hooks directory once per clone:

```powershell
git config core.hooksPath .githooks
```

The `pre-push` hook then runs the `quick` tier and aborts the push if anything
fails. Bypass it in an emergency with `git push --no-verify`.

The individual commands, if you prefer to run them by hand:

```powershell
python -m pytest
python -m compileall -q main.py backend

Push-Location frontend
npm ci
npm run typecheck
npm run lint
npm test -- --run
npm run build
Pop-Location
```

When API models change, regenerate and review both contract artifacts:

```powershell
python tools/generate_contracts.py --write
```

## Pull requests

- Keep each pull request limited to one staged architectural concern.
- Do not stage local databases, frontend build output, credentials, or private
  planning files.
- Add focused tests for behavior, persistence compatibility, and safe failure.
- Update the README and changelog for user-visible runtime changes.
- Include a rollback procedure for data or launcher changes.
- Use Conventional Commit subjects, for example
  `fix(storage): preserve legacy chat migration sources`.

The repository workflow uses draft pull requests, required CI, review before
ready status, and squash merges into `main`.

## Code style

Python should be typed and readable, with safe user-facing errors and no raw
prompt/response logging. TypeScript should use strict typing and accessible
controls. Keep network access explicit and loopback-safe. Avoid adding
framework dependencies to backend domain and repository modules unless the
boundary requires them.

## Security reports

Please use GitHub private vulnerability reporting for security issues rather
than public issues. See [SECURITY.md](SECURITY.md).
