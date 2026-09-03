# Cortex frontend refactor: from chat UI to local-model harness

**Status:** Planning document — not implemented. Local working copy only; intentionally
untracked by git per request (do not `git add`/commit/push this file).
**Author context:** Drafted after a full audit of `frontend/src`, `backend/cortex_backend`,
`contracts/cortex-api.ts`, `docs/UI_MODERNIZATION_AUDIT.md`, and current (2025–2026) local-LLM
harness UX conventions (Open WebUI, LM Studio, Jan).
**Scope:** Frontend architecture + the minimal backend contract surface needed to support it.
Does not touch execution/sandboxing, packaging, or the Windows launcher.

---

## Table of contents

1. [What "harness" means here](#1-what-harness-means-here)
2. [Current state — the facts this plan is built on](#2-current-state--the-facts-this-plan-is-built-on)
3. [Guiding constraints](#3-guiding-constraints)
4. [New dependencies](#4-new-dependencies)
5. [Target architecture](#5-target-architecture)
6. [Feature designs, with code](#6-feature-designs-with-code)
7. [Design system deltas](#7-design-system-deltas)
8. [File-by-file migration map](#8-file-by-file-migration-map)
9. [Phased delivery plan](#9-phased-delivery-plan)
10. [Testing strategy updates](#10-testing-strategy-updates)
11. [Risk register](#11-risk-register)
12. [Non-goals](#12-non-goals)
13. [Definition of done](#13-definition-of-done)

---

## 1. What "harness" means here

Cortex today is a good **chat client**: streaming, markdown, retry/regenerate, fork,
attachments, memory. It is not yet a **harness** — a workbench for *working with* a local
model, not just talking to one. The gap, concretely:

- You cannot see or change temperature/top_p/top_k/seed/context-window for a single
  conversation — only globally, in Settings, and only three of those five exist at all
  (`temperature`, `num_ctx`, `seed` — confirmed in `core/settings.py:41-45`).
- You cannot see how fast the model ran, how many tokens it produced, or how full the context
  window is. Ollama returns this on every response and Cortex throws it away
  (`services/llm.py:521-523` reads only `message.content` and `message.thinking`).
- You cannot see what a model actually *is* — parameter count, quantization, context length —
  even though the backend already fetches `/api/show` for every installed model and discards
  everything except the `capabilities` array (`services/models.py:246-268`).
- You cannot search your conversation history, export a transcript, save a reusable prompt, or
  drive the app from the keyboard beyond the composer itself.
- Long conversations render as one giant unvirtualized flex column
  (`tokens.css:259`, plain `overflow-y:auto`) — fine today, a real problem once transcripts get
  long, since every streamed token currently re-renders the whole `.map()`.

None of this requires a rewrite. Cortex's bones — the job/SSE pattern, the session model, the
additive-JSON settings store, the accessible hand-rolled components — are sound and well tested
(11 component test files, 4 Playwright e2e flows, a CI gate that already regenerates and diffs
the API contract on every push). The refactor below is additive and incremental: new state
layer, new streaming primitives, new backend fields that slot into existing shapes, and new
harness-grade panels — without discarding what already works.

---

## 2. Current state — the facts this plan is built on

Condensed from the full audit (file:line references preserved so each claim is checkable).

### Frontend

| Area | Fact |
|---|---|
| State management | None. `App.tsx`'s `AuthenticatedWorkspace` owns ~15 `useState` hooks and drills callbacks through `AppShell` → `ChatPage`/`SettingsPanel`. |
| Streaming | `ChatPage.consumeJob` (`ChatPage.tsx:181-262`) hand-parses SSE, reconnects on drop, and calls `setPartial`/`setThoughts` on **every token** — a full transcript re-render per token today. |
| Routing | Custom, ~80 lines, in `lib/navigation.ts` — `history.pushState` + a synthetic `cortex:navigation` event, consumed via `useSyncExternalStore`. No router library. Small, correct, keep it. |
| Design system | `tokens.css`, 632 lines, **only** color/shadow/radius custom properties (`--bg`, `--surface*`, `--text*`, `--line*`, `--accent*`, `--success*`, `--danger*`, `--shadow-*`, `--radius-*`), light/dark via `[data-theme]`. No spacing/type-scale tokens — sizes are literal per-rule. Breakpoints are literal `@media` values: 920px, 760px, 560px. One global reduced-motion rule (`tokens.css:629-631`). |
| Accessible widgets | Two **independently reimplemented** roving-tabindex comboboxes: `SettingsPanel`'s `RoundedPicker` (`SettingsPanel.tsx:275-386`) and `LocalModelMenu.tsx`. No headless UI library anywhere. |
| Markdown/code | `SafeMarkdown.tsx` wraps `react-markdown` + `remark-gfm` + `rehype-sanitize`. Fenced code gets a copy-button toolbar but **zero syntax coloring** — plain `<code>` text. Images are stripped (`img: () => null`). |
| Testing | Vitest + Testing Library, real `CortexApi` against a faked `fetch`, no MSW. Playwright intercepts all API calls via `page.route()`. `frontend/src/test/setup.ts` is 5 lines. This pattern is good and the plan preserves it. |
| CI | `.github/workflows/quality.yml` — Python checks → **regenerate `contracts/*` and `git diff --exit-code`** (any backend schema change *must* be followed by `python tools/generate_contracts.py`) → `npm run typecheck` → `lint` → `test` → `e2e` (Chromium only, `--workers=1`) → `build` → native package build + WebView2 signature check. Frontend tests run before packaging; packaging failing on a frontend regression is expensive to debug, so this plan keeps every phase green at that gate. |

### Backend

| Area | Fact |
|---|---|
| Per-request generation options | **None exist.** `GenerationRequest`/`RegenerationRequest` (`api/schemas.py:546-563`) carry no temperature/top_p/etc. The Ollama `options` dict is built exclusively from the single global `CortexSettings.generation` row (`routes.py:1994-2046`, the `_generation_snapshot` function) plus one derived key (`num_predict`, computed in `llm.py:513-514`). Two other call sites (translation, title generation) use **hardcoded** literals (`llm.py:558`, `llm.py:719`) that bypass settings entirely — out of scope for this plan, noted as a pre-existing quirk. |
| Ollama response data | `SynthesisAgent.generate` reads only `message.content` and `message.thinking` off the Ollama response (`llm.py:521-523`). `eval_count`, `eval_duration`, `prompt_eval_count`, `prompt_eval_duration`, `total_duration` are never read, never placed in an SSE payload, never persisted. Confirmed via a repo-wide grep: zero matches. |
| Model metadata | `/api/show` (Ollama's model-detail endpoint) is **already called** for every installed model, inside `ModelService.capabilities()` (`services/models.py:246-268`), but only the `capabilities` array is extracted. `parameter_size`, `quantization_level`, `family`, and context length are sitting in the same already-fetched response, unused — this is a zero-new-network-call win. |
| Persistence | `messages` table (`repositories/legacy_storage.py:116-138`): `id, thread_id, role, content, sources, thoughts, attachments, timestamp`. No stats/options column. `sources`/`thoughts`/`attachments` are already JSON-in-TEXT — the established pattern for adding structured, optional per-message data. Settings is a single-row JSON blob (`cortex_settings.payload`) — adding a *global* generation field needs no migration at all; a *per-chat* override needs a new column, because settings has no per-chat scope today. |
| Endpoints | Solid REST + job/SSE pattern already exists for chats, generations, models, execution. No `/models/{name}` detail route, no embeddings route (a legacy unused `vectors` table exists but is wired to nothing). |
| Auth | Every route — including SSE — goes through `Depends(require_session)`: bearer session token + loopback/origin header check (`api/security.py`). Any new stream must follow the identical pattern. |

### Library research verdicts (full rationale in each feature section below)

| Concern | Pick | Rejected |
|---|---|---|
| Global client state | **Zustand** | Jotai (bigger mental shift than needed), Context+useReducer (re-render fan-out on every token without hand-splitting), Valtio (smaller ecosystem), Legend-State (unmaintained — no release in ~2 years, 210 open issues) |
| Server-state caching | **Keep the hand-rolled `CortexApi`** | TanStack Query — its SSE story is "push into the cache yourself," so it adds ceremony without solving anything this app doesn't already solve with SSE + Zustand |
| Transcript virtualization | **`react-virtuoso`** | `@tanstack/react-virtual` (headless, more code to own), `react-window` (fixed-size-oriented, stagnant) |
| Syntax highlighting | **`rehype-highlight`** (highlight.js/lowlight) | `shiki`/`react-shiki` (best fidelity, but ships a ~1.5MB WASM grammar engine — a real cold-start tax inside a WebView2 shell) |
| Command palette | **`cmdk`** | `kbar` (ships its own opinionated UI, harder to skin onto a hand-rolled token system) |
| Headless accessible primitives | **`@base-ui/react`** | Radix (one npm package per primitive — bigger dependency graph for a packaged desktop app) |

---

## 3. Guiding constraints

These are non-negotiable while implementing anything below:

1. **Design language stays.** `docs/UI_MODERNIZATION_AUDIT.md` already established the rules:
   no frosted glass, no decorative gradients, no dashboard card grid, no ornamental icons per
   category, text-led over iconography, one accent color. Every new surface (command palette,
   parameter popover, model info panel) follows the same squared, flat, text-first vocabulary as
   `SettingsPanel` and the composer — not a new visual language bolted on top.
2. **Local-first, loopback-only.** No new endpoint, stream, or stored value may imply a cloud
   account, remote sync, or telemetry. Everything proposed here is local SQLite + local SSE.
3. **CI gate stays green at every phase boundary.** Typecheck, lint, Vitest, Playwright e2e,
   production build, contract-drift check, native package build. A phase isn't "done" until all
   of these pass, not just the new tests for that phase.
4. **Contract-first backend changes.** Any `schemas.py` change must be followed by
   `python tools/generate_contracts.py` before the frontend touches the new field — the CI
   contract-diff check enforces this, so the plan enforces it too.
5. **No framework rewrite.** No React Router, no Tailwind, no SSR, no bundler change. Every new
   dependency is additive to the current Vite 8 + React 19 + hand-rolled-CSS stack.
6. **Accessibility bar doesn't drop.** The existing app already hand-builds correct
   `role="listbox"`/roving-tabindex/`aria-live` patterns (see audit §7). New widgets must match
   or exceed that, which is exactly why headless primitives (Base UI) are being introduced now —
   to stop *reimplementing* that work per component.

---

## 4. New dependencies

```bash
npm.cmd install --prefix frontend zustand react-virtuoso rehype-highlight cmdk @base-ui/react
```

| Package | Why | Approx. weekly downloads / maintenance signal |
|---|---|---|
| `zustand` | Global store for chats, active generation, models, settings draft, UI (palette/toasts) without prop-drilling or context re-render fan-out. | ~40M/week, actively maintained |
| `react-virtuoso` | Chat-shaped virtualization: `followOutput` gives "stick to bottom while streaming, don't yank the user back down if they scrolled up" for free; handles variable-height markdown/code messages without manual measurement. | Actively released, purpose-built chat examples |
| `rehype-highlight` | Syntax coloring for fenced code blocks, synchronous, no WASM — matters for a WebView2 cold start. | Standard `unified`/`rehype` ecosystem package |
| `cmdk` | Unstyled command palette primitive (the same one shadcn/ui builds on) — styles entirely via our own classNames onto `tokens.css`. | Extremely wide adoption, stable API |
| `@base-ui/react` | One package (not one-per-primitive like Radix) for Select/Popover/Dialog/Tooltip/Menu — replaces the two duplicated hand-rolled comboboxes and backs every new popover/dialog below. Built by the MUI team as of the 1.0 release (Dec 2025). | Single dependency-graph entry, active full-time maintenance |

No dependency is removed. `react-markdown`, `remark-gfm`, `rehype-sanitize`, `lucide-react`
stay exactly as they are.

---

## 5. Target architecture

### 5.1 Folder restructure

Current `frontend/src/components/*.tsx` is a flat 16-file bag mixing shell chrome, settings,
chat, and system components. Target layout groups by feature, keeps `lib/` for pure helpers,
and adds three new top-level folders:

```text
frontend/src/
  api/
    client.ts                  # unchanged shape, gains a couple of methods (§6.1, §6.3)
  stores/                      # NEW — Zustand
    useChatStore.ts
    useModelStore.ts
    useSettingsStore.ts
    useUiStore.ts               # command palette open state, toasts, keyboard-shortcut overlay
  hooks/                       # NEW — cross-feature hooks
    useGenerationStream.ts      # extracted from ChatPage.consumeJob
    useRafBatchedText.ts
    useHotkey.ts
  shared/ui/                   # NEW — Base UI wrappers styled with tokens.css
    Select.tsx                 # replaces RoundedPicker + LocalModelMenu's internals
    Popover.tsx
    Dialog.tsx
    Tooltip.tsx
  features/
    chat/
      ChatPage.tsx              # slimmer — delegates to hooks/stores
      MessageList.tsx           # NEW — react-virtuoso wrapper
      MessageCard.tsx           # extracted from ChatPage.tsx
      MessageStats.tsx          # NEW — tok/s, TTFT, duration chip
      MessageComposer.tsx
      GenerationParamsPopover.tsx  # NEW
      ExportTranscriptMenu.tsx     # NEW
    models/
      LocalModelMenu.tsx         # rebuilt on shared/ui/Select
      ModelsPanel.tsx
      ModelInfoPanel.tsx         # NEW
    settings/
      SettingsPanel.tsx          # RoundedPicker usages swapped for shared/ui/Select
      MemoryPanel.tsx
      PromptTemplatesPanel.tsx   # NEW (Phase 6, optional)
    shell/
      AppShell.tsx
      ConversationSearch.tsx     # NEW
      ExecutionTaskTray.tsx
      Onboarding.tsx
      LocalSetup.tsx
      SystemStatusCard.tsx
      NavigationLink.tsx
    command-palette/
      CommandPalette.tsx         # NEW — cmdk
      ShortcutsHelpDialog.tsx    # NEW
    markdown/
      SafeMarkdown.tsx           # gains rehype-highlight + deferred-highlight prop
  lib/                          # unchanged: navigation.ts, localModels.ts, chatTitle.ts, ...
```

This is a mechanical `git mv` exercise plus import-path updates — no component's *behavior*
changes just from moving. Do the move in its own commit per phase (see §9) so diffs stay
reviewable.

### 5.2 State layer — Zustand stores

Four stores, matching the four domains the audit identified as prop-drilled: chats/generation,
models, settings-draft, and transient UI state. Each store is a plain module — no `Provider`
tree, which matters for a WebView2-embedded SPA that doesn't need to worry about multiple React
roots.

**`stores/useChatStore.ts`** — the one that matters most, since it holds per-token streaming
state and must not cause unrelated components (sidebar, settings) to re-render on every token:

```ts
// frontend/src/stores/useChatStore.ts
import { create } from "zustand";
import type { ChatMessage, ChatResponse, ChatSummary } from "../../../contracts/cortex-api";

type GenerationPhase = "idle" | "starting" | "streaming" | "stopping";

interface GenerationState {
  jobId: string | null;
  threadId: string | null;
  phase: GenerationPhase;
  partialContent: string;
  partialThoughts: string;
  statusText: string;
  error: string | null;
}

interface ChatStoreState {
  chats: ChatSummary[];
  activeChat: ChatResponse | null;
  generation: GenerationState;

  setChats: (chats: ChatSummary[]) => void;
  upsertChatSummary: (chat: ChatResponse) => void;
  setActiveChat: (chat: ChatResponse | null) => void;

  beginGeneration: (jobId: string, threadId: string) => void;
  appendContentToken: (jobId: string, delta: string) => void;
  appendThinkingToken: (jobId: string, delta: string) => void;
  setStatusText: (jobId: string, text: string) => void;
  markStopping: (jobId: string) => void;
  endGeneration: (jobId: string, error?: string | null) => void;
}

const idleGeneration: GenerationState = {
  jobId: null,
  threadId: null,
  phase: "idle",
  partialContent: "",
  partialThoughts: "",
  statusText: "",
  error: null,
};

export const useChatStore = create<ChatStoreState>((set) => ({
  chats: [],
  activeChat: null,
  generation: idleGeneration,

  setChats: (chats) => set({ chats }),
  upsertChatSummary: (chat) =>
    set((state) => ({
      chats: [
        { id: chat.id, title: chat.title, timestamp: chat.timestamp },
        ...state.chats.filter((item) => item.id !== chat.id),
      ],
    })),
  setActiveChat: (chat) => set({ activeChat: chat }),

  beginGeneration: (jobId, threadId) =>
    set({ generation: { ...idleGeneration, jobId, threadId, phase: "starting" } }),

  // Every guard here ignores events from a stale/superseded job — the same
  // invariant ChatPage.consumeJob enforces today via event_id/thread_id checks.
  appendContentToken: (jobId, delta) =>
    set((state) =>
      state.generation.jobId === jobId
        ? { generation: { ...state.generation, phase: "streaming", partialContent: state.generation.partialContent + delta } }
        : state,
    ),
  appendThinkingToken: (jobId, delta) =>
    set((state) =>
      state.generation.jobId === jobId
        ? { generation: { ...state.generation, phase: "streaming", partialThoughts: state.generation.partialThoughts + delta } }
        : state,
    ),
  setStatusText: (jobId, text) =>
    set((state) => (state.generation.jobId === jobId ? { generation: { ...state.generation, statusText: text } } : state)),
  markStopping: (jobId) =>
    set((state) => (state.generation.jobId === jobId ? { generation: { ...state.generation, phase: "stopping" } } : state)),
  endGeneration: (jobId, error = null) =>
    set((state) => (state.generation.jobId === jobId ? { generation: { ...idleGeneration, error } } : state)),
}));

// Selector hooks — components subscribe to exactly the slice they render,
// so MessageComposer re-renders on every token but the sidebar chat list does not.
export const useGenerationPhase = () => useChatStore((s) => s.generation.phase);
export const useStreamingContent = () => useChatStore((s) => s.generation.partialContent);
```

`useModelStore` and `useSettingsStore` follow the same shape (state + narrow actions, no
`Provider`) and simply hold what `App.tsx` currently holds in `useState`: `models`,
`modelProgress`, `modelBusy`, and `settings`/`saving` respectively. `useUiStore` holds
`commandPaletteOpen`, `shortcutsDialogOpen`, and toast state (replacing `ToastProvider`'s
context with a store — same external API via a thin `useToast()` wrapper, so call sites don't
change).

### 5.3 Unified SSE streaming hook

`ChatPage.consumeJob` (81 lines) currently hand-reconnects, hand-parses event ordering, and
writes directly into six different `useState` setters. Extracting it into a hook that writes
into `useChatStore` instead removes the duplication and — critically — decouples "receiving a
token" from "re-rendering the whole page":

```ts
// frontend/src/hooks/useGenerationStream.ts
import { useCallback, useRef } from "react";
import type { CortexApi } from "../api/client";
import { ApiError } from "../api/client";
import { useChatStore } from "../stores/useChatStore";

const RECONNECT_DELAY_MS = 250;

export function useGenerationStream(api: CortexApi) {
  const consumingRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const store = useChatStore;

  const stop = useCallback(() => abortRef.current?.abort(), []);

  const consume = useCallback(
    async (jobId: string, threadId: string, startEventId: number, onCompleted: (threadId: string) => Promise<void>) => {
      if (consumingRef.current === jobId) return;
      consumingRef.current = jobId;
      const controller = new AbortController();
      abortRef.current = controller;
      let cursor = startEventId;
      let terminal = false;

      try {
        while (!terminal && !controller.signal.aborted) {
          try {
            await api.streamGeneration(
              jobId,
              (event) => {
                if (event.event_id <= cursor || event.thread_id !== threadId) return;
                cursor = event.event_id;
                const data = event.data ?? {};
                if (typeof data.message === "string") store.getState().setStatusText(jobId, data.message);
                if (event.event === "generation.cancelling") store.getState().markStopping(jobId);
                if (event.event === "generation.thinking_delta" && typeof data.delta === "string") {
                  store.getState().appendThinkingToken(jobId, data.delta);
                }
                if (event.event === "generation.content_delta" && typeof data.delta === "string") {
                  store.getState().appendContentToken(jobId, data.delta);
                }
                if (event.event === "generation.completed") {
                  terminal = true;
                  void onCompleted(threadId);
                }
                if (event.event === "generation.failed" || event.event === "generation.cancelled") {
                  terminal = true;
                  store.getState().endGeneration(jobId, typeof data.message === "string" ? data.message : "Generation did not complete.");
                  void onCompleted(threadId);
                }
              },
              { signal: controller.signal, afterEventId: cursor },
            );
            if (!terminal && !controller.signal.aborted) await delay(RECONNECT_DELAY_MS);
          } catch (streamError) {
            if (controller.signal.aborted) return;
            if (streamError instanceof ApiError && streamError.status === 401) return;
            const snapshot = await api.generationStatus(jobId);
            if (snapshot.status === "succeeded" || snapshot.status === "failed" || snapshot.status === "cancelled") {
              terminal = true;
              if (snapshot.status !== "succeeded") store.getState().endGeneration(jobId, snapshot.error ?? "Generation did not complete.");
              await onCompleted(threadId);
            } else {
              await delay(RECONNECT_DELAY_MS);
            }
          }
        }
      } finally {
        if (terminal) store.getState().endGeneration(jobId);
        consumingRef.current = null;
      }
    },
    [api],
  );

  return { consume, stop };
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}
```

`ChatPage.tsx` shrinks to: call `consume()` on mount/job-resume, subscribe to
`useStreamingContent()`/`useGenerationPhase()` for rendering, and keep only what's genuinely
page-local (draft text, attachments, the transcript scroll-position ref). The reconnect/backoff
semantics, `sessionStorage`-persisted active-job resume, and thread/event-id staleness guards
are preserved exactly — this is a structural extraction, not a behavior change.

### 5.4 RAF-batched token buffering

Right now every SSE `content_delta` triggers a React state update and a full markdown re-parse
of the growing message. At fast token rates this is the single biggest perf risk in the whole
plan — worse with virtualization and syntax highlighting layered on top. Fix once, centrally:

```ts
// frontend/src/hooks/useRafBatchedText.ts
import { useCallback, useEffect, useRef, useState } from "react";

/** Coalesces many rapid `push()` calls into at most one state update per animation frame. */
export function useRafBatchedText(initial = "") {
  const [text, setText] = useState(initial);
  const bufferRef = useRef(initial);
  const rafRef = useRef<number | null>(null);

  const push = useCallback((chunk: string) => {
    bufferRef.current += chunk;
    if (rafRef.current == null) {
      rafRef.current = requestAnimationFrame(() => {
        setText(bufferRef.current);
        rafRef.current = null;
      });
    }
  }, []);

  const reset = useCallback((value = "") => {
    bufferRef.current = value;
    setText(value);
  }, []);

  useEffect(() => () => { if (rafRef.current != null) cancelAnimationFrame(rafRef.current); }, []);
  return { text, push, reset };
}
```

Applied at the `useChatStore.appendContentToken` call site (batch there instead of in the
component, so every subscriber sees the same coalesced rate) rather than per-component, so it
benefits `MessageList`, `MessageCard`, and any future consumer (e.g. a live token counter)
uniformly.

---

## 6. Feature designs, with code

Ordered by dependency (state layer → rendering → harness controls → productivity layer), not by
priority — see §9 for the actual phase order and what's optional.

### 6.1 Virtualized, streaming-safe transcript

Only virtualize once it's worth it — short chats render fine as a plain map, and virtualization
has its own overhead. `react-virtuoso`'s `followOutput` is exactly the "stick to bottom while
streaming, but respect the user scrolling up to read history" behavior `ChatPage.tsx` currently
hand-builds via `isNearTranscriptEnd`/`showJumpToLatest` (`ChatPage.tsx:510-523`) — that logic
gets deleted, not reimplemented:

```tsx
// frontend/src/features/chat/MessageList.tsx
import { useRef } from "react";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";
import type { ChatMessage } from "../../../../contracts/cortex-api";
import { MessageCard } from "./MessageCard";

const VIRTUALIZE_THRESHOLD = 40;

type Props = {
  messages: ChatMessage[];
  isStreaming: boolean;
  finalAssistantId: string | null;
  onRegenerate: (message: ChatMessage, index: number) => void;
  onFork: (message: ChatMessage) => void;
};

export function MessageList({ messages, isStreaming, finalAssistantId, onRegenerate, onFork }: Props) {
  const virtuosoRef = useRef<VirtuosoHandle>(null);

  if (messages.length < VIRTUALIZE_THRESHOLD) {
    return (
      <div className="transcript-plain">
        {messages.map((message, index) => (
          <MessageCard
            key={message.id ?? `${message.role}-${index}`}
            message={message}
            isFinalAssistant={message.id === finalAssistantId}
            onRegenerate={() => onRegenerate(message, index)}
            onFork={() => onFork(message)}
          />
        ))}
      </div>
    );
  }

  return (
    <Virtuoso
      ref={virtuosoRef}
      className="transcript-virtual"
      data={messages}
      followOutput={isStreaming ? "smooth" : false}
      initialTopMostItemIndex={messages.length - 1}
      alignToBottom
      itemContent={(index, message) => (
        <MessageCard
          message={message}
          isFinalAssistant={message.id === finalAssistantId}
          onRegenerate={() => onRegenerate(message, index)}
          onFork={() => onFork(message)}
        />
      )}
    />
  );
}
```

The in-flight streaming bubble (the `partial`/`thoughts` card `ChatPage.tsx:544-553` renders
below the message loop) stays outside the `Virtuoso`/plain-map branch entirely, appended after
either — it's exactly one item and doesn't need virtualizing.

**Test impact:** `e2e/chat.spec.ts` currently queries the transcript DOM directly. Below the
40-message threshold (true for every existing e2e fixture) nothing changes — `.transcript-plain`
renders the identical DOM structure. A new e2e spec (`e2e/virtualized-transcript.spec.ts`, added
in the phase that ships this) seeds 60+ messages and asserts `react-virtuoso`'s windowed
rendering plus the stick-to-bottom behavior specifically.

### 6.2 Streaming-safe syntax highlighting

Two problems solved together: no highlighting exists today, and naively adding
`rehype-highlight` would re-tokenize and re-highlight a growing code block on every one of
potentially hundreds of tokens per second. Fix: **only highlight once a message is no longer
streaming.** In-flight code renders as plain (already-styled) `<pre>`, then gets highlighted in
one pass the moment the turn completes — visually seamless, computationally free during the hot
path.

```tsx
// frontend/src/features/markdown/SafeMarkdown.tsx (delta)
import rehypeHighlight from "rehype-highlight";
// ...existing imports (ReactMarkdown, remarkGfm, rehypeSanitize, existing component overrides)

type SafeMarkdownProps = {
  content: string;
  /** Skip syntax highlighting while a message is still streaming; pass true once finalized. */
  finalized?: boolean;
};

export function SafeMarkdown({ content, finalized = true }: SafeMarkdownProps) {
  const rehypePlugins = finalized ? [rehypeSanitize, rehypeHighlight] : [rehypeSanitize];
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={rehypePlugins} components={components}>
      {content}
    </ReactMarkdown>
  );
}
```

Call sites: the in-flight assistant bubble passes `finalized={false}`; every persisted message
(`MessageCard`) passes the default `finalized={true}`. `rehype-highlight` output classes
(`hljs-keyword`, `hljs-string`, …) get mapped onto `tokens.css`'s existing accent/text palette
rather than importing a prebuilt highlight.js theme, so highlighted code doesn't introduce new
colors outside the established system (see §7).

### 6.3 Per-chat generation parameter overrides

This is the core "harness" feature. Today temperature/context/seed are global-only
(`core/settings.py:41-45`, `routes.py:1994-2046`). The design promotes them (plus `top_p`,
`top_k`, `repeat_penalty` — currently absent even as *globals*) to a two-layer model: **global
defaults** (Settings → AI Model, extended) and an optional **per-request override** from a new
composer control, exactly mirroring how `num_ctx`/`seed` already flow, just widened.

**Backend — `core/settings.py`:**

```python
class GenerationSettings(_SettingsModel):
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    top_p: float = Field(default=0.9, ge=0.0, le=1.0)
    top_k: int = Field(default=40, ge=0, le=200)
    repeat_penalty: float = Field(default=1.1, ge=0.5, le=2.0)
    num_ctx: int = Field(default=4096, ge=2048, le=16384)
    seed: int = Field(default=-1, ge=-1, le=2147483647)
    system_instructions: str = Field(default="", max_length=1800)


GENERATION_OVERRIDE_FIELDS = ("temperature", "top_p", "top_k", "repeat_penalty", "num_ctx", "seed")


class GenerationOptionsOverride(_SettingsModel):
    """Optional per-request overrides. Unset fields fall back to GenerationSettings."""
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    top_k: int | None = Field(default=None, ge=0, le=200)
    repeat_penalty: float | None = Field(default=None, ge=0.5, le=2.0)
    num_ctx: int | None = Field(default=None, ge=2048, le=16384)
    seed: int | None = Field(default=None, ge=-1, le=2147483647)
```

**Backend — `api/schemas.py`** (adds one optional field to each request; nothing existing
changes shape):

```python
class GenerationRequest(_ApiModel):
    request_id: str | None = None
    thread_id: str | None = None
    user_input: str
    base_revision: int | None = None
    attachments: list[ChatAttachment] = Field(default_factory=list)
    options: GenerationOptionsOverride | None = None   # NEW


class RegenerationRequest(_ApiModel):
    request_id: str | None = None
    message_id: str
    user_input: str | None = None
    base_revision: int | None = None
    attachments: list[ChatAttachment] = Field(default_factory=list)
    options: GenerationOptionsOverride | None = None   # NEW
```

**Backend — `api/routes.py`**, `_generation_snapshot` merge (before/after):

```python
# Before (routes.py:2018-2036, abridged):
return GenerationSnapshot(
    ...,
    model_options={
        "temperature": settings.generation.temperature,
        "num_ctx": settings.generation.num_ctx,
        "seed": settings.generation.seed,
    },
    ...,
)

# After:
def _merged_model_options(
    generation_settings: GenerationSettings,
    override: GenerationOptionsOverride | None,
) -> dict[str, float | int]:
    merged: dict[str, float | int] = {
        field: getattr(generation_settings, field) for field in GENERATION_OVERRIDE_FIELDS
    }
    if override is not None:
        for field in GENERATION_OVERRIDE_FIELDS:
            value = getattr(override, field, None)
            if value is not None:
                merged[field] = value
    return merged

return GenerationSnapshot(
    ...,
    model_options=_merged_model_options(settings.generation, request.options),
    ...,
)
```

`GenerationSnapshot.model_options` is already an immutable `Mapping[str, Any]` captured once per
job (`core/generation.py:189`) and forwarded unmodified through `GenerationService.generate` and
into `SynthesisAgent.generate`'s `options` parameter — **no change needed** below `routes.py`;
the override just changes what arrives in that dict. Validation bounds live once, on the
Pydantic field definitions, so an override can't smuggle an out-of-range value past what the
global setting itself would allow.

After this schema change: `python tools/generate_contracts.py`, which regenerates
`contracts/cortex-api.ts`'s `GenerationOptionsOverride`/`GenerationRequest`/`RegenerationRequest`
— commit that alongside the schema change or CI's contract-diff check fails.

**Frontend — the popover.** Built on the new `shared/ui/Popover` (Base UI), anchored to a new
"Parameters" toolbar button in `MessageComposer`'s existing `composer-toolbar-leading` group
(next to the model picker):

```tsx
// frontend/src/features/chat/GenerationParamsPopover.tsx
import { SlidersHorizontal, RotateCcw } from "lucide-react";
import { Popover } from "../../shared/ui/Popover";
import type { GenerationOptionsOverride } from "../../../../contracts/cortex-api";

type Props = {
  value: GenerationOptionsOverride | null;
  defaults: { temperature: number; top_p: number; top_k: number; repeat_penalty: number; num_ctx: number };
  onChange: (next: GenerationOptionsOverride | null) => void;
};

export function GenerationParamsPopover({ value, defaults, onChange }: Props) {
  const active = value !== null && Object.values(value).some((v) => v != null);
  const set = (field: keyof GenerationOptionsOverride, raw: string) => {
    const next = { ...(value ?? {}) };
    next[field] = raw === "" ? null : Number(raw);
    onChange(next);
  };

  return (
    <Popover.Root>
      <Popover.Trigger className={`icon-button icon-button-small${active ? " icon-button-active" : ""}`} aria-label="Generation parameters for this chat">
        <SlidersHorizontal size={15} aria-hidden="true" />
      </Popover.Trigger>
      <Popover.Content className="params-popover" aria-label="Generation parameters">
        <div className="params-popover-header">
          <span>Parameters for this chat</span>
          {active && (
            <button type="button" className="button button-quiet" onClick={() => onChange(null)}>
              <RotateCcw size={13} aria-hidden="true" /> Reset to defaults
            </button>
          )}
        </div>
        <ParamSlider label="Temperature" field="temperature" min={0} max={2} step={0.1} value={value} defaults={defaults} onSet={set} />
        <ParamSlider label="Top P" field="top_p" min={0} max={1} step={0.05} value={value} defaults={defaults} onSet={set} />
        <ParamSlider label="Top K" field="top_k" min={0} max={200} step={1} value={value} defaults={defaults} onSet={set} />
        <ParamSlider label="Repeat penalty" field="repeat_penalty" min={0.5} max={2} step={0.05} value={value} defaults={defaults} onSet={set} />
        <ParamSlider label="Context window" field="num_ctx" min={2048} max={16384} step={1024} value={value} defaults={defaults} onSet={set} />
        <p className="params-popover-hint">Overrides apply to this chat only. Settings → AI Model controls the defaults.</p>
      </Popover.Content>
    </Popover.Root>
  );
}
```

(`ParamSlider` is a small shared row component — label, native `<input type="range">`, numeric
readout — styled with the exact same `.setting-field`/`.setting-row` classes `SettingsPanel`
already uses for its temperature slider, so this doesn't introduce a second visual pattern for
"a labeled slider.") The override is per-chat state, not persisted server-side beyond being sent
with each `generate()`/`regenerate()` call — stored client-side in `useChatStore` keyed by
thread id, cleared on "Reset to defaults."

### 6.4 Live token/timing stats

**Backend — capture.** Ollama's non-streamed terminal chunk includes usage/timing fields Cortex
currently discards entirely. Extract them once, in `llm.py`, right where the response is already
being read:

```python
# core/generation.py — new small value type, JSON-serializable
@dataclass(frozen=True, slots=True)
class GenerationStats:
    prompt_eval_count: int | None = None
    eval_count: int | None = None
    prompt_eval_duration_ms: float | None = None
    eval_duration_ms: float | None = None
    total_duration_ms: float | None = None
    tokens_per_second: float | None = None


# services/llm.py — extracted alongside the existing content/thinking read
def _extract_stats(response: dict) -> GenerationStats | None:
    eval_count = response.get("eval_count")
    eval_duration = response.get("eval_duration")  # nanoseconds, per Ollama's API
    if eval_count is None and response.get("total_duration") is None:
        return None
    tokens_per_second = (
        round(eval_count / (eval_duration / 1_000_000_000), 1)
        if eval_count and eval_duration
        else None
    )
    return GenerationStats(
        prompt_eval_count=response.get("prompt_eval_count"),
        eval_count=eval_count,
        prompt_eval_duration_ms=_ns_to_ms(response.get("prompt_eval_duration")),
        eval_duration_ms=_ns_to_ms(eval_duration),
        total_duration_ms=_ns_to_ms(response.get("total_duration")),
        tokens_per_second=tokens_per_second,
    )


def _ns_to_ms(value: int | None) -> float | None:
    return round(value / 1_000_000, 1) if value is not None else None
```

`SynthesisAgent.generate`'s return type grows from `tuple[str, str | None, MemoryCommand]` to
`tuple[str, str | None, MemoryCommand, GenerationStats | None]`; every current call site
(`routes.py`'s job runner) already unpacks this tuple and gains one more field. **Required
companion change:** `testing/fake_ollama.py`'s terminal `{"done": true}` chunk (currently
`testing/fake_ollama.py:236-242`) must start including `eval_count`/`eval_duration`/etc. so
tests exercise this path — without it, every test's stats will be `None`, silently skipping
coverage of the new feature.

**Backend — surface + persist.** The `generation.completed` SSE event's `data` payload is just a
dict the job runner returns (`routes.py:1755-1765`); extend it directly since `GenerationEvent.data`
is already an untyped `Record<string, unknown>` in the contract — no contract break:

```python
# routes.py job runner, after generate() call
final_answer, thoughts, memory_command, stats = synthesis_agent.generate(...)
...
return {
    "thread_id": thread_id,
    "user_message_id": user_message_id,
    "assistant_message_id": assistant_message_id,
    "chat_revision": chat_revision,
    "title": title,
    "response": final_answer,
    "thoughts": thoughts,
    "clear_requested": memory_command.clear,
    "code_execution_job_id": code_execution_job_id,
    "stats": asdict(stats) if stats else None,   # NEW
}
```

Persist the same value onto the assistant's row so it survives a reload — additive column,
following the exact pattern `sources`/`thoughts`/`attachments` already use (JSON-in-TEXT):

```sql
-- migration, guarded like the codebase's existing additive column migrations
ALTER TABLE messages ADD COLUMN generation_stats_json TEXT;
```

```python
# repositories/legacy_storage.py — additive migration guard (follow the existing
# pattern used for prior columns in this file; sketch below)
existing_columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
if "generation_stats_json" not in existing_columns:
    connection.execute("ALTER TABLE messages ADD COLUMN generation_stats_json TEXT")
```

`ChatMessage` (schemas.py + contract) gains `stats: GenerationStats | None = None`, populated on
read/write in `repositories/chats.py` exactly where `thoughts`/`sources` already round-trip
through JSON.

**Frontend — display.** A quiet, understated chip — not a colorful dashboard badge, per the
design constraints — appended to `MessageIdentity`'s existing metadata row:

```tsx
// frontend/src/features/chat/MessageStats.tsx
import type { GenerationStats } from "../../../../contracts/cortex-api";

export function MessageStats({ stats }: { stats?: GenerationStats | null }) {
  if (!stats || stats.tokens_per_second == null) return null;
  const seconds = stats.total_duration_ms ? (stats.total_duration_ms / 1000).toFixed(1) : null;
  return (
    <span className="message-stats" title="Generation performance for this response">
      {stats.eval_count ?? "?"} tok · {stats.tokens_per_second} tok/s{seconds ? ` · ${seconds}s` : ""}
    </span>
  );
}
```

Rendered inline after the existing `<time>` element in `MessageIdentity` (`ChatPage.tsx:636-641`),
same `text-faint` treatment, hidden behind a new Settings → AI Model toggle
(`generation.show_stats`, default off) so it doesn't clutter the transcript for people who don't
want it — consistent with Cortex's "quiet by default" ethos.

### 6.5 Model info panel

Zero new network calls — `/api/show` is already fetched per model in
`ModelService.capabilities()`; this just stops throwing away most of the response:

```python
# services/models.py — extend capabilities() extraction (models.py:246-268)
def capabilities(self, model: str) -> ModelCapabilities:
    response = self._gateway.show(model)
    capabilities = tuple(response.get("capabilities", ()))
    details = response.get("details", {})
    model_info = response.get("model_info", {})
    family = details.get("family")
    context_length = model_info.get(f"{family}.context_length") if family else None
    return ModelCapabilities(
        capabilities=capabilities,
        supports_vision="vision" in capabilities,
        parameter_size=details.get("parameter_size"),      # NEW, e.g. "8.0B"
        quantization_level=details.get("quantization_level"),  # NEW, e.g. "Q4_K_M"
        family=family,                                       # NEW, e.g. "qwen3"
        context_length=context_length,                       # NEW, e.g. 40960
    )
```

> `details`/`model_info` key names above match Ollama's documented `/api/show` shape as of
> current releases; **verify against the actually-installed Ollama version's response** before
> shipping, and extend `testing/fake_ollama.py`'s `/api/show` fixture to model these fields (it
> currently returns `{"capabilities": [...]}` only) so the new fields have test coverage.

`InstalledModel` (schemas.py + contract) gains the four optional fields; regenerate contracts.
Frontend: a small expandable panel triggered from `LocalModelMenu`'s existing option rows (an
"info" affordance next to each model name) or a dedicated row in `ModelsPanel`, rendering
`"8.0B params · Q4_K_M · 40,960 ctx"` — plain text, no iconography, matching `ModelsPanel`'s
existing chip style.

### 6.6 Command palette + keyboard shortcuts help

```tsx
// frontend/src/features/command-palette/CommandPalette.tsx
import { Command } from "cmdk";
import { useChatStore } from "../../stores/useChatStore";
import { useModelStore } from "../../stores/useModelStore";
import { useUiStore } from "../../stores/useUiStore";
import { useNavigate } from "../../lib/navigation";

export function CommandPalette() {
  const open = useUiStore((s) => s.commandPaletteOpen);
  const setOpen = useUiStore((s) => s.setCommandPaletteOpen);
  const navigate = useNavigate();
  const models = useModelStore((s) => s.localModels);
  const selectModel = useModelStore((s) => s.selectModel);
  const toggleTheme = useUiStore((s) => s.toggleTheme);
  const close = () => setOpen(false);

  return (
    <Command.Dialog open={open} onOpenChange={setOpen} label="Command palette" className="cmdk-root">
      <Command.Input placeholder="Type a command or search chats…" className="cmdk-input" />
      <Command.List className="cmdk-list">
        <Command.Empty>No results.</Command.Empty>
        <Command.Group heading="Chat">
          <Command.Item onSelect={() => { navigate("/chat/new"); close(); }}>New chat</Command.Item>
          <Command.Item onSelect={() => { navigate("/settings"); close(); }}>Open settings</Command.Item>
          <Command.Item onSelect={() => { toggleTheme(); close(); }}>Toggle theme</Command.Item>
        </Command.Group>
        <Command.Group heading="Model">
          {models.map((model) => (
            <Command.Item key={model} onSelect={() => { void selectModel(model); close(); }}>Switch to {model}</Command.Item>
          ))}
        </Command.Group>
        <RecentChatsGroup onNavigate={close} />
      </Command.List>
    </Command.Dialog>
  );
}
```

Global binding lives in a tiny hook, not a new dependency:

```ts
// frontend/src/hooks/useHotkey.ts
import { useEffect } from "react";

export function useHotkey(key: string, withMeta: boolean, handler: () => void) {
  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      const modifierMatches = withMeta ? event.ctrlKey || event.metaKey : !event.ctrlKey && !event.metaKey;
      if (event.key.toLowerCase() === key && modifierMatches) {
        event.preventDefault();
        handler();
      }
    };
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, [key, withMeta, handler]);
}
```

Mounted once in `AppShell`: `useHotkey("k", true, () => setCommandPaletteOpen(true))` and
`useHotkey("?", false, () => setShortcutsDialogOpen(true))`. `ShortcutsHelpDialog` is a static
Base UI `Dialog` listing every binding that already exists today (Enter/Shift+Enter in composer,
Escape-to-stop, arrow-key navigation in pickers) plus the new ones — the first time any of this
becomes *discoverable*, since the audit found no existing help surface at all.

### 6.7 Conversation search

Client-side only — `chats` is already fully loaded in `AppShell`'s scope, no backend change
needed. A single controlled input above the existing `chat-list`:

```tsx
// inside AppShell.tsx (delta) — or extracted to features/shell/ConversationSearch.tsx
const [query, setQuery] = useState("");
const filteredChats = useMemo(
  () => (query.trim() ? chats.filter((chat) => chat.title.toLowerCase().includes(query.trim().toLowerCase())) : chats),
  [chats, query],
);
```

```tsx
<input
  type="search"
  className="sidebar-search"
  placeholder="Search chats"
  value={query}
  onChange={(event) => setQuery(event.target.value)}
  aria-label="Search chats by title"
/>
```

Title-only substring match is deliberately the v1 scope — full-message search would need a
backend query over `messages.content`, which is a bigger, separate change (indexing strategy,
possibly the already-present-but-dormant `vectors` table) not required for the harness goals
here. Note it as a natural Phase-8+ follow-up, not part of this plan.

### 6.8 Transcript export

Also fully client-side — `ChatResponse.messages` is already the complete transcript in memory:

```ts
// frontend/src/features/chat/exportTranscript.ts
import type { ChatResponse } from "../../../../contracts/cortex-api";

export function exportTranscriptAsMarkdown(chat: ChatResponse): void {
  const lines = chat.messages?.map((m) => `### ${m.role === "user" ? "You" : "Cortex"}\n\n${m.content}`) ?? [];
  downloadBlob(`${chat.title}.md`, lines.join("\n\n---\n\n"), "text/markdown");
}

export function exportTranscriptAsJson(chat: ChatResponse): void {
  downloadBlob(`${chat.title}.json`, JSON.stringify(chat, null, 2), "application/json");
}

function downloadBlob(filename: string, content: string, mimeType: string): void {
  const url = URL.createObjectURL(new Blob([content], { type: mimeType }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename.replace(/[/\\:*?"<>|]/g, "_");
  anchor.click();
  URL.revokeObjectURL(url);
}
```

Exposed as a small `ExportTranscriptMenu` (Base UI `Menu`) from the chat header, two items:
"Export as Markdown" / "Export as JSON." No approval flow needed — this is a local file save the
user's browser/WebView2 handles natively, not one of Cortex's sandboxed execution paths.

### 6.9 Component consolidation on Base UI

The audit's clearest structural finding: `RoundedPicker` (`SettingsPanel.tsx:275-386`) and
`LocalModelMenu.tsx` are two independent, ~100-line reimplementations of the same accessible
combobox pattern (roving tabindex, `aria-haspopup="listbox"`, outside-click close, `Escape`,
Home/End). Collapse both onto one shared primitive:

```tsx
// frontend/src/shared/ui/Select.tsx
import { Select as BaseSelect } from "@base-ui/react/select";

type Option = { value: string; label: string };

type Props = {
  value: string | null;
  options: Option[];
  onChange: (value: string) => void | Promise<void>;
  placeholder?: string;
  disabled?: boolean;
  "aria-label": string;
};

export function Select({ value, options, onChange, placeholder, disabled, ...aria }: Props) {
  return (
    <BaseSelect.Root value={value ?? undefined} onValueChange={(v) => void onChange(v)} disabled={disabled}>
      <BaseSelect.Trigger className="select-trigger" {...aria}>
        <BaseSelect.Value placeholder={placeholder} />
      </BaseSelect.Trigger>
      <BaseSelect.Portal>
        <BaseSelect.Positioner>
          <BaseSelect.Popup className="select-popup">
            {options.map((option) => (
              <BaseSelect.Item key={option.value} value={option.value} className="select-item">
                <BaseSelect.ItemText>{option.label}</BaseSelect.ItemText>
              </BaseSelect.Item>
            ))}
          </BaseSelect.Popup>
        </BaseSelect.Positioner>
      </BaseSelect.Portal>
    </BaseSelect.Root>
  );
}
```

`SettingsPanel`'s theme/model/translation-model pickers and `LocalModelMenu`'s model picker both
become thin callers of `<Select>`. Net effect: ~180 lines of duplicated a11y logic deleted, one
place to fix if a screen-reader bug is ever found instead of two, and `shared/ui/Popover`
(§6.3) / `shared/ui/Dialog` (used by a rebuilt `AppShell` rename/delete dialog and
`ShortcutsHelpDialog`) come from the same package for free. Each wrapper gets its CSS from
`tokens.css` classes (`.select-trigger`, `.select-popup`, `.select-item`, …) styled identically
to how `RoundedPicker`'s DOM looks today — this is an implementation consolidation, not a visual
change, and existing component tests for `SettingsPanel`/`LocalModelMenu` should need only
selector updates, not new assertions, since keyboard behavior is equivalent (Base UI implements
the same WAI-ARIA listbox pattern by contract).

---

## 7. Design system deltas

`tokens.css` gains a handful of new custom properties, still only color/shadow/radius (no
scope-creep into a full spacing/type scale — that's a separate, unrelated refactor if ever
needed):

```css
:root {
  /* existing --bg/--surface*/--text*/--line*/--accent*/--success*/--danger*/--shadow-*/--radius-* unchanged */

  /* command palette + popovers */
  --overlay-scrim: rgba(16, 17, 18, 0.35);   /* flat scrim, no blur/frosted-glass per design rules */
  --popover-surface: var(--surface-raised);
  --popover-border: var(--line);

  /* syntax highlighting — mapped onto the existing palette, not a new theme */
  --code-keyword: var(--accent-strong);
  --code-string: var(--success);
  --code-comment: var(--text-faint);
  --code-number: var(--text-muted);
  --code-function: var(--text);
}
```

Explicit adherence check against `docs/UI_MODERNIZATION_AUDIT.md`'s rules, since every new
surface in §6 is exactly the kind of thing that visual scope-creep sneaks into:

- Command palette scrim: flat `--overlay-scrim`, **no** `backdrop-filter: blur(...)`.
- Parameter popover: same squared `--radius-md`, `--shadow-md`, `--line` border as
  `SettingsPanel`'s existing sections — not a new "card" visual language.
- Model info panel: plain text stats (`"8.0B params · Q4_K_M · 40,960 ctx"`), no colored badges
  per field, matching `ModelsPanel`'s existing chip style.
- Stats chip: `--text-faint`, same weight as the existing `<time>` element — a whisper, not a
  metric dashboard.
- Syntax highlighting: five colors, all already-existing palette entries reused via new
  `--code-*` aliases — no new hues introduced into either theme.

---

## 8. File-by-file migration map

| Current file | Action |
|---|---|
| `app/App.tsx` | Slims significantly: workspace-load bootstrapping stays; per-domain `useState` (chats, models, settings, executionTasks) moves into stores. Route→page dispatch stays here. |
| `app/ToastProvider.tsx` | Internals move to `stores/useUiStore.ts`; public `useToast()` hook signature unchanged, so no call sites elsewhere need to change. |
| `components/ChatPage.tsx` → `features/chat/ChatPage.tsx` | Streaming logic extracted to `hooks/useGenerationStream.ts`; message rendering extracted to `MessageList.tsx`/`MessageCard.tsx`; scroll-anchoring logic deleted in favor of `react-virtuoso`'s `followOutput`. |
| `components/MessageComposer.tsx` → `features/chat/MessageComposer.tsx` | Gains the `GenerationParamsPopover` trigger in its toolbar; otherwise unchanged (this component's existing behavior — Enter/Shift+Enter, IME guarding, attachment flow — is solid and untouched). |
| `components/SafeMarkdown.tsx` → `features/markdown/SafeMarkdown.tsx` | Gains `rehype-highlight` + `finalized` prop (§6.2). |
| `components/SettingsPanel.tsx` → `features/settings/SettingsPanel.tsx` | `RoundedPicker` usages replaced by `shared/ui/Select`; gains `top_p`/`top_k`/`repeat_penalty` fields and the stats-visibility toggle. |
| `components/LocalModelMenu.tsx` → `features/models/LocalModelMenu.tsx` | Rebuilt on `shared/ui/Select`; ~150 lines → ~40. |
| `components/ModelsPanel.tsx` → `features/models/ModelsPanel.tsx` | Gains `ModelInfoPanel` rows. |
| `components/AppShell.tsx` → `features/shell/AppShell.tsx` | Gains `ConversationSearch`, mounts `CommandPalette` + `ShortcutsHelpDialog`, rename/delete dialogs rebuilt on `shared/ui/Dialog`. |
| `components/ExecutionTaskTray.tsx`, `Onboarding.tsx`, `LocalSetup.tsx`, `SystemStatusCard.tsx`, `NavigationLink.tsx` | Move to `features/shell/`, no behavior change. |
| `components/MemoryPanel.tsx` | Move to `features/settings/`, no behavior change. |
| `lib/*` | Unchanged, stays at `lib/`. |
| *(new)* `stores/*.ts`, `hooks/*.ts`, `shared/ui/*.tsx`, `features/chat/{MessageList,MessageStats,GenerationParamsPopover,ExportTranscriptMenu}.tsx`, `features/models/ModelInfoPanel.tsx`, `features/command-palette/*.tsx` | New, per §5–§6. |

---

## 9. Phased delivery plan

Each phase ends with the full CI gate green (§3, item 3) before starting the next. Phases are
independently shippable — this isn't "big bang," it's incremental, and a team could stop after
any phase with a strictly-better app than today.

**Phase 0 — Scaffolding (no user-visible change).**
Install the five new dependencies. Create the folder structure from §5.1 as empty
barrels/re-exports. `git mv` existing components into their new feature folders with import
paths updated — nothing else changes. Exit criteria: build/typecheck/lint/test/e2e all pass with
zero behavior diff; this is purely a structural commit, reviewed as such.

**Phase 1 — State layer.**
Introduce the four Zustand stores (§5.2). Migrate `App.tsx`/`AppShell.tsx`/`ChatPage.tsx` off
`useState`+prop-drilling one domain at a time (models first — lowest risk, then settings, then
chats/generation last since it's the most involved). `ToastProvider` internals move to
`useUiStore` behind the same public hook. Exit criteria: existing component tests pass with
**no new assertions required** (this phase changes *how* state is held, not what the UI does) —
if a test needs new setup beyond swapping a fake prop for a store `act()` call, that's a signal
the migration changed behavior, which it shouldn't yet.

**Phase 2 — Streaming extraction & perf.**
Extract `useGenerationStream` (§5.3) and `useRafBatchedText` (§5.4). Depends on Phase 1's
`useChatStore` existing. This is the highest-risk phase for regressions — the reconnect/backoff/
staleness-guard logic in `ChatPage.consumeJob` is subtle and already has 14 tests
(`ChatPage.test.tsx`) covering it; port those tests to exercise the hook directly (`renderHook`)
in addition to the existing page-level tests, don't just delete and rewrite. Exit criteria: all
14 existing streaming-related test cases pass unchanged in behavior, plus new hook-level tests
for the extracted reconnect logic.

**Phase 3 — Virtualized transcript + syntax highlighting.**
Ship `MessageList` (§6.1) and the `finalized`-aware `SafeMarkdown` (§6.2). Depends on Phase 2 (
`useRafBatchedText` should land first so highlighting doesn't compound the per-token re-render
cost). New e2e spec for the >40-message virtualized path. Exit criteria: existing e2e transcript
assertions (rich-markdown rendering, table overflow) still pass against `.transcript-plain`
(below-threshold) *and* pass against the new virtualized path in the new spec.

**Phase 4 — Harness controls: parameters + stats.**
Backend: extend `GenerationSettings`, add `GenerationOptionsOverride`, merge logic in
`_generation_snapshot`, stats extraction in `llm.py`, `generation_stats_json` migration,
`fake_ollama.py` fixture updates (§6.3, §6.4). Regenerate contracts. Frontend:
`GenerationParamsPopover`, `MessageStats`, Settings → AI Model gains `top_p`/`top_k`/
`repeat_penalty` sliders + stats-visibility toggle. This is the phase with the most backend
surface area — do it as its own reviewable unit, backend-then-frontend, contract regeneration as
the hard boundary between the two halves. Exit criteria: `pytest` covers the merge-precedence
logic (override present vs. absent vs. partially set) and stats extraction (present vs. absent
in the Ollama response); Vitest covers the popover's reset-to-defaults and per-thread scoping.

**Phase 5 — Model intelligence.**
Backend: extend `ModelService.capabilities()` extraction, `InstalledModel` schema fields (§6.5).
Frontend: `ModelInfoPanel`. Low risk, additive-only, no existing behavior touched. Exit criteria:
`fake_ollama.py`'s `/api/show` fixture models `details`/`model_info`; a pytest case asserts
`context_length` derivation from the `family`-prefixed `model_info` key.

**Phase 6 — Productivity layer.**
Command palette + shortcuts help (§6.6), conversation search (§6.7), transcript export (§6.8).
All independent of each other — can ship in any order or in parallel once Phase 1's `useUiStore`
exists. Prompt template library is explicitly **optional/stretch** here: start with a
client-only (`localStorage`) implementation; only add backend persistence if usage shows it's
wanted, since unlike memory/settings there's no existing precedent for "how Cortex persists a
small user-authored list" beyond the memory-JSON pattern this would have to justify reusing.

**Phase 7 — Component consolidation.**
`shared/ui/Select`/`Popover`/`Dialog`/`Tooltip` on Base UI (§6.9); migrate `RoundedPicker` and
`LocalModelMenu` onto `Select`; migrate `AppShell`'s rename/delete dialogs onto `Dialog`. Do this
*after* the params popover and command palette (Phases 4/6) have already proven the Base UI
integration works, so the higher-risk "replace a widely-used, well-tested existing component"
work benefits from that experience. Exit criteria: `SettingsPanel.test.tsx`/
`LocalModelMenu.test.tsx` pass with only selector updates (not new assertions) — confirming the
consolidation didn't change keyboard/ARIA behavior, only implementation.

**Phase 8 — Hardening & polish.**
Full accessibility re-audit against §3 item 6 (screen-reader pass over every new surface:
palette, popover, model info panel, search); WebView2 cold-start timing check with the five new
dependencies installed (bundle-size budget, not just "does it still launch"); update
`README.md`'s feature list and `docs/UI_MODERNIZATION_AUDIT.md`'s "what was audited" table to
reflect the new surfaces, so that document stays the accurate source of truth it is today.

---

## 10. Testing strategy updates

- **Preserve the existing pattern.** Real `CortexApi` + faked `fetch`, no MSW, no snapshot
  testing — this plan adds to that pattern, it doesn't replace it.
- **Zustand stores get their own unit tests** (`stores/useChatStore.test.ts`), independent of any
  component — assert `appendContentToken` ignores stale `jobId`s, `endGeneration` resets to
  idle, etc. This is new coverage the current `useState`-in-component approach couldn't isolate.
- **`useGenerationStream` gets `renderHook` tests** ported from `ChatPage.test.tsx`'s existing
  streaming assertions (Phase 2).
- **Playwright additions:** virtualized-transcript spec (Phase 3), command-palette spec (Phase
  6, `Ctrl+K` → type → select → assert navigation), generation-params spec (Phase 4, set an
  override, assert the request payload via `page.route()` interception includes it).
  `playwright.config.ts` stays Chromium-only — no scope change there.
  Given the CI job is `--workers=1` for e2e already, each new spec adds to that budget's runtime;
  keep new specs focused (one flow each) rather than broad, matching the existing four specs'
  granularity.
- **Contract regeneration is a hard gate, not a suggestion.** Every phase touching `schemas.py`
  ends its PR with `python tools/generate_contracts.py` run and the diff committed — CI's
  `git diff --exit-code` on `contracts/*` will fail otherwise, exactly as it does today for any
  other backend contributor.
- **`fake_ollama.py` fidelity matters more after this plan than before.** Phases 4 and 5 both
  depend on the fake actually modeling fields (`eval_count`, `/api/show`'s `details`) it
  currently omits — treat updating this fixture as part of the backend work in those phases, not
  an afterthought, since without it the new pytest coverage would be exercising `None`-branches
  only.

---

## 11. Risk register

| Risk | Phase | Mitigation |
|---|---|---|
| Streaming extraction (§5.3) subtly changes reconnect/backoff timing or the `sessionStorage`-persisted active-job resume behavior. | 2 | Port all 14 existing `ChatPage.test.tsx` streaming assertions to the hook before deleting the inline version; don't rewrite from a blank page. |
| `react-virtuoso` changes transcript DOM structure enough to break existing e2e selectors. | 3 | Keep the plain (`.transcript-plain`) path for the common case (<40 messages) so every existing e2e fixture is unaffected; only the new large-transcript spec exercises the virtualized path. |
| RAF-batching (§5.4) introduces a one-frame (~16ms) display lag per token. | 2–3 | Imperceptible at any realistic local-model token rate; verify manually in Phase 3 against the fastest available local model before shipping. |
| `shiki`-class WASM cold-start risk was avoided by picking `rehype-highlight`, but any future highlighter swap must re-check this against WebView2 startup budget. | 3 | Documented explicitly here so it isn't re-litigated or silently regressed by a later "let's use shiki for better themes" change. |
| Per-request options override (§6.3) could let a malformed/out-of-range value reach Ollama if validation is duplicated instead of shared. | 4 | Validation bounds live once, on the Pydantic field definitions shared by `GenerationSettings` and `GenerationOptionsOverride` — not restated ad hoc in the merge function. |
| New `generation_stats_json`/model-detail fields silently stay `None` forever if `fake_ollama.py` isn't updated, masking a real integration bug until manual testing against real Ollama. | 4, 5 | Explicit exit criteria in §9 require fixture updates as part of the phase, not deferred. |
| Base UI is young post-1.0 (renamed/re-released Dec 2025) — API surface could still shift. | 7 | Scheduled last, after two lower-stakes integrations (Popover in Phase 4, Dialog/Menu in Phase 6) have already validated the library in this codebase before the highest-blast-radius consolidation (replacing `LocalModelMenu`/`RoundedPicker`) happens. |
| Five new dependencies increase the PyInstaller-packaged bundle size and WebView2 cold-start time. | 8 | Explicit bundle-size/cold-start check in the hardening phase, not assumed fine. |
| Adding `top_p`/`top_k`/`repeat_penalty` as new global settings fields changes `CortexSettings.generation`'s shape — existing installs read this from the single-row JSON blob. | 4 | Pydantic model with `Field(default=...)` on new fields deserializes missing keys as defaults automatically — no migration needed, verified against the existing additive-JSON-settings pattern already used for every prior settings field. |

---

## 12. Non-goals

Explicitly out of scope for this plan — noted so scope doesn't silently creep during
implementation:

- **No React Router / file-based routing.** `lib/navigation.ts` is ~80 lines, correct, and
  tested. Replacing it would be churn with no user-facing benefit.
- **No Tailwind or CSS-in-JS.** `tokens.css` stays the single source of visual truth.
- **No SSR, no bundler change.** Stays Vite 8 + React 19 SPA.
- **No full-message conversation search** (only chat-title search, §6.7) — full-text search over
  message content is a separate, larger effort (indexing strategy, possibly reviving the dormant
  `vectors` table) not required to make Cortex a harness.
- **No side-by-side multi-model comparison UI.** Table-stakes in some competitor apps (§2), but
  it's a genuinely new interaction model (parallel generation jobs, split-view layout) rather
  than an extension of existing patterns — worth its own follow-up plan once Phases 0–8 have
  landed, not bundled into this one.
- **No changes to the execution/sandboxing subsystem, packaging scripts, or launcher.** This plan
  touches `frontend/` and the narrow backend slice in §6.3–§6.5 only.
- **No telemetry, analytics, or usage tracking** of any kind — stats added in §6.4 are rendered
  client-side from data already returned per-request; nothing is aggregated, stored centrally, or
  sent anywhere beyond the existing local SQLite row.
- **No change to the two hardcoded-options call sites** (translation, chat-title generation,
  `llm.py:558`/`llm.py:719`) that already bypass global settings — noted as a pre-existing quirk,
  not fixed here, since it's orthogonal to per-chat overrides for the main generation path.

---

## 13. Definition of done

The refactor is complete when, without regressing anything in §2's "current state" table:

1. A user can open a chat, adjust temperature/top_p/top_k/repeat_penalty/context-window for that
   conversation only, see live tok/s and duration on each response, and reset to global defaults
   — all without leaving the composer.
2. A user can see what a model actually is (parameter size, quantization, context length) before
   choosing it.
3. A 200-message conversation scrolls and streams smoothly (virtualized, RAF-batched), with
   syntax-highlighted code once each response finishes streaming.
4. `Ctrl+K` opens a command palette covering new-chat/settings/theme/model-switch/recent-chats;
   `?` opens a shortcuts reference — the first discoverable list of keyboard bindings in the app.
5. A user can search chats by title, and export any transcript to Markdown or JSON.
6. Every duplicated accessible-widget implementation (two comboboxes) is now one shared,
   Base-UI-backed primitive.
7. `tokens.css`'s design vocabulary — flat surfaces, one accent, text-led hierarchy, no
   glass/gradient/card-grid — is unbroken across every new surface.
8. The full CI gate (typecheck → lint → Vitest → Playwright e2e → contract-diff → build → native
   package build + signature check) passes exactly as it does today, with proportionally more
   coverage, not less.
