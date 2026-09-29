# Cortex

Cortex is a local-first AI workspace for Windows. It runs models on the machine
-- either through Ollama or by loading a local `.gguf` file with its own managed
llama.cpp runtime -- keeps conversations and memory in local storage, and
presents the React/TypeScript interface inside a Python-owned pywebview/WebView2
window. The normal launcher owns the backend, native window, and any development
frontend process; Cortex does not open the user's installed browser.

## The workspace

The interface is deliberately small: a chat library, a focused transcript, a
composer with a local model picker, and settings for model, memory, appearance,
and execution controls.

![The Cortex workspace: a chat library filed into Engineering, Research, and
Decisions groups on the left, a transcript rendering Markdown and a
syntax-highlighted Python block with its own copy control, and the composer with
its model picker below.](.github/images/workspace.png)

Every response carries its own footer -- timestamp, token count, and tokens per
second -- with copy, regenerate, and fork controls that appear on hover.

Reasoning and sources stay out of the answer body until asked for, each as its
own disclosure beneath the response:

![A Cortex answer with both disclosures open: a Sources list citing three
papers, and a Reasoning panel showing the model's working behind the
answer.](.github/images/reasoning-and-sources.png)

### Models and runtimes

Cortex lists models installed through Ollama alongside any `.gguf` files in the
local models folder, each with the parameter size, quantization, and context
length read from the model itself. A GGUF can be fetched by Hugging Face repo id
or direct URL, then selected from the same picker as everything else:

![Cortex settings, System section: Ollama shown as connected above the installed
model inventory, each model listed with its parameter size, quantization, and
context length, and a GGUF download form
beneath.](.github/images/models-and-runtimes.png)

Generation defaults live in one place, and the chat model doubles as the title
model so there is only one choice to make:

![Cortex settings, AI Model section: the chat model picker showing each model's
source, size, and quantization, above the generation defaults -- Precise,
Balanced, and Creative response styles, sliders with an editable exact value,
and one-tap context window sizes from 2K to 64K.](.github/images/settings.png)

Temperature, top-p, top-k, repeat penalty, and context window can also be
overridden for a single conversation from the composer, without disturbing these
standing defaults.

### Keyboard-first

`Ctrl`/`Cmd`+`K` opens a command palette that reaches new chat, settings, theme,
model switching, and recent conversations without leaving the keyboard:

![The Cortex command palette open over the workspace, listing chat actions, the
installed models to switch between with the current one marked, and recent
chats.](.github/images/command-palette.png)

The whole workspace follows the system theme, or can be pinned light or dark:

![The Cortex workspace in light theme, showing the same chat library and
transcript.](.github/images/workspace-light.png)

> The application in these images is the real build. The conversations, library,
> memories, and staged tasks are a fixed demo workspace defined in
> `tools/screenshots/showcase_server.py` -- no model is contacted during a
> capture, so the images regenerate byte for byte with
> `./tools/screenshots/showcase.ps1`.

## What Cortex provides

- **Local chat.** Stream responses from a local model with Markdown,
  syntax-highlighted fenced code, reasoning details, sources, code-block copy
  controls, retry/regenerate, and forked threads. Long transcripts render in a
  virtualized list so scroll performance stays smooth regardless of history
  length.
- **Two local runtimes.** Use models installed through Ollama, or point Cortex at
  a folder of `.gguf` files and it serves them through its own managed llama.cpp
  runtime -- no separate install. The `llama-server` binary is fetched once,
  verified against a pinned SHA-256, and cached. GPU backend selection is
  `auto` (try Vulkan, fall back to CPU), `vulkan`, or `cpu`.
