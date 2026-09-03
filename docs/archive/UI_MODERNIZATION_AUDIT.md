# Cortex UI modernization audit

**Status:** UI modernization follow-up complete; dedicated image-editor affordances, preset prompt cards, composer clutter, runtime-status overlap, and flattened transcript output removed; native package rebuilt and startup-verified
**Scope:** Every user-facing frontend surface, not a cosmetic theme pass

## What was audited

The review covered the application shell, conversation list, empty and active
chat states, composer, model picker, background task tray, settings and each of
its five sections, local setup, onboarding, dialogs, errors, toasts, loading
states, and compact-window behavior. Image operations remain available to the
execution layer for approved code-driven tasks, but Cortex does not present a
dedicated image editor or generator in the chat UI.

The baseline had a functional UI but not a coherent product interface. The
primary problems were structural:

| Area | Finding | Modernization outcome |
| --- | --- | --- |
| Workspace shell | The top bar conveyed almost no context and the sidebar was a loose list of text. | A clear workspace header, local-runtime status, product identity, conversation count, and a deliberate sidebar footer establish hierarchy. |
| Conversation canvas | A new chat opened into a large, empty dark region, then introduced generic preset prompts. | A quiet new-thread orientation gives the canvas context while leaving the user in control of the first message. |
| Transcript output | The modernization flattened assistant responses into transparent text and weakened the visual treatment of markdown, reasoning, sources, and code. | Role-aware message bubbles, identity metadata, structured markdown rhythm, expandable reasoning/source sections, tables, and labeled copyable fenced-code surfaces restore a professional local workbench transcript. |
| Composer | The input was visually stranded in a full-width bottom strip, with redundant badges and competing colored controls. Long offline-runtime status text could also paint beneath the send control. | The composer is a compact centered island with one model control, grouped metadata, neutral idle chrome, an accent send action only when actionable, and a bounded status region that ellipsizes inside a dedicated gutter before the send column. |
| Conversation controls | Saved chats, rename, and delete controls had weak grouping and feedback. | The list uses active/hover/focus states, reveal-on-focus actions, and purposeful confirmation dialogs. |
| Settings and submenus | Settings read as a large collection of unrelated boxes. | Text-led categories, descriptive labels, and a unified detail pane make settings read like a native control surface. |
| Supporting flows | Setup, model pulls, memory, jobs, errors, and notifications used unrelated visual patterns; the chat also advertised an image editor that is not Cortex's product surface. | Shared surfaces, status colors, progress treatment, form controls, and meaningful feedback now connect those flows. Code-driven image work stays behind the execution layer instead of appearing as a first-class editor. |
| Compact layouts | Responsive behavior was not designed as a first-class layout. | The sidebar becomes an overlay, settings become a horizontal section selector, and the composer remains usable without overflow. |

## Design direction

Cortex is a local desktop workbench, not a cloud-AI landing page or a SaaS
dashboard. The visual system is deliberately utilitarian: a continuous matte
canvas, a compact thread rail, flat operational surfaces, a single warm accent,
and typography that establishes hierarchy before decoration does.

The rules are explicit: no frosted glass, no decorative gradients, no dashboard
card grid, no ornamental category icons, and no canned promotional language.
The composer is the one intentional elevated surface because it is the primary
tool; it visibly floats over the conversation while still belonging to it.
Settings, model operations, task state, and destructive dialogs use the same
squared, text-led vocabulary rather than each inventing a new visual treatment.

The system includes both light and dark themes, explicit focus rings, reduced
motion support, keyboard-accessible menus, visible status text in addition to
color, and touch-friendly controls. It preserves the product's local-only
language throughout the interface rather than implying a cloud account or an
opaque background service.

## Implementation map

- `frontend/src/styles/tokens.css` defines the complete visual system and all
  responsive layouts.
- `frontend/src/components/AppShell.tsx` establishes the workspace hierarchy,
  runtime state, conversation navigation, and better destructive-action dialogs.
- `frontend/src/components/ChatPage.tsx` adds the intentional blank-chat launch
  surface, role-aware transcript bubbles, message metadata, and structured
  response sections. It does not advertise image transformation, image
  generation, or preset prompt suggestions.
- `frontend/src/components/SafeMarkdown.tsx` keeps links and HTML safe while
  providing readable fenced-code toolbars with language labels and copy actions.
- `frontend/src/components/MessageComposer.tsx` integrates local-model context
  into the composer island with a compact, quiet utility row and state-based
  send control.
- `frontend/src/components/SettingsPanel.tsx` turns settings into a structured
  text-led category navigation with descriptions while retaining existing
  keyboard and screen-reader names.

No API contracts, persistence formats, model behavior, or approval semantics
were changed by the UI modernization. The existing execution-layer image
recipe remains available for approved programmatic work; its dedicated chat
panel and starter affordance were removed so the product does not imply that it
is an image-generation tool.

