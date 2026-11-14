# Protect `main` with the Cortex Quality checks

> **Important:** A JSON file committed to the repository does **not** activate a GitHub ruleset.
> Repository administration access is required to apply it through GitHub's Settings or API.
> Issue [#498](https://github.com/dovvnloading/Cortex/issues/498) remains open until GitHub reports active protection.

## Activate (repository owner or administrator)

1. Sign in to [GitHub repository Rulesets](https://github.com/dovvnloading/Cortex/settings/rules) and create an **active branch ruleset** targeting only `main`. You can configure it using the exact requirements in [`.github/rulesets/main-protection.json`](../.github/rulesets/main-protection.json).
2. Alternatively, from a trusted local terminal with the repository cloned and an already authorized `gh` login that has repository **Administration: write** permission, first inspect existing rules with `gh api repos/dovvnloading/Cortex/rulesets`. If there is no matching active ruleset, run:

   ```bash
   gh api --method POST repos/dovvnloading/Cortex/rulesets --input .github/rulesets/main-protection.json
   ```

   **Do not POST repeatedly.** If a matching ruleset already exists, edit that ruleset in the GitHub UI instead.

## Required protection

- Require a pull request; permit the single maintainer to squash-merge without self-approval (zero required approving reviews).
- Require these case-sensitive check contexts: `heavy`, `Backend tests`, `Lint, types and contracts`, `Frontend checks`, `dependency-review`, and `Browser tests`.
- Prevent force pushes and deletion of `main`; allow no bypass actors.
- `heavy` is the fail-closed aggregate Quality gate. It checks the relevant suite, backend coverage shards, Windows packaging, and the browser expectation.
- The existing `Browser tests` job is conditional on the changed files. A skipped **job** reports a successful status, so docs-only changes can still be merged; do not skip the entire workflow.
- Enable `CodeQL` as another required check **only if** its workflow exists and publishes that check on pull requests.
- Do **not** require Python-compatibility jobs: they run after a push or on a schedule, not as PR checks.

The provided ruleset uses loose rather than strict base-branch freshness, so a PR need not rerun the suite every time `main` advances. The required checks still have to pass for the PR commit. Change this setting to strict if the team later needs mandatory retesting against every new base commit.

## Verify

```bash
gh api repos/dovvnloading/Cortex/branches/main --jq '.protected'
gh api repos/dovvnloading/Cortex/rulesets --jq '.[] | {id, name, enforcement}'
```

The branch endpoint should return `true` or the ruleset list should show an **active** matching rule targeting `main`. Check that a test PR with a failing `heavy` gate cannot merge, and that a documentation-only PR can merge when the expected jobs are skipped and the aggregate gate passes.

These are GitHub account-level settings: local files and CI alone cannot enforce them. Never paste a personal access token into an issue, commit, or chat.