- **Bring your own GGUF.** Download a model into the local folder by direct URL
  or Hugging Face repo, then select it from the same picker as everything else.
  The folder is searched a few levels deep, so the one-folder-per-repository
  layout downloaders use works as-is; a file appears as `gguf:<path>`, e.g.
  `gguf:Qwen3-8B-GGUF/Qwen3-8B-Q4_K_M.gguf`. A download that drops resumes from
  the bytes already stored, a split model (`-00001-of-00003`) is fetched whole or
  not at all, and a gated Hugging Face repository works once its access token is
  in the `HF_TOKEN` environment variable (it is sent to huggingface.co only,
  never stored or logged). Links must be `https://` and must resolve to public
  addresses; that is checked before every request and redirect. One limit: the
  check looks the host name up itself and the connection then looks it up again,
  so a DNS server that answers differently the second time (DNS rebinding) is not
  stopped by the check alone. TLS still applies, because the connection is
  verified against the requested host name and no request is sent before that
  succeeds, but Cortex does not pin the connection to the address it checked, so
  only download from hosts you trust.
- **Composer model control.** Inspect the local inventory, switch models without
  leaving the composer, refresh the inventory, and stage local image or text
  attachments.
- **Per-chat generation parameters.** Temperature, top-p, top-k, repeat penalty,
  and context window default to the values in Settings but can be overridden
  for a single conversation from the composer, without changing the standing
  default. Each response shows its token count and tokens/sec once generation
  finishes.
- **Prompt control.** Standing system instructions apply to every turn, with no
  length cap. A local model can also be run raw: **Bypass Cortex's default
  system prompt** leaves the built-in identity and safety instructions out of
  the request entirely. It is off by default and takes a deliberate opt-in.
- **Model details.** The Models panel shows each installed model's parameter
  size, quantization, and context length alongside its name, read from Ollama's
  existing model-detail response.
- **Keyboard-first navigation.** A command palette (Ctrl/Cmd+K) reaches new
  chat, settings, theme, model switching, and recent chats; `?` opens a
  shortcuts reference. The sidebar also supports searching chats by title.
- **Durable context.** Threads live in SQLite; permanent memories use atomic
  local JSON storage. Existing JSON chat history and Windows settings are read
  additively during migration without rewriting the legacy source.
- **Exact arithmetic.** An explicit calculation ("what is 17.5% of 2,340?") is
  checked by a deterministic local calculator before the model answers, so the
  figure in the reply is computed rather than guessed.
- **Approval-gated Python.** A local model may propose a task in a small,
  restricted subset of Python (no imports, no `def`/`class`/`while`, no method
  or attribute calls -- assignment, bounded loops, comprehensions, and a fixed
  builtin list), and the backend validates it, records the source digest and
  requested capabilities, and waits for one-time user approval. The task tray
  shows pending approval, progress, output, errors, cancellation, and revoke
  state. Ordinary assistant text and fenced code are never executed.
- **Optional response translation.** A configured local model can translate
  each response into a target language, off by default and set independently
  of the chat model.
- **Native local runtime.** The API binds to loopback, the native handoff uses
  an expiring session token, and the embedded WebView uses a private Cortex-owned
  profile.

## How local execution is bounded

When a model proposes a task, the transcript states what it wants and the exact
access it is asking for, and the task parks in the tray until the user decides.
Nothing runs in the meantime:

![A Cortex transcript where the model has proposed a short Python calculation
for the monthly payment and total interest on a car loan: it shows the exact
source it wants to run and states that it asks for no file, process, or network
access. The task activity tray on the right reads "Review before running" with
Allow once and Deny controls.](.github/images/execution-approval.png)

The tray names the requested host access, flags broad access as a high-risk
choice for that run, and lets the generated source be inspected before anything
executes. A finished task keeps its captured output alongside it:

![The Cortex task activity tray: the pending loan calculation marked "No host
access requested", with a prompt to review the generated source and Allow once
and Deny buttons; below it a completed task showing its captured
output.](.github/images/execution-tray.png)

Both tasks in these images pass the real validator, and the output shown is
what the restricted interpreter prints for that source
(`tests/test_showcase_fixtures.py` holds the demo to that).

Code execution is a separate capability from scratch computation:

1. The local model emits a structured request containing Python source, a plain-
   language intent, and the exact filesystem, process, and network capabilities
   it wants.
