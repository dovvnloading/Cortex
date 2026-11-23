# Cortex user guide

## Local models and downloads

**Ollama.** Start the Ollama service and run `ollama list` to see which models
are available. Cortex uses `http://127.0.0.1:11434` by default. To use an
Ollama server on a trusted network, set `CORTEX_OLLAMA_HOST` before launching
Cortex. Chat requests then travel to that server.

**GGUF.** Open **Settings → System** to select your models folder, enter an
HTTPS download URL, or browse the files in a Hugging Face repository. For a
gated repository, set `HF_TOKEN` in the environment that launches Cortex.
Cortex can resume interrupted downloads and fetch the parts of a split model
together. Use trusted download hosts. Cortex checks for a public IP address
before each request and redirect, but the connection resolves the hostname
again and could receive a different address.

Cortex fetches and verifies its managed llama.cpp runtime on first use. In
**Settings → System** you can choose automatic, Vulkan or CPU operation,
unload a GGUF model, or adjust its idle timeout (30 minutes by default).
Ollama manages its own model lifetime. The `generation.keep_alive_minutes`
setting in the Cortex settings API accepts `0` for the Ollama default, `1` to
`1440` for an explicit duration, or `-1` to keep the model loaded.

Use **Settings → AI Model** for standing instructions, response style and
generation defaults. The composer can override sampling and context length
for one conversation. To translate responses, choose a local model and target
language under **Settings → Translation**. Cortex keeps the original response
for subsequent turns.

## Cloud providers and custom endpoints

1. Open **Settings → AI Model → Cloud providers**. Select a provider to see the
   variables or cloud identity it needs. For example, OpenAI uses
   `OPENAI_API_KEY` and Anthropic uses `ANTHROPIC_API_KEY`.
2. Set the variables for the Windows account running Cortex, then fully quit
   and relaunch the app. For a single session in PowerShell 7.1 or newer, you
   can use `$env:OPENAI_API_KEY = Read-Host "OpenAI API key" -MaskInput`
   before starting Cortex from that shell.
3. Choose **Refresh cloud models** in Settings or **Rescan** in the composer.
   Pick a model your account can use. Provider quotas, billing and retention
   apply to chat requests and chat title generation.

| Configuration | What to set |
| --- | --- |
| OpenAI-compatible endpoint | Set `CORTEX_CUSTOM_BASE_URL` to the full API root, including `/v1` when needed. Set `CORTEX_CUSTOM_API_KEY` if the service requires a key. HTTPS and loopback HTTP are supported. |
| Multiple custom endpoints | Give each endpoint a name, for example `CORTEX_CUSTOM_TEAM_BASE_URL`, `CORTEX_CUSTOM_TEAM_API_KEY` and `CORTEX_CUSTOM_TEAM_MODELS`. |
| Manual model IDs | Set `CORTEX_<PROVIDER>_MODELS` to replace discovery with comma-separated IDs. Use the upper-case provider ID with hyphens replaced by underscores. |
| Azure / Microsoft Foundry | Select the exact deployment name. Set `AZURE_FOUNDRY_PROJECT` for discovery or `AZURE_OPENAI_DEPLOYMENTS` for a comma-separated list. For a manually listed Foundry Claude deployment, use `anthropic/<deployment-name>`. |

## Running Agent on a project

Turn on **Enable Agent mode** under **Settings → General**, save, and open
**Agent mode** in the top bar. Under **Project runs**, enter a goal, select a model
and approve copying eligible files from the project folder for this run. A cloud model also asks to send the
goal and eligible project context to its provider. On a local model’s first
Agent run, Cortex checks how it handles tools and reports any checks it fails.

| Mode | What it allows |
| --- | --- |
| **Plan only** | Search and read the work copy, make a plan and request a public web reference. Each web request needs its own approval. |
| **Ask before every command** | In addition to research, propose edits, use local Git tools and run project checks. File changes, commits and commands that start processes each need approval. |
| **Auto-approve safe commands in the sandbox** | Recognized inspection and build/test commands can proceed automatically in a verified Windows AppContainer. Changes to files, commits and network operations still need approval. |

