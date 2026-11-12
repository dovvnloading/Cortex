# Agent instructions

## No browser for code changes (EMERGENCY RULE)

Do not open any browser (a built-in Chromium browser pane, Claude in Chrome, Playwright/Puppeteer previews, or computer use) to make, check, preview or screenshot a code change in this repo.

- Edit code directly in files. Use `git` and `gh` for anything on GitHub. Never use the GitHub website to edit, commit or open PRs.
- Check changes with this project's own build, lint, typecheck or tests from the shell. A small change (copy, styling, config, a few lines) needs no visual check at all.
- Open a browser only when the user explicitly asks for it, or when the task is about a web page that cannot be checked any other way. Ask first in that case.