2. Cortex validates and persists a pending job. A model response, Markdown block,
   or malformed request cannot start a worker.
3. The user chooses **Allow once** or **Deny**. Permissions are not carried into
   the next run.
4. An isolated, short-lived worker applies source, time, memory, output, and
   child-process limits. Host operations go through the brokered `cortex` API.
   The worker is started with Cortex's own environment and clears the
   environment variables it inherited (keeping only the Windows system root)
   before it runs any source or makes any brokered call; the network broker
   also ignores proxy settings. That is a clearing step inside the worker, not
   a launch with a clean environment.
5. The task tray records the lifecycle and renders structured output. Stop,
   cancellation, timeout, denial, and revoke invalidate the grant.

Broad filesystem, process, or network access is a clearly labeled high-risk
choice for that run. It is never silently enabled or persisted. If the required
worker boundary cannot be established, Cortex fails closed and ordinary chat
remains available.

The calculator and the image transform run the same way as code: a short-lived
child that clears its inherited environment and then waits at a checkpoint,
touching nothing it was given, until Cortex has put it in a Windows job object.
The job ends the child if Cortex exits, caps the memory the child can commit,
allows the calculator and image workers exactly one process (the code worker a
handful), and blocks clipboard, desktop and other-window access. That is a
resource and lifetime boundary, not a sandbox: a job does not limit which files
or network addresses a process running as you could otherwise reach.

What the capabilities reach today is deliberately narrow:

- **Files:** a scratch folder created empty for that one run and deleted
  afterwards. A task cannot read your documents or your projects.
- **Processes:** refused. Starting another program is not available until
  Windows can enforce the same boundary the broker promises.
- **Network:** a small number of HTTP GET requests to public addresses; private,
  local, and carrier-NAT ranges are refused.

The language itself is a restricted Python subset, so this suits calculations
over data the model writes into the program -- not editing files or running
your tests. A consent-gated workspace mode for that kind of work is the
project's stated direction, and it is not built yet.

## Architecture

```text
main.py
  +-- supervised FastAPI backend (Python)
  |   +-- versioned loopback API, session auth, SSE jobs
  |   +-- SQLite conversations/settings and local memory repositories
  |   +-- model and generation services (Ollama + managed llama.cpp)
  |   `-- scratch, image, attachment, and code execution lifecycles
  +-- native pywebview / WebView2 window
  `-- supervised Vite server (development mode only)

frontend/                 React + Vite + TypeScript application
backend/cortex_backend/   API, repositories, services, and worker boundaries
contracts/                generated TypeScript API contracts
assets/                   externalized model prompt assets
packaging/                Windows PyInstaller build and WebView2 bootstrapper
tools/                    contract generation, artifact review, screenshots
tests/                    Python API, lifecycle, worker, and migration tests
```

The supported source runtime is Windows. User data stays under
`%APPDATA%\ChatLLM\ChatLLM-Assistant` unless an explicit `--data-dir` is supplied.
The large, re-downloadable folders a new install creates -- the llama.cpp runtime,
the default GGUF models folder and the WebView profile -- go under
`%LOCALAPPDATA%\ChatLLM\ChatLLM-Assistant` instead, so a roaming profile does not
synchronise gigabytes of them. A folder that already exists in the data directory
stays where it is and keeps being used there; Cortex does not move or copy an
existing one to the new location. With
`--data-dir` everything stays together under that folder.
Cortex does not pull an embedding model at startup and has no semantic retrieval.

## Requirements

- Windows 10 or later
- Python 3.10 or later
- At least one local model, from either runtime:
  - Ollama installed and running at `http://127.0.0.1:11434`, or
  - a `.gguf` file in the local models folder, served by the managed llama.cpp
    runtime (no Ollama required)
- Node.js 22+ and npm for frontend development or source builds

## Quick start

Get a model, create a virtual environment, and launch the native desktop
application:

```powershell
ollama pull qwen3:8b
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
```

