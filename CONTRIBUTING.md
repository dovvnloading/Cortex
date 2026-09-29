# Contributing to Cortex

Cortex is a Windows-first, local-first application with a React/Vite frontend
and a Python backend. Contributions should preserve local data compatibility,
loopback-only operation, and a clean launcher lifecycle.

Before changing code, read the repository [agent operating
contract](AGENTS.md). It defines the required inspect -> reproduce -> patch ->
verify -> review workflow, the current bounded-execution boundary, and the
evidence expected in a handoff.

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

Install a small local model for smoke checks; the README's quick start names
one, and any model you already have works too. Do not use real prompts,
responses, memories, or user data in tests or logs.

`requirements.txt` and `requirements-dev.txt` intentionally carry loose
version ranges so your local environment isn't forced onto one exact set of
versions. CI installs from `requirements.lock.txt` /
`requirements-dev.lock.txt` instead -- hash-pinned, fully resolved lock files
for the same Python 3.11 target CI actually runs -- so a change is verified
against the same dependency versions every time. If you edit
`pyproject.toml`'s `dependencies` or `dev` extra, regenerate both locks and
commit the result, or CI's `lint` job will fail on a staleness check:

```powershell
python -m pip install uv
uv pip compile pyproject.toml --python-version 3.11 --generate-hashes -o requirements.lock.txt
uv pip compile pyproject.toml --extra dev --python-version 3.11 --generate-hashes -o requirements-dev.lock.txt
```

## Quality checks

The development requirements pin the same Ruff version used by CI, and the
first thing `check.ps1` does is verify your environment actually has it --
linting with a different Ruff means a green run here and a red one in CI for
no reason the diff explains. If it reports drift, reinstall:

```powershell
python -m pip install -r requirements-dev.lock.txt
```

One script runs the repository's fast quality gates on your machine:

```powershell
./scripts/check.ps1
```

That is the `quick` tier -- environment and lockfile checks, Ruff, the workflow
linters, the `mypy` type check, backend tests, the artifact-boundary review,
contract drift, and frontend types/lint/unit tests. The lockfile check needs
`uv` (`python -m pip install uv`); without it that one step reports `skip`
rather than failing, and CI still enforces it. Before opening a pull request,
run the `full` tier, which adds `compileall`, the Playwright browser
installation and tests, and the bundle build:

```powershell
./scripts/check.ps1 -Tier full
```

Use `-SkipFrontend` or `-SkipBackend` to narrow the run while iterating.

Packaging (PyInstaller) and WebView2 signature verification are deliberately
left out of both tiers: they take 35+ minutes and need Windows packaging
tooling. CI covers them.

### Run the checks automatically before a push

Point Git at the tracked hooks directory once per clone:

```powershell
git config core.hooksPath .githooks
```

The `pre-push` hook then runs the `quick` tier and aborts the push if anything
fails. A push of only tags or branch deletions carries no new code, so the hook
lets it through without running anything. Bypass it in an emergency with
`git push --no-verify` or `CORTEX_SKIP_HOOK=1 git push`.

The individual commands, if you prefer to run them by hand. These are the steps
`check.ps1` runs and CI's `lint`, `backend`, `frontend` and `e2e` jobs run; the
quick tier skips the ones marked `full tier`:

```powershell
python scripts/check_dev_environment.py
python -m ruff check backend tests tools main.py app_factory.py scripts
python -m mypy
python -m pytest -q
python -m coverage run -m pytest -q   # full tier, in place of the line above
python -m coverage report             # full tier: enforces the coverage floor
python tools/artifact_boundary_review.py --json --strict
python tools/generate_contracts.py --check
python -m compileall -q main.py app_factory.py backend   # full tier

Push-Location frontend
npm ci
npm run typecheck
npm run lint
npm test -- --run
npm run test:coverage             # full tier, in place of the line above
npx playwright install chromium   # full tier
npm run e2e -- --workers=1        # full tier
npm run build                     # full tier
Pop-Location
```

When API models change, regenerate and review both contract artifacts:

```powershell
python tools/generate_contracts.py --write
```

### Coverage floors and the CI layout

The Quality workflow runs its gates as parallel jobs -- `lint` (locks, Ruff,
workflow linters, mypy, contract drift), `backend`, `frontend` and `e2e` -- and
packaging (`heavy`) starts only after all four pass. A new push to a pull
request cancels the run it supersedes; a pull request that changes nothing but
`README.md`, `SECURITY.md`, `LICENSE` or `docs/` skips the gates and still ends
green.

Backend coverage is measured with branches over `backend/cortex_backend`
(`[tool.coverage]` in `pyproject.toml`) and the frontend's with Vitest's V8
provider (`frontend/vitest.config.ts`). Both fail when total coverage falls
under a floor, and CI uploads the reports as workflow artifacts. The floors
only go up: raise one in the pull request that adds the tests, and never lower
one to make a change pass.

The other Python versions the project supports are covered by the separate
`Python compatibility` workflow, which runs on every push to `main`, weekly,
and on demand rather than on every pull request.

### Workflow and dependency checks

The GitHub Actions workflows are part of the code and get the same treatment:

- `actionlint` checks them for mistakes (bad expressions, unknown keys, wrong
  runner labels) and `zizmor` audits them for security (unpinned actions,
  template injection, persisted credentials). Both are pinned in the dev lock
  and run in `check.ps1` and in CI's `lint` job. Every `uses:` is pinned to a
  commit with the version as a trailing comment; to bump one, look up the
  commit for the new tag and change both.
- A `dependency-review` job fails a pull request that adds a dependency with
  a known vulnerability.
- CodeQL (GitHub's default setup) scans Python, TypeScript and the workflows
  on every push and pull request and weekly, and Dependabot alerts are on.
  Results appear under the repository's Security tab; neither opens pull
  requests.
- The `Dependency refresh` workflow runs on the first of each month, or on
  demand from the Actions tab. It regenerates both Python locks with
  `uv pip compile --upgrade` and `frontend/package-lock.json` with
  `npm update --package-lock-only`, within the ranges already declared, and
  opens one grouped pull request. A pull request opened by a workflow does
  not start CI on its own: close and reopen it, or push to its branch.

### Bumping the pinned llama.cpp release

Cortex downloads a Windows llama.cpp build on demand and trusts only the
SHA-256 values pinned in `backend/cortex_backend/llamacpp/binary_release.py`
(`CURRENT_RELEASE`), because upstream publishes no checksums. To move to a newer
build:

1. Pick a `bNNNN` release tag from the ggml-org/llama.cpp releases page.
2. Run `python tools/pin_llamacpp_release.py bNNNN`. It downloads the CPU and
   Vulkan Windows archives (roughly 50-150 MB each) and prints a
   `PinnedRelease(...)` literal holding each archive's SHA-256 and the hash of
   its whole extracted directory. Run it deliberately, never from CI: it makes
   real downloads.
3. Paste the literal over the value of `CURRENT_RELEASE` and update the comment
   above it with the new tag and date. The tool does not edit the file.
4. Run `python -m pytest -q tests/test_llamacpp_binary_fetcher.py
   tests/test_llamacpp_server_manager.py`, then load a GGUF model once from a
   source checkout to confirm the new `llama-server` still accepts the flags
   Cortex passes. Nothing checks that automatically today.

## Pull requests

- Keep each pull request limited to one staged architectural concern.
- Do not stage local databases, frontend build output, credentials, or private
  planning files.
- Add focused tests for behavior, persistence compatibility, and safe failure.
- Update the README for user-visible runtime changes, and add a
  `Change_Log.md` entry under `[Unreleased]` for any user-visible change.
- Include a rollback procedure for data or launcher changes.
- Use Conventional Commit subjects, for example
  `fix(storage): preserve legacy chat migration sources`.
- Fill in the pull request template; its headings are the handoff list in
  [AGENTS.md](AGENTS.md).

What the repository enforces, and what it does not: CI runs on every pull
request and every push to `main`, and a change should not be merged until it is
green. That is a convention, not a rule GitHub applies -- `main` has no branch
protection or ruleset, so a failing check does not block a merge. There is no
required reviewer, because Cortex has a single maintainer. Changes are
squash-merged so each pull request becomes one Conventional Commit on `main`;
the repository settings also still allow merge commits and rebase merges, so
that is a convention too.

## Releasing

The version is written once, in `backend/cortex_backend/__init__.py`;
`frontend/package.json` must match it (a test enforces this), and
`python tools/generate_contracts.py --write` carries it into the API contract.

1. Merge a pull request that sets the new version and turns the changelog's
   `[Unreleased]` section into `## [<version>] - <date>`, leaving a fresh empty
   `[Unreleased]` above it.
2. Tag that commit on `main` as `v<version>` and push the tag. The release
   workflow refuses a tag that disagrees with the declared version, builds and
   smoke-tests the Windows package from the tag, and opens a **draft** release
   with an unsigned archive and `SHA256SUMS.txt`.
3. Sign it locally:

   ```powershell
   ./packaging/sign_release.ps1 -Tag v<version> -Upload `
       -AzureCliPath <az.cmd> -DlibPath <Azure.CodeSigning.Dlib.dll> -DotNetRoot <.NET 8>
   ```

   Signing uses Azure Artifact Signing. The account, certificate profile and
   the one allowed signer are read from `packaging/signing.local.json`, which
   is git-ignored: copy `packaging/signing.local.example.json` and fill it in
   on the signing machine. Account details never belong in this repository or
   in a pull request. Azure CLI must be signed in as that dedicated signer; the
   script refuses any other identity, checks the downloaded build against CI's
   checksum, verifies the signature, its publisher and its timestamp, and
   replaces the archive and checksum on the draft.
4. Write the release notes from the changelog, review the draft, and publish.

## Code style

Python should be typed and readable, with safe user-facing errors and no raw
prompt/response logging. TypeScript should use strict typing and accessible
controls. Keep network access explicit and loopback-safe. Avoid adding
framework dependencies to backend domain and repository modules unless the
boundary requires them.

## Security reports

Please use GitHub private vulnerability reporting for security issues rather
than public issues. See [SECURITY.md](SECURITY.md). Findings from CodeQL,
Dependabot and dependency review are in the repository's Security tab.
