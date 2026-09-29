# Security Policy

Cortex is designed to keep conversations, memories, and model execution local.
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
  Windows account and back up this directory before upgrades.
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

## Automated checks

CodeQL scans the Python, TypeScript and workflow code on every push and pull
request and on a weekly schedule; Dependabot alerts cover both Python locks
and the frontend lock; a dependency-review job fails a pull request that adds
a dependency with a known vulnerability; and the workflows themselves are
linted with `actionlint` and audited with `zizmor`, with every action pinned
to a commit. Findings are in the repository's Security tab.
