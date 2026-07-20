# Contributing to Cortex

Cortex is a Windows-first, local-first application with a React/Vite frontend
and a Python backend. Contributions should preserve local data compatibility,
loopback-only operation, and a clean launcher lifecycle.

## Development setup

Install Git, Python 3.10+, Node.js 22+, npm, and Ollama. Then:

```powershell
git clone https://github.com/dovvnloading/Cortex.git
cd Cortex
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py --dev
```

Install a small local model for smoke checks, for example
`nemotron-3-nano:4b`. Do not use real prompts, responses, memories, or user
data in tests or logs.

## Quality checks

Run the relevant checks before opening a pull request:

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
python tools/generate_contracts.py
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