If you would rather not run Ollama, skip the first line and instead drop a
`.gguf` file into the local models folder shown under **Settings -> System**, or
download one from there by URL or Hugging Face repo. Cortex serves it with its
own llama.cpp runtime.

The first launch checks the local model inventory. Choose a model from the
composer's model picker, or under **Settings -> AI Model**. If neither runtime
has a usable model, Cortex still opens and explains the connection state;
generation becomes available once a model is present and the inventory is
refreshed.

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

The local check script covers the fast gates CI runs, from the repository root.
For contributor setup, install `requirements-dev.txt` as described in
`CONTRIBUTING.md` so the local Ruff version matches CI:

```powershell
./scripts/check.ps1              # fast gates, including the artifact-boundary review
./scripts/check.ps1 -Tier full   # adds compileall, Playwright, and the bundle build
```

For coding-agent work, read the repository-level [agent operating
contract](AGENTS.md) first. Cortex's current product boundary is bounded local
execution: approval-gated code in a restricted Python subset, safe computation,
and fixed-function image recipes. Nothing here enables arbitrary workspace or
shell authority.

To run them automatically before every push:

```powershell
git config core.hooksPath .githooks
```

Use `npm.cmd` on Windows when PowerShell execution policy blocks the `npm.ps1`
shim. API contract artifacts are generated with:

```powershell
python tools/generate_contracts.py --write
```

The README screenshots are regenerated from a fixed demo workspace, so they stay
in step with the UI without hand-editing images. `showcase.ps1` writes the full
showcase set (to the Desktop by default, or `-OutDir`); the images used above are
a curated subset copied into `.github/images/`:

