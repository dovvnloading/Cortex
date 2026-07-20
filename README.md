More info: <https://dovvnloading.github.io/Cortex/>

# Cortex

Cortex is a local-first AI assistant for Ollama. The application is a React,
Vite, and TypeScript web UI hosted by a Python FastAPI backend. `main.py`
supervises the complete local runtime: it prepares the frontend, starts the
backend, opens an authenticated loopback browser session, and shuts down owned
processes cleanly.

## Capabilities

- Local chat with configurable Ollama models, streaming responses, reasoning,
  sources, retry, fork, and regeneration.
- Persistent SQLite conversations and atomic JSON permanent memory.
- Validated settings, model inventory and pull progress, translation, and
  follow-up suggestions.
- Loopback-only API authentication with one-time browser handoff tokens.
- Windows one-folder packaging with the frontend bundled and no Node.js or
  system Python required at runtime.

## Architecture

```text
main.py
  ├─ supervised FastAPI backend (Python)
  │    ├─ versioned API, jobs, SSE, authentication
  │    ├─ SQLite/settings/memory repositories
  │    └─ Ollama/model services
  └─ supervised Vite server (development only)

frontend/                 React/Vite/TypeScript UI
backend/cortex_backend/  framework-independent domain and runtime code
assets/                  prompt assets used by the model boundary
packaging/               Windows PyInstaller build
tests/                   headless Python and browser-facing tests
```

The supported runtime is Windows. User data remains in the existing location:
`%APPDATA%\ChatLLM\ChatLLM-Assistant`. Legacy SQLite, JSON chat, permanent
memory, and QSettings data are read without changing their original formats.
Legacy settings are imported additively into SQLite; the original registry
source is left untouched so a verified backup can be used for rollback.

Semantic vector memory is intentionally dormant until retrieval is integrated
end to end; Cortex does not initialize or pull an embedding model at startup.

## Requirements

Runtime:

- Windows 10 or later
- Python 3.10+ for source execution
- Ollama installed and running at `http://127.0.0.1:11434`
- At least one locally installed generation model

Development additionally requires Node.js 22+ and npm. A small local model such
as `nemotron-3-nano:4b` is a good smoke-test default.

## Quick start

Install Ollama from <https://ollama.com/download>, then install a model:

```powershell
ollama pull nemotron-3-nano:4b
```

Install Python dependencies and launch:

```powershell
python -m pip install -r requirements.txt
python main.py
```

The first source launch builds the frontend when needed. For frontend work,
use the supervised development runtime:

```powershell
python main.py --dev
```

Useful options are `--no-browser`, `--port 0`, `--data-dir PATH`,
`--skip-build-check`, and `--build-frontend`.

If Ollama is unavailable, Cortex still starts and presents the local setup and
connection state; generation remains unavailable until the service returns.

## Packaging

Build the production frontend and Windows one-folder executable with:

```powershell
python main.py --build-frontend
powershell -ExecutionPolicy Bypass -File packaging/build_windows.ps1
```

The artifact is produced at `dist/Cortex/Cortex.exe`. The package contains the
frontend and prompt assets and does not need Node.js or a global Python
installation when launched.

## Development

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
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

Contract artifacts are generated from the FastAPI application:

```powershell
python tools/generate_contracts.py
```

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and
[Change_Log.md](Change_Log.md) for project and release guidance.

## Privacy and security

Cortex binds its API to loopback and uses an authenticated, expiring browser
handoff. Prompts, responses, memories, and model output are not written to
diagnostic logs. Ollama remains local unless the user intentionally configures
a different endpoint.

## License

This project is licensed under the terms in [LICENSE](LICENSE).
