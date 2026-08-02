# Cortex

Cortex is a local-first AI workspace for Windows. It runs Ollama models on the
machine, keeps conversations and memory in local storage, and presents the
React/TypeScript interface inside a Python-owned pywebview/WebView2 window.
The normal launcher owns the backend, native window, and any development
frontend process; Cortex does not open the user's installed browser.

## Current workspace

The interface is deliberately small: a thread list, a focused transcript, a
composer with a local model picker, and settings for model, memory, appearance,
and execution controls.

Screenshots are intentionally omitted while the curated capture set is being
finalized. The examples used for the next capture pass are staged locally in an
isolated profile and are not part of the repository.

## What Cortex provides

- **Local chat.** Stream responses from an installed Ollama model with Markdown,
  syntax-highlighted fenced code, reasoning details, sources, code-block copy
  controls, retry/regenerate, and forked threads. Long transcripts render in a
  virtualized list so scroll performance stays smooth regardless of history
  length.
- **Composer model control.** Inspect the local inventory, switch models without
  leaving the composer, refresh the inventory, and stage local image or text
  attachments.
- **Per-chat generation parameters.** Temperature, top-p, top-k, repeat penalty,
  and context window default to the values in Settings but can be overridden
  for a single conversation from the composer, without changing the standing
  default. Each response shows its token count and tokens/sec once generation
  finishes.
- **Model details.** The Models panel shows each installed model's parameter
  size, quantization, and context length alongside its name, read from Ollama's
  existing model-detail response.
- **Keyboard-first navigation.** A command palette (Ctrl/Cmd+K) reaches new
  chat, settings, theme, model switching, and recent chats; `?` opens a
  shortcuts reference. The sidebar also supports searching chats by title.
- **Transcript export.** Any conversation can be exported as Markdown or JSON
  from the workspace header.
- **Durable context.** Threads live in SQLite; permanent memories use atomic
  local JSON storage. Existing JSON chat history and Windows settings are read
  additively during migration without rewriting the legacy source.
- **Bounded local tools.** Explicit arithmetic can use the deterministic scratch
  calculator. User-selected PNG/JPEG/WebP transforms run as fixed recipes in a
  short-lived worker.
- **Approval-gated Python.** A local model may propose a structured Python task,
  but the backend validates it, records the source digest and requested
  capabilities, and waits for one-time user approval. The task tray shows
  pending approval, progress, output, errors, cancellation, and revoke state.
  Ordinary assistant text and fenced code are never executed.
- **Native local runtime.** The API binds to loopback, the native handoff uses
  an expiring session token, and the embedded WebView uses a private Cortex-owned
  profile.

## How local execution is bounded

Code execution is a separate capability from scratch computation:

1. The local model emits a structured request containing Python source, a plain-
   language intent, and the exact filesystem, process, and network capabilities
   it wants.
2. Cortex validates and persists a pending job. A model response, Markdown block,
   or malformed request cannot start a worker.
3. The user chooses **Allow once** or **Deny**. Permissions are not carried into
   the next run.
4. An isolated, short-lived worker applies source, time, memory, output, and
   child-process limits. Host operations go through the brokered `cortex` API;
   environment variables, credentials, and application secrets are not inherited.
5. The task tray records the lifecycle and renders structured output. Stop,
   cancellation, timeout, denial, and revoke invalidate the grant.

Broad filesystem, process, or network access is a clearly labeled high-risk
choice for that run. It is never silently enabled or persisted. If the required
worker boundary cannot be established, Cortex fails closed and ordinary chat
remains available.

## Architecture

```text
main.py
  +-- supervised FastAPI backend (Python)
  |   +-- versioned loopback API, session auth, SSE jobs
  |   +-- SQLite conversations/settings and local memory repositories
  |   +-- Ollama/model and generation services
  |   `-- scratch, image, attachment, and code execution lifecycles
  +-- native pywebview / WebView2 window
  `-- supervised Vite server (development mode only)

frontend/                 React + Vite + TypeScript application
backend/cortex_backend/   API, repositories, services, and worker boundaries
contracts/                generated TypeScript API contracts
assets/                   externalized model prompt assets
packaging/                Windows PyInstaller build and WebView2 bootstrapper
tests/                    Python API, lifecycle, worker, and migration tests
```

The supported source runtime is Windows. User data stays under
`%APPDATA%\ChatLLM\ChatLLM-Assistant` unless an explicit `--data-dir` is supplied.
Semantic vector storage is intentionally dormant until retrieval is integrated
end to end; Cortex does not pull an embedding model at startup.

## Requirements

- Windows 10 or later
- Python 3.10 or later
- Ollama installed and running at `http://127.0.0.1:11434`
- At least one locally installed generation model
- Node.js 22+ and npm for frontend development or source builds

## Quick start

Install a model in Ollama, create a virtual environment, and launch the native
desktop application:

```powershell
ollama pull qwen3:8b
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
```

The first launch checks the local model inventory. Choose a model from the setup
screen or later from the composer picker. If Ollama is unavailable, Cortex still
opens and explains the connection state; generation becomes available after the
service is running and the inventory is refreshed.

Useful launcher options:

```powershell
python main.py --dev                 # supervise Vite while developing the UI
python main.py --headless --port 8765 # backend only, for diagnostics/automation
python main.py --data-dir PATH       # use an isolated local profile
python main.py --skip-build-check    # reuse the existing frontend/dist bundle
python main.py --build-frontend      # build the frontend and exit
```

`--no-browser` is retained as a deprecated alias for `--headless`. The Ollama
endpoint can be intentionally changed for a trusted local network setup with
`CORTEX_OLLAMA_HOST`; the default remains loopback.

## Development checks

From the repository root:

```powershell
python -m pytest -q
python -m compileall -q main.py backend

npm.cmd ci --prefix frontend
npm.cmd run lint --prefix frontend
npm.cmd run typecheck --prefix frontend
npm.cmd test --prefix frontend -- --run
npm.cmd run build --prefix frontend
```

Use `npm.cmd` on Windows when PowerShell execution policy blocks the `npm.ps1`
shim. API contract artifacts are generated with:

```powershell
python tools/generate_contracts.py
```

## Windows packaging

Build the one-folder package with:

```powershell
powershell -ExecutionPolicy Bypass -File packaging/build_windows.ps1
```

The result is written to `dist/Cortex/Cortex.exe`. The package contains the
frontend, prompt assets, Python runtime, pywebview bridge, and the signed
Evergreen WebView2 bootstrapper. A packaged launch does not require Node.js, a
global Python installation, or an installed browser.

## Privacy and security

Cortex keeps the API on loopback and requires an expiring authenticated native
window session. The embedded view uses a private profile and does not inherit
browser cookies, history, extensions, or profiles. Prompts, responses, memories,
and raw model output are excluded from diagnostic logs. Ollama remains local
unless `CORTEX_OLLAMA_HOST` is intentionally configured otherwise.

The code-execution worker is a bounded containment boundary, not a claim of
arbitrary operating-system isolation. Review the exact source and capabilities
before approving a run. See [SECURITY.md](SECURITY.md) for reporting guidance.

## Project documentation

- [Open-source execution plan](docs/OPEN_SOURCE_EXECUTION_PLAN.md)
- [Attachment boundary](docs/CHAT_ATTACHMENTS.md)
- [Execution architecture records](docs/adr/)
- [Contributing guide](CONTRIBUTING.md)
- [Change log](Change_Log.md)
- [Security policy](SECURITY.md)

## License

Cortex is distributed under the terms in [LICENSE](LICENSE).