```powershell
npm.cmd run build --prefix frontend
./tools/screenshots/showcase.ps1
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

## Upgrading and recovery

Back up `%APPDATA%\ChatLLM\ChatLLM-Assistant` before installing a new release.
Cortex keeps reading legacy SQLite, JSON chat, permanent-memory, and Windows
registry settings, and both the chat and settings databases carry verified
backups. Those backups and the untouched legacy settings source are the
rollback path; an untested in-place downgrade is not.

What is kept, all beside the database files in that folder:

- `cortex_db.sqlite.bak` and `.bak.1` (and the same for
  `cortex_settings.sqlite`) are the two newest verified backups. They are
  refreshed each launch, after any schema upgrade, so they hold the new schema.
  The chat database's `.bak` is checked before it takes the `.bak.1` slot, and
  one that fails the check is replaced instead, so a backup that has rotted
  never pushes the older good copy out. If a backup cannot be written (a full
  disk, a file held by another program) Cortex still starts and logs the
  failure. The failure is also reported by the diagnostics API
  (`GET /api/v1/diagnostics`); the app does not show it yet.
- `cortex_db.sqlite.pre-v<N>.bak` is a snapshot of the chat database taken
  just before a release upgraded it from schema version `N`, and it is the file
  an older release can open. The upgrade does not run without it: if Cortex
  cannot write the snapshot (a full disk, a file held by another program) it
  refuses to upgrade, changes nothing, and tries again on the next launch. The
  name always holds the newest snapshot for that version. One that already
  exists and no longer matches the database (a rollback followed by more chats
  on the older release, then a second upgrade) is kept as
  `pre-v<N>.bak.superseded-<n>`. Cortex never deletes any of these.
- If the chat or settings database is found corrupt at launch, Cortex restores
  the newest verified backup and keeps what it replaced as
  `<database>.corrupt-<id>`, with its write-ahead log beside it as
  `<database>.corrupt-<id>-wal`. The restored data is the backup's, which is
  the state at the previous launch; the diagnostics API
  (`GET /api/v1/diagnostics`, `chat_backup` and `settings_backup`) names the
  quarantined file. Cortex never deletes it. If Cortex is stopped part-way
  through a recovery, the next launch finishes it from the newest verified
  backup, and any write-ahead log the earlier attempt had set aside under a
  different name is moved beside the quarantined file (the diagnostics list it
  as `adopted_sidecars`). The diagnostics are API-only for now: nothing in the
  app reads them yet. A database that is simply missing beside a
  `.corrupt-<id>` file and a valid backup looks like exactly that interrupted
  recovery and is restored, so to start over on purpose remove the backups and
  the `.corrupt-<id>` files along with it (they hold copies of your chats).
- Deleting chats frees space inside `cortex_db.sqlite` but not on disk. At
  launch, once that launch's backup has been written and verified, Cortex hands
  the space back when more than a quarter of the file is free. A database
  created by this release does it in small steps; an older one is rewritten
  once (which needs roughly twice its size free on the data drive and the
  temporary-files drive, and is skipped when there is not, when the database
  holds over 1 GiB, or when it takes over 20 seconds). A skipped or interrupted
  pass changes nothing and is tried again on the next launch.
- Chats imported from the old JSON `chat_history` folder are moved to
  `chat_history_migrated_<time>` and files that could not be read to
  `chat_history/quarantine`. The `chat_history` folder itself is removed only
  once it is completely empty; anything still in it is left alone.

Cortex needs write-ahead logging, so the data directory has to be on a local
drive SQLite can use it on; some network, cloud-synced and removable drives
cannot (a folder on a network share is one; when `%APPDATA%` itself is a network
path, Cortex uses `%LOCALAPPDATA%` for its data instead of refusing to start). If SQLite
reports that it could not enable it, Cortex stops at startup with a message
saying so rather than running with weaker durability. Your existing databases
are not deleted or modified when it does, and your chats and settings are still
in the data folder. To keep using them, first copy the existing data files (the
whole folder, including the databases and any `-wal` and `-shm` files beside
them) into a folder on a local drive, then start Cortex with `--data-dir`
pointing at that folder. A `--data-dir` folder that starts out empty holds no
chats.

To go back to an older release after a newer one upgraded the chat database:
close Cortex, move `cortex_db.sqlite` and any `-wal` and `-shm` files beside it
into a folder of your own, copy `cortex_db.sqlite.pre-v<N>.bak` to
`cortex_db.sqlite`, and start the older release. Anything written after the
upgrade is only in the files you moved aside. If you upgrade again later, that
upgrade takes a fresh snapshot, so the next rollback goes back to that one and
the earlier snapshot stays beside it as `.superseded-<n>`. An older release that
meets a newer database refuses it without changing it and names the snapshot to
restore.

## Troubleshooting

- If Cortex reports Ollama unavailable, verify the Ollama service and endpoint.
- If no models appear, run `ollama list` and install a generation model.
- The window opens on a short "Starting Cortex" page while the backend starts,
  and shows what went wrong (and where the log is) if startup fails.
- If the native window does not open from source, reinstall `requirements.txt`
  and verify that the Microsoft Edge WebView2 Runtime is installed. When it is
  missing Cortex asks before installing it (Cancel closes Cortex without an
  error); offline, or if the installer fails, the message names Microsoft's
  download link. From source, `packaging/prepare_webview2.ps1` fetches the
  installer Cortex looks for.
- Two logs live in the data folder: `startup.log` (startup failures, one line per
  successful start, and the previous file as `startup.log.1`) and
  `logs\cortex.log` (what the backend and launcher log, rotating at 1 MiB with
  three backups; `--log-level` sets its verbosity). Both are redacted for
  credential-like text, and a traceback in `cortex.log` keeps only its frames
  and exception class names, never an exception message. Read them before
  sharing anyway.
- If a previous Cortex instance is already running, launching Cortex again
  restores its native window rather than starting a second server. If that
  instance is still starting, the second launch waits up to 90 seconds for its
  window instead of reporting an error.

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

- [Agent operating contract](AGENTS.md)
- [Contributing guide](CONTRIBUTING.md)
- [Change log](Change_Log.md)
- [Security policy](SECURITY.md)

## License

Cortex is distributed under the terms in [LICENSE](LICENSE).
