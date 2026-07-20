# Cortex Windows Quick Setup

Cortex runs as a local web application hosted by its Python backend and rendered
inside its own native pywebview window. Cortex does not launch the interface in
the user's browser or reuse a browser profile; chat data and model calls remain
on the machine.

## 1. Install Ollama

Install Ollama from <https://ollama.com/download> and leave its local service
running. Cortex expects `http://127.0.0.1:11434` by default.

## 2. Install a small model

In PowerShell:

```powershell
ollama pull nemotron-3-nano:4b
ollama list
```

Larger models can be selected later from Cortex settings. Optional title and
translation models are not required for the initial smoke test.

## 3. Run from source

```powershell
python -m pip install -r requirements.txt
python main.py
```

Cortex builds the frontend if necessary, starts its loopback backend, opens a
native desktop window, and owns shutdown of the processes it started. Use
`python main.py --headless` only for backend diagnostics or automation.

## 4. Windows package

The one-folder package can be built from a development checkout:

```powershell
powershell -ExecutionPolicy Bypass -File packaging/build_windows.ps1
```

Launch `dist\Cortex\Cortex.exe`. The packaged application includes the web
bundle, Python runtime, pywebview bridge, and a signed Microsoft WebView2
bootstrapper. It does not require Node.js, a global Python installation, or an
installed browser. If WebView2 is missing, the packaged bootstrapper installs
the Evergreen Runtime before Cortex opens; that one-time path requires internet
access.

## Data and recovery

Existing data stays under:

```text
%APPDATA%\ChatLLM\ChatLLM-Assistant
```

Before installing a new release, back up that directory. Cortex preserves
legacy SQLite, JSON chat, permanent-memory, and Windows registry settings. A
verified backup and the unchanged legacy settings source are the rollback path;
do not attempt an untested in-place downgrade.

## Troubleshooting

- If Cortex reports Ollama unavailable, verify the Ollama service and endpoint.
- If no models appear, run `ollama list` and install a generation model.
- If the native window does not open from source, reinstall `requirements.txt`
  and verify that Microsoft Edge WebView2 Runtime is installed.
- If a previous Cortex instance is already running, launching Cortex again
  restores its native window rather than starting a second server.
