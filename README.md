More info -> https://dovvnloading.github.io/Cortex/

# Cortex

<img width="1920" height="1032" alt="501164220-d5817af5-db5d-4f4b-96c0-7a455e234b49" src="https://github.com/user-attachments/assets/8b80241a-3842-42c4-8c38-ea0e197ab303" />


Cortex is a desktop AI assistant for running local large language models through Ollama, with a native PySide6 interface and persistent local data storage.

The project is focused on local-first operation: conversation processing, memory, translation, and chat state all run on your machine.

## Table of Contents
- [Overview](#overview)
- [Core Capabilities](#core-capabilities)
- [System Architecture](#system-architecture)
- [Repository Layout](#repository-layout)
- [Requirements](#requirements)
- [Quick Start](#quick-start)
- [Preview Web UI](#preview-web-ui)
- [Configuration and Runtime Behavior](#configuration-and-runtime-behavior)
- [Data and Persistence](#data-and-persistence)
- [Troubleshooting](#troubleshooting)
- [Security and Privacy Notes](#security-and-privacy-notes)
- [Development](#development)
- [License](#license)

## Overview

Cortex combines:
- A Qt-based desktop application (`PySide6`) for a native UI.
- Ollama-backed model orchestration for chat, title generation, and translation.
- Persistent conversation and memo memory systems backed by SQLite and atomic JSON writes.
- Multi-threaded workers to keep the UI responsive during long-running model operations.

## Core Capabilities

- **Local chat with Ollama models**: configurable generation model, host, and generation parameters.
- **Threaded conversation management**: new chat creation, title generation, and chat history handling.
- **Translation pipeline**: optional post-generation translation using a dedicated model.
- **Suggestion generation**: optional context-aware follow-up suggestions.
- **Permanent memo memory**: persistent user/project memory used to improve response relevance.
- **Theme and UX controls**: light/dark theme support and UI state persisted via `QSettings`.

## System Architecture

Cortex is organized around three layers:

1. **Presentation Layer**
   - Built with PySide6 widgets and custom UI components.
   - Main window and dialogs manage chat, settings, memory controls, and translation/suggestion toggles.

2. **Orchestration Layer**
   - The `Orchestrator` in `Chat_LLM.py` coordinates model calls, thread lifecycle, and feature toggles.
   - Worker objects and `QThread` usage isolate blocking operations (query execution, title generation, update checks, model connection checks).

3. **Data + Model Layer**
   - Ollama client interaction for inference.
   - Persistent storage for chat records and permanent memory data.
   - Prompt-building and synthesis logic in the synthesis agent.

## Repository Layout

```text
.
├── Chat_LLM/
│   ├── assets/                  # Icons and prompt assets
│   └── Chat_LLM/
│       ├── Chat_LLM.py          # Main application entry point + orchestrator
│       ├── main_window.py       # Primary UI window
│       ├── synthesis_agent.py   # Prompting + generation/translation/suggestions
│       ├── memory.py            # Memory/database managers
│       ├── ui_*.py              # UI components/styles/dialogs
│       └── ...
├── Cortex_Startup.py            # Startup utility for Ollama setup/model pulling
├── requirements.txt             # Root Python dependencies
├── index.html                   # Landing page
└── README.md
```

The runnable desktop source is under `Chat_LLM/Chat_LLM/`, tests are under `tests/`, and `pyproject.toml` contains the test configuration.

## Requirements

### Runtime
- Python 3.10+
- Ollama installed and running (default host: `http://127.0.0.1:11434`)

### Python Dependencies
Install from the repository root:

```bash
pip install -r requirements.txt
```

Root dependencies currently include:
- `PySide6`
- `markdown`
- `ollama`
- `pytest`

## Quick Start

### 1) Install Ollama
Install Ollama for your platform from the official site:
- <https://ollama.com/download>

### 2) Pull at least one chat model
Example:

```bash
ollama pull qwen3:8b
```

Optional models used by advanced features:

```bash
# Chat title generation
ollama pull granite4:tiny-h

# Translation
ollama pull translategemma:4b

```

### 3) Launch Cortex
From repository root:

```bash
python Chat_LLM/Chat_LLM/Chat_LLM.py
```

### 4) Optional: run the setup utility
The startup utility can help install/pull models with a GUI workflow:

```bash
python Cortex_Startup.py
```

## Preview Web UI

The staged React/Vite web UI is opt-in while the native Qt launcher remains
the default. From the repository root, build the frontend once and start the
local preview backend:

```bash
cd frontend
npm ci
npm run build
cd ..
python Cortex_Preview.py
```

Open the printed loopback URL and enter the one-time bootstrap token printed
by the preview process. The preview exposes validated settings, permanent
memory management, Ollama connectivity, installed model tags, exact model
pull progress, diagnostics, and the streamed chat workflow. On the first
preview settings read, legacy QSettings are imported into additive SQLite
settings tables; the legacy QSettings source is not rewritten.

## Configuration and Runtime Behavior

Default runtime configuration is defined in `Chat_LLM/Chat_LLM/Chat_LLM.py` (`CONFIG` dictionary), including:
- Ollama host URL
- default generation/title/translation models
- generation parameters (`temperature`, `num_ctx`, `seed`)
- available chat model list
- update check URL

User-specific settings (theme, feature toggles, selected models, and related UI preferences) are persisted with `QSettings`.

## Data and Persistence

Cortex uses local persistence for conversation state and memory systems. In practice, this includes:
- chat/thread records and related metadata
- permanent memo-style memory for personalization
- local user settings via `QSettings`

Semantic vector memory is intentionally disabled until its retrieval path is integrated end to end; Cortex does not initialize or pull an embedding model at startup.

## Troubleshooting

### Ollama connection errors
- Verify Ollama is installed and running.
- Confirm the host in configuration/settings matches your local Ollama endpoint.

### No response or slow response
- Ensure your selected model exists locally (`ollama list`).
- Reduce model size if hardware resources are limited.
- Check RAM/CPU/GPU load while generating.

### Missing model errors for optional features
- Pull the required specialized model (translation/title) or disable that feature in settings.

### UI startup issues
- Run from a terminal to inspect logs.
- Confirm Python dependencies are installed in the active environment.

## Security and Privacy Notes

Cortex is designed for local usage, but your privacy posture still depends on local environment configuration:
- Keep Ollama bound to local interfaces unless remote access is intentionally configured.
- Review any custom model endpoints before use.
- Protect your local machine and account, since all data is stored locally.

## Development

From the repository root, create a virtual environment and install the bounded development dependencies:

```bash
python -m venv .venv

# Windows PowerShell
.venv\Scripts\Activate.ps1

python -m pip install -r requirements.txt
```

Run the headless test suite and compile check before opening a pull request:

```bash
python -m pytest
python -m compileall -q Chat_LLM/Chat_LLM
```

Launch the Windows desktop application with:

```bash
python Chat_LLM/Chat_LLM/Chat_LLM.py
```

Contribution process: see `CONTRIBUTING.md`. Security disclosures: see `SECURITY.md`. Project history: see `Change_Log.md`.

## License

This project is licensed under the terms in `LICENSE`.