## Verification and release gate

The frontend must pass type checking, linting, component tests, a production
build, and browser-level flows for new chat, streaming, retry/regenerate/fork,
settings, memory, model progress, and compact-window composer behavior. The
native Windows package must also launch from a fresh build before release.

This slice passed the release checks: 47 component tests in a single worker,
8 browser-level flows (including transcript structure and a geometry regression
that keeps long runtime-status text at least 6px before the send control), typecheck, lint, production build, a fresh Windows
package build, and a packaged launch with `GET /api/v1/health/ready` returning
HTTP 200. The production bundle contains no dedicated image-transform UI
labels or selectors, and no preset starter-card selectors or labels.

During the audit, the existing packaged executable exposed a separate native
startup defect: Uvicorn's default console formatter dereferenced `sys.stderr`
in a windowed executable where no console stream exists. The launcher now skips
only that console-oriented logging configuration when the stream is absent. A
regression test covers the condition; the package launch is verified separately
as the final gate.

## Addendum: frontend harness refactor

A later, larger pass turned the frontend from a chat client into a workbench
for working with local models, following the plan in
`docs/FRONTEND_HARNESS_REFACTOR_PLAN.md` (kept local, not pushed to GitHub).
Unlike the modernization above, this pass **did** change the API contract
(new `GenerationOptionsOverride`, `GenerationStats`, and extended
`InstalledModel` fields) and persistence (`messages.generation_stats_json`,
an additive SQLite migration) — noted here so this file doesn't silently
become inaccurate on that point.

**Structural change:** `frontend/src/components/*` was reorganized into
`frontend/src/features/{chat,models,settings,shell,command-palette,markdown}/`,
plus new `stores/` (Zustand), `hooks/`, and `shared/ui/` (Base UI wrappers)
directories. Every path in the "Implementation map" above is stale; the
current equivalents are `features/shell/AppShell.tsx`,
`features/chat/ChatPage.tsx`, `features/markdown/SafeMarkdown.tsx`,
`features/chat/MessageComposer.tsx`, and `features/settings/SettingsPanel.tsx`.

**New surfaces**, held to the same visual rules as above (no glass, no
gradients, no dashboard card grid, text-led over iconography):

- Per-chat generation parameter overrides (`GenerationParamsPopover`) and a
  quiet, `--text-faint` token/tok-per-second chip per response
  (`MessageStats`) — off by default in visual weight, matching the existing
  `<time>` treatment, not a metrics dashboard.
- A model info line (parameter size, quantization, context length) in the
  Models panel, sourced from an `/api/show` response the backend already
  fetched — no new Ollama round trip.
- A virtualized transcript (`react-virtuoso`, `MessageList.tsx`) that only
  activates above 40 messages; below that threshold the DOM is byte-for-byte
  the same plain scrollable div as before.
- Syntax-highlighted fenced code (`rehype-highlight`) that is skipped while a
  message is still streaming and applied once on completion, so it never
  re-tokenizes a growing code block mid-stream.
- A command palette (`cmdk`, Ctrl/Cmd+K) and a keyboard-shortcuts reference
  (`?`), sidebar chat search, and Markdown/JSON transcript export.
- `shared/ui/Select` and `shared/ui/Dialog` (Base UI) replace the
  `RoundedPicker` combobox duplicated between Settings and AppShell's
  rename/delete dialogs, and pick up focus-trapping the hand-rolled versions
  didn't have. `LocalModelMenu` was deliberately **not** migrated: it has
  async reject-and-reopen selection and an adjacent rescan action a plain
  select doesn't model, so it stays purpose-built rather than forced onto a
  primitive that would either lose behavior or need as much wrapping as it
  saves.

**Verification:** typecheck, lint (ESLint + ruff), the full Vitest suite
(139 tests), Playwright e2e (17 specs, including a new virtualized-transcript
spec and a command-palette spec), the production Vite build, and the backend
pytest suite (417 tests) all pass. Contracts were regenerated via
`tools/generate_contracts.py` and match the CI drift check. **Not run in this
pass:** the native Windows package build
(`packaging/build_windows.ps1` → PyInstaller → WebView2 bootstrapper signature
check) — that step is orthogonal to the code changes made here (nothing in
`packaging/`, the launcher, or the PyInstaller spec was touched) but should
still be run before treating this as release-ready, per this project's own
release gate above.

The production JS bundle grew from ~134 KB to ~276 KB gzipped across the five
new dependencies (`zustand`, `react-virtuoso`, `rehype-highlight`, `cmdk`,
`@base-ui/react`). `rehype-highlight` already defaults to `lowlight`'s
"common" ~37-language subset, not the full ~190-language set, so there was no
easy further trim without cutting real language support; the growth is
attributable to genuine new capability rather than obvious waste.