Review the action and its execution context in each approval dialog. The
**Runs as you** label means the command would run with your account’s access
and requires a fresh decision. The Windows process limits do not impose a disk
quota. You can deny an action or stop the run. [Sandbox and tool limits](../SECURITY.md#agent-mode)

After a run, open **Review staged changes**, expand a file’s diff and select
the files you want. **Accept selected to project** followed by **Confirm
accept** writes just those files after checking their original hashes.
**Discard all** removes the work copy. **Undo** requires its own confirmation
and restores the pre-accept originals if your session, checkpoint and current
file hashes still match. Command side effects and later independent edits fall
outside that recovery.

**Agent run history** replays activity for runs owned by the current session.
The **Saved recovery checkpoints** panel is for the retired Project workspace.
It remains available even with Agent disabled. **Restore originals** and
**Discard backup** each ask for confirmation. The checkpoint journal contains
unencrypted copies of source files, so include it when protecting the local
data directory.

For read-only repository research from PowerShell, the separate CLI accepts
an absolute project path and an explicit grant. It can list and search a
bounded snapshot, or answer questions with a loopback Ollama model.

```powershell
python tools/workspace_research.py --root "C:\src\project" --allow-read-only --list
python tools/workspace_research.py --root "C:\src\project" --allow-read-only --search "class Client"
```

## Data, backups and upgrades

Close Cortex and copy the entire data directory before upgrading. By default
it is `%APPDATA%\ChatLLM\ChatLLM-Assistant`. If the roaming profile is on a
network share, Cortex uses `%LOCALAPPDATA%\ChatLLM\ChatLLM-Assistant`.
New installs keep downloaded runtimes, default GGUF models and the WebView
profile under the local path. An existing cache in the old location stays in
use. Launching with `--data-dir "C:\Cortex\data"` puts everything under the
specified local directory.

| Files in the data directory | Purpose |
| --- | --- |
| `cortex_db.sqlite` and `cortex_settings.sqlite` | Chat history and settings. Their `.bak` and `.bak.1` files hold verified launch backups. |
| `cortex_db.sqlite.pre-v<N>.bak` | Snapshot immediately before a chat database schema upgrade. The schema number identifies which snapshot an older release can open. |
| `<database>.corrupt-<id>` and its sidecars | Files set aside during recovery. Cortex restores from a verified backup and retains the damaged copy. |
| `workspace_checkpoints.sqlite` | Original project source for accepted edits and saved recovery checkpoints. |
| `startup.log` and `logs\cortex.log` | Launch and runtime diagnostics. Review the contents before sharing. |

**To return to an older chat schema**, close Cortex and move the current
`cortex_db.sqlite` and any adjacent `-wal` and `-shm` files into a backup
folder. Copy the matching `cortex_db.sqlite.pre-v<N>.bak` to
`cortex_db.sqlite` in the data directory, then start the older release. Chats
written after the upgrade remain in the files you moved. If multiple snapshots
exist, the newest for that schema keeps the base name and previous versions
use the suffix `.superseded-<n>`.

**If the data drive cannot support SQLite write-ahead logging**, close Cortex,
copy the whole data directory, including any WAL and SHM files, to a local
drive, and launch with `--data-dir` pointing to that copy. An empty destination
starts a fresh workspace. The diagnostics endpoint `GET /api/v1/diagnostics`
reports backup and recovery status.

When migrating legacy JSON history, Cortex moves imported files into
`chat_history_migrated_<time>`, puts unreadable files in
`chat_history/quarantine`, and renames an emptied source directory to
`chat_history.retired`. Cortex also reads legacy permanent memory and Windows
registry settings.

## Launcher and troubleshooting

Run these from the repository root with the virtual environment active, or
replace `python` with `.\.venv\Scripts\python.exe`.

| Command | Use |
| --- | --- |
| `python main.py --dev` | Run the native window with a supervised Vite server for frontend development. |
| `python main.py --headless --port 8765` | Start the loopback backend alone for diagnostics. |
| `python main.py --build-frontend` | Force a frontend rebuild and exit. |
| `python main.py --skip-build-check` | Start with an existing `frontend/dist` bundle. |

- **Empty model picker.** Check `ollama list`, the configured GGUF folder or
  your cloud credentials, then choose **Rescan**. A cloud catalog can list
  models for which your account still needs inference permission.
- **Native window fails to start.** Check `startup.log`, the Python environment
  and Microsoft Edge WebView2 Runtime. For a source install,
  `./packaging/prepare_webview2.ps1` downloads the bootstrapper Cortex uses.
- **Building a Windows package.** From a clean Python 3.11 environment,
  run `./packaging/build_windows.ps1`. It builds and smoke-tests the
  one-folder distribution at `dist/Cortex/Cortex.exe`. Keep the executable
  with the rest of that directory. [Contributor guide](../CONTRIBUTING.md)
