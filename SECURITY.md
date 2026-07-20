# Security Policy

Cortex is designed to keep conversations, memories, and model execution local.
The Python API binds to loopback and requires an expiring authenticated native-window
session. Prompts, responses, memories, and raw model output are excluded from
diagnostic logging.

## Supported versions

Security fixes are provided for the latest `main` release and the immediately
preceding stable release. Upgrade before reporting an issue when possible.

## Reporting a vulnerability

Please do not disclose security issues in public GitHub issues. Use GitHub's
[private vulnerability reporting](https://github.com/dovvnloading/Cortex/security)
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
  Windows account and back up this directory before upgrades.
- External links and rendered model content are validated by the frontend.
- Model-produced memory actions are validated and destructive clears require
  explicit user confirmation.

Dependency or packaging concerns that could affect these boundaries should be
reported privately as well.
