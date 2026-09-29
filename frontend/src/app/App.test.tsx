import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import { resolveRuntimeAvailability } from "./runtimeAvailability";
import { CortexApi } from "../api/client";
import { useChatStore } from "../stores/useChatStore";
import { useSettingsStore } from "../stores/useSettingsStore";
import { useModelStore } from "../stores/useModelStore";
import { ToastProvider } from "./ToastProvider";

describe("App", () => {
  /**
   * Open Settings and wait for its lazily-imported panel.
   *
   * SettingsPanel is a React.lazy dynamic import, so the first click renders a
   * Suspense fallback while vitest resolves and transforms the module. Under a
   * full 31-file run that resolution can outlast findBy's 1s default, which
   * made this an intermittent "Unable to find role=button name=AI Model".
   */
  const openModelSettings = async (user: ReturnType<typeof userEvent.setup>) => {
    await user.click(screen.getByRole("link", { name: "Settings" }));
    await user.click(
      await screen.findByRole("button", { name: "AI Model" }, { timeout: 10_000 }),
    );
  };

  const respond = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

  /** The routes every workspace load needs, for tests that only care about session handling. */
  const workspaceRoute = (url: string): Response | null => {
    if (url.endsWith("/system")) return respond({ status: "ok", preview: true, session_required: true, started_at: "2026-07-21T18:00:00Z" });
    if (url.endsWith("/chat-groups")) return respond([]);
    if (url.endsWith("/chats")) return respond([]);
    if (url.endsWith("/settings")) return respond({ settings: { models: { chat: null, title: null }, appearance: { theme: "dark" } } });
    if (url.endsWith("/models")) return respond({ required_models: [], optional_models: [], installed_models: [], connection: { success: true, status: "connected", message: "Ready" } });
    return null;
  };

  /**
   * Record whether the onboarding boundary is ever rendered. Asserting only the
   * end state cannot tell "renewed in place" from "left and came back".
   */
  const watchOnboarding = () => {
    let shown = false;
    const check = () => { if (document.querySelector(".onboarding")) shown = true; };
    const observer = new MutationObserver(check);
    observer.observe(document.body, { childList: true, subtree: true });
    return { wasShown: () => shown, stop: () => observer.disconnect() };
  };

  const callsTo = (fetcher: { mock: { calls: readonly (readonly unknown[])[] } }, suffix: string) =>
    fetcher.mock.calls.filter(([input]) => String(input).endsWith(suffix));

  afterEach(() => {
    useModelStore.getState().setLlamacppStatus(null);
    // A generation kept across a 401 is left tracked on purpose; do not let it
    // leak into the next test.
    const { generation, endGeneration } = useChatStore.getState();
    if (generation.jobId) endGeneration(generation.jobId);
    window.sessionStorage.clear();
    window.history.replaceState({}, "", "/");
  });

  it("starts without exposing a manual sign-in boundary", () => {
    window.sessionStorage.clear();
    render(<App api={new CortexApi("/api/v1", window.fetch.bind(window))} />);
    expect(screen.getByRole("heading", { name: "Start local workspace" })).toBeInTheDocument();
    expect(screen.queryByLabelText(/token/i)).not.toBeInTheDocument();
  });

  it("scrubs a bootstrap query credential before rendering an existing session", async () => {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    window.history.replaceState({}, "", "/?bootstrap=query-secret&view=compact#pane=chat");
    const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url.endsWith("/system")) return json({ status: "ok", preview: true, session_required: true, started_at: "2026-07-21T18:00:00Z" });
      if (url.endsWith("/chat-groups")) return json([]);
      if (url.endsWith("/chats")) return json([]);
      if (url.endsWith("/settings")) return json({ settings: { models: { chat: null, title: null }, appearance: { theme: "dark" } } });
      if (url.endsWith("/memories")) return json({ memos: [] });
      if (url.endsWith("/models")) return json({ required_models: [], optional_models: [], installed_models: [], connection: { success: true, status: "connected", message: "Ready" } });
      return json({ detail: "Unexpected test route." }, 404);
    });

    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);

    expect(await screen.findByRole("heading", { name: "New thread" })).toBeVisible();
    expect(window.location.search).toBe("?view=compact");
    expect(window.location.hash).toBe("#pane=chat");
    expect(window.location.href).not.toContain("query-secret");
    expect(fetcher.mock.calls.some(([input]) => String(input).endsWith("/session/exchange"))).toBe(false);
  });

  it("does not reuse a consumed bootstrap token after the local session expires", async () => {
    window.history.replaceState({}, "", "/?bootstrap=desktop-handoff");
    const fetcher = vi.fn<(input: RequestInfo | URL) => Promise<Response>>(async (input) => {
      const url = String(input);
      if (url.endsWith("/session/exchange")) {
        return new Response(JSON.stringify({
          session_token: "local-session",
          expires_at: "2026-07-20T17:00:00Z",
          token_type: "bearer",
        }), { headers: { "Content-Type": "application/json" } });
      }
      return new Response(JSON.stringify({ detail: "Local session expired." }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      });
    });

    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher as unknown as typeof fetch)} /></ToastProvider>);

    await waitFor(() => expect(screen.getByRole("heading", { name: "Opening local workspace" })).toBeVisible());
    expect(fetcher.mock.calls.filter(([input]) => String(input).endsWith("/session/exchange"))).toHaveLength(1);
    expect(screen.queryByLabelText(/token/i)).not.toBeInTheDocument();
  });

  it("renews an expired session without leaving the workspace", async () => {
    // A 401 used to swap the whole workspace for the onboarding card and mount
    // it again from scratch -- a spinner and a second load of every list -- to
    // do what is a sub-second loopback exchange. The client now renews in place.
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    window.history.replaceState({}, "", "/#handoff=desktop-handoff");
    let expireSystem = true;
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/system") && expireSystem) {
        expireSystem = false;
        return respond({ detail: "Local session expired." }, 401);
      }
      if (url.endsWith("/memories")) return respond({ memos: [] });
      if (url.endsWith("/session/handoff")) {
        expect(new Headers(init?.headers).get("X-Cortex-Handoff")).toBe("desktop-handoff");
        return respond({ bootstrap_token: "fresh-bootstrap", expires_at: "2026-07-21T18:05:00Z" });
      }
      if (url.endsWith("/session/exchange")) return respond({ session_token: "recovered-session", expires_at: "2026-07-21T19:00:00Z", token_type: "bearer" });
      return workspaceRoute(url) ?? respond({ detail: "Unexpected test route." }, 404);
    });
    const onboarding = watchOnboarding();

    try {
      render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);

      expect(await screen.findByRole("heading", { name: "New thread" })).toBeVisible();
      expect(callsTo(fetcher, "/session/handoff")).toHaveLength(1);
      expect(callsTo(fetcher, "/session/exchange")).toHaveLength(1);
      // Loaded once. A remount would have loaded the chat list a second time.
      expect(callsTo(fetcher, "/chats")).toHaveLength(1);
      expect(onboarding.wasShown()).toBe(false);
      expect(window.sessionStorage.getItem("cortex.session.token")).toBe("recovered-session");
    } finally {
      onboarding.stop();
    }
  });

  it("renews once when every request of the first load has expired", async () => {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    window.sessionStorage.setItem("cortex.session.handoff", "desktop-handoff");
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/session/handoff")) return respond({ bootstrap_token: "fresh-bootstrap", expires_at: "2026-07-21T18:05:00Z" });
      if (url.endsWith("/session/exchange")) return respond({ session_token: "recovered-session", expires_at: "2026-07-21T19:00:00Z", token_type: "bearer" });
      if (new Headers(init?.headers).get("Authorization") === "Bearer local-session") {
        return respond({ detail: "Local session expired." }, 401);
      }
      if (url.endsWith("/memories")) return respond({ memos: [] });
      return workspaceRoute(url) ?? respond({ detail: "Unexpected test route." }, 404);
    });
    const onboarding = watchOnboarding();

    try {
      render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);

      expect(await screen.findByRole("heading", { name: "New thread" })).toBeVisible();
      expect(callsTo(fetcher, "/session/handoff")).toHaveLength(1);
      expect(callsTo(fetcher, "/session/exchange")).toHaveLength(1);
      expect(onboarding.wasShown()).toBe(false);
    } finally {
      onboarding.stop();
    }
  });

  it("recovers the session after a reload that dropped the launch fragment", async () => {
    // The launcher delivers the handoff secret once, in the URL fragment, and
    // the app scrubs it. It used to live only in React state, so any reload --
    // including the error boundary's own "Reload workspace" button -- lost it,
    // and the next session expiry left onboarding with no retry that could
    // ever succeed. The user had to quit and relaunch Cortex.
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    window.sessionStorage.setItem("cortex.session.handoff", "desktop-handoff");
    window.history.replaceState({}, "", "/");
    const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
    let expireSystem = true;
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/system") && expireSystem) {
        expireSystem = false;
        return json({ detail: "Local session expired." }, 401);
      }
      if (url.endsWith("/system")) return json({ status: "ok", preview: true, session_required: true, started_at: "2026-07-21T18:00:00Z" });
      if (url.endsWith("/chat-groups")) return json([]);
      if (url.endsWith("/chats")) return json([]);
      if (url.endsWith("/settings")) return json({ settings: { models: { chat: null, title: null }, appearance: { theme: "dark" } } });
      if (url.endsWith("/memories")) return json({ memos: [] });
      if (url.endsWith("/models")) return json({ required_models: [], optional_models: [], installed_models: [], connection: { success: true, status: "connected", message: "Ready" } });
      if (url.endsWith("/session/handoff")) {
        expect(new Headers(init?.headers).get("X-Cortex-Handoff")).toBe("desktop-handoff");
        return json({ bootstrap_token: "fresh-bootstrap", expires_at: "2026-07-21T18:05:00Z" });
      }
      if (url.endsWith("/session/exchange")) return json({ session_token: "recovered-session", expires_at: "2026-07-21T19:00:00Z", token_type: "bearer" });
      return json({ detail: "Unexpected test route." }, 404);
    });

    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);

    await waitFor(() => expect(fetcher.mock.calls.some(([input]) => String(input).endsWith("/session/handoff"))).toBe(true));
    expect(await screen.findByRole("heading", { name: "New thread" })).toBeVisible();
    expect(window.sessionStorage.getItem("cortex.session.token")).toBe("recovered-session");
  });

  it("keeps the handoff secret from the launch fragment for later reloads", async () => {
    window.history.replaceState({}, "", "/#bootstrap=launch-token&handoff=desktop-handoff");
    const json = (body: unknown) => new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url.endsWith("/session/exchange")) return json({ session_token: "session-1", expires_at: "2026-07-21T19:00:00Z", token_type: "bearer" });
      if (url.endsWith("/system")) return json({ status: "ok", preview: true, session_required: true, started_at: "2026-07-21T18:00:00Z" });
      if (url.endsWith("/chat-groups")) return json([]);
      if (url.endsWith("/chats")) return json([]);
      if (url.endsWith("/settings")) return json({ settings: { models: { chat: null, title: null }, appearance: { theme: "dark" } } });
      if (url.endsWith("/memories")) return json({ memos: [] });
      if (url.endsWith("/models")) return json({ required_models: [], optional_models: [], installed_models: [], connection: { success: true, status: "connected", message: "Ready" } });
      return json({ detail: "Unexpected test route." });
    });

    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);

    expect(await screen.findByRole("heading", { name: "New thread" })).toBeVisible();
    expect(window.sessionStorage.getItem("cortex.session.handoff")).toBe("desktop-handoff");
    // The visible URL is still scrubbed.
    expect(window.location.href).not.toContain("handoff=");
  });

  it("returns to onboarding but keeps the running generation when its stream session cannot be renewed", async () => {
    // No handoff secret: nothing can renew the session, so the app has to go
    // back to onboarding. The job is still running on the backend, though, and
    // the transcript must not lose it.
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    window.sessionStorage.setItem("cortex.active.generation", JSON.stringify({
      jobId: "job-expired",
      threadId: "thread-expired",
      lastEventId: 3,
    }));
    window.history.replaceState({}, "", "/chat/thread-expired");
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url.endsWith("/chats")) return respond([{ id: "thread-expired", title: "Interrupted", timestamp: "2026-07-21T18:00:00Z" }]);
      if (url.endsWith("/chats/thread-expired")) return respond({ id: "thread-expired", title: "Interrupted", timestamp: "2026-07-21T18:00:00Z", revision: 1, messages: [] });
      if (url.endsWith("/settings")) return respond({ settings: { models: { chat: "model-a", title: null }, appearance: { theme: "dark" } } });
      if (url.endsWith("/memories")) return respond({ memos: [] });
      if (url.endsWith("/models")) return respond({ required_models: [], optional_models: [], installed_models: ["model-a"], connection: { success: true, status: "connected", message: "Ready" } });
      if (url.endsWith("/generations/job-expired/events")) return respond({ detail: "Local session expired." }, 401);
      return workspaceRoute(url) ?? respond({ detail: "Unexpected test route." }, 404);
    });

    // This test targets recovery from an expired stream session, not the
    // scheduler-dependent time needed to transform the lazy chat route.
    await import("../features/chat/ChatPage");
    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);

    // Wait for the recovery stream's 401 to notify the session listener and
    // return the app to its unauthenticated boundary. This explicit timeout
    // is independent of (and was previously shorter than headroom under) the
    // global testTimeout in vitest.config.ts -- under full-suite load the two
    // could elapse at effectively the same moment, failing the test even
    // though the app was still working correctly.
    expect(await screen.findByRole("heading", { name: "Start local workspace" }, { timeout: 12_000 })).toBeVisible();
    expect(callsTo(fetcher, "/generations/job-expired/events")).toHaveLength(1);
    expect(window.sessionStorage.getItem("cortex.session.token")).toBeNull();
    expect(JSON.parse(window.sessionStorage.getItem("cortex.active.generation") ?? "null")).toMatchObject({ jobId: "job-expired" });
    expect(useChatStore.getState().generation).toMatchObject({ jobId: "job-expired", threadId: "thread-expired" });
    // Explicit ceiling: this waits on the lazily-loaded chat route and a real
    // 401 round trip, and the default 15s has been outrun on a busy machine.
  }, 45_000);

  it("resumes the running generation once the session is back after a failed renewal", async () => {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    window.sessionStorage.setItem("cortex.session.handoff", "desktop-handoff");
    window.sessionStorage.setItem("cortex.active.generation", JSON.stringify({
      jobId: "job-expired",
      threadId: "thread-expired",
      lastEventId: 3,
    }));
    window.history.replaceState({}, "", "/chat/thread-expired");
    const streamEvents = [
      { event_id: 1, event: "generation.content_delta", job_id: "job-expired", thread_id: "thread-expired", data: { delta: "Recovered answer" } },
      { event_id: 2, event: "generation.completed", job_id: "job-expired", thread_id: "thread-expired", data: {} },
    ];
    let handoffs = 0;
    let releaseHandoff!: () => void;
    const secondHandoff = new Promise<void>((resolve) => { releaseHandoff = resolve; });
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/session/handoff")) {
        handoffs += 1;
        // The launcher is busy for the client's own renewal; the app's
        // reconnect from onboarding is held so the in-between state is visible.
        if (handoffs === 1) return respond({ detail: "Launcher busy." }, 503);
        await secondHandoff;
        return respond({ bootstrap_token: "fresh-bootstrap", expires_at: "2026-07-21T18:05:00Z" });
      }
      if (url.endsWith("/session/exchange")) return respond({ session_token: "recovered-session", expires_at: "2026-07-21T19:00:00Z", token_type: "bearer" });
      if (url.endsWith("/generations/job-expired/events")) {
        if (new Headers(init?.headers).get("Authorization") === "Bearer local-session") {
          return respond({ detail: "Local session expired." }, 401);
        }
        return new Response(
          streamEvents.map((event) => `id: ${event.event_id}\ndata: ${JSON.stringify(event)}\n\n`).join(""),
          { status: 200, headers: { "Content-Type": "text/event-stream" } },
        );
      }
      if (url.endsWith("/chats")) return respond([{ id: "thread-expired", title: "Interrupted", timestamp: "2026-07-21T18:00:00Z" }]);
      if (url.endsWith("/chats/thread-expired")) return respond({ id: "thread-expired", title: "Interrupted", timestamp: "2026-07-21T18:00:00Z", revision: 1, messages: [] });
      if (url.endsWith("/settings")) return respond({ settings: { models: { chat: "model-a", title: null }, appearance: { theme: "dark" } } });
      if (url.endsWith("/memories")) return respond({ memos: [] });
      if (url.endsWith("/models")) return respond({ required_models: [], optional_models: [], installed_models: ["model-a"], connection: { success: true, status: "connected", message: "Ready" } });
      return workspaceRoute(url) ?? respond({ detail: "Unexpected test route." }, 404);
    });

    await import("../features/chat/ChatPage");
    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);

    // The first attach is refused and cannot be renewed yet, so the app falls
    // back to onboarding -- with the generation still tracked, not discarded.
    expect(await screen.findByRole("heading", { name: "Start local workspace" }, { timeout: 12_000 })).toBeVisible();
    expect(JSON.parse(window.sessionStorage.getItem("cortex.active.generation") ?? "null")).toMatchObject({ jobId: "job-expired" });
    expect(useChatStore.getState().generation).toMatchObject({ jobId: "job-expired", threadId: "thread-expired" });

    // The session comes back; the workspace re-attaches and finishes the job.
    releaseHandoff();
    await waitFor(() => expect(callsTo(fetcher, "/generations/job-expired/events")).toHaveLength(2), { timeout: 12_000 });
    await waitFor(() => expect(useChatStore.getState().generation).toMatchObject({ jobId: null, phase: "idle" }), { timeout: 12_000 });
    expect(window.sessionStorage.getItem("cortex.active.generation")).toBeNull();
    expect(window.sessionStorage.getItem("cortex.session.token")).toBe("recovered-session");
    expect(handoffs).toBe(2);
    // Three bounded waits in sequence (12s each), so the default 15s cannot cover it.
  }, 45_000);

  it("returns to onboarding when a model job stream reports an expired session", async () => {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/system")) return json({ status: "ok", preview: true, session_required: true, started_at: "2026-07-21T18:00:00Z" });
      if (url.endsWith("/chat-groups")) return json([]);
      if (url.endsWith("/chats")) return json([]);
      if (url.endsWith("/settings")) return json({ settings: { models: { chat: null, title: null }, appearance: { theme: "dark" } } });
      if (url.endsWith("/memories")) return json({ memos: [] });
      if (url.endsWith("/jobs/models") && init?.method === "POST") return json({ job_id: "model-job-401", kind: "models", status: "queued" }, 202);
      if (url.endsWith("/jobs/model-job-401/events")) return json({ detail: "Local session expired." }, 401);
      if (url.endsWith("/models")) return json({ required_models: [], optional_models: [], installed_models: [], connection: { success: true, status: "connected", message: "Ready" } });
      return json({ detail: "Unexpected test route." }, 404);
    });

    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);

    expect(await screen.findByRole("heading", { name: "New thread" })).toBeVisible();
    const user = userEvent.setup();
    await openModelSettings(user);
    await user.click(screen.getByRole("button", { name: "Rescan local models" }));

    expect(await screen.findByRole("heading", { name: "Start local workspace" })).toBeVisible();
    expect(window.sessionStorage.getItem("cortex.session.token")).toBeNull();
  });

  it("a stale 401 from a request sent under the previous token does not restart the re-exchanged session", async () => {
    // A slow request sent under the old token fails after another request has
    // already renewed the session. Answering that 401 with a second handoff,
    // exchange and remount is what the hand-written branches used to do.
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    window.sessionStorage.setItem("cortex.session.handoff", "desktop-handoff");
    let releaseSlowGroups!: (response: Response) => void;
    const slowGroups = new Promise<Response>((resolve) => { releaseSlowGroups = resolve; });
    const group = { id: "group-filed", name: "Filed", collapsed: false, created_at: "2026-07-21T18:00:00Z", updated_at: "2026-07-21T18:00:00Z" };
    let groupRequests = 0;
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/session/handoff")) return respond({ bootstrap_token: "fresh-bootstrap", expires_at: "2026-07-21T18:05:00Z" });
      if (url.endsWith("/session/exchange")) return respond({ session_token: "recovered-session", expires_at: "2026-07-21T19:00:00Z", token_type: "bearer" });
      if (url.endsWith("/chat-groups")) {
        groupRequests += 1;
        return groupRequests === 1 ? slowGroups : respond([group]);
      }
      if (url.endsWith("/settings") && init?.method === "PUT") {
        // The theme toggle is what finds the session expired.
        if (new Headers(init.headers).get("Authorization") === "Bearer local-session") {
          return respond({ detail: "Local session expired." }, 401);
        }
        return respond({ settings: { models: { chat: null, title: null }, appearance: { theme: "light" } } });
      }
      if (url.endsWith("/memories")) return respond({ memos: [] });
      return workspaceRoute(url) ?? respond({ detail: "Unexpected test route." }, 404);
    });
    const onboarding = watchOnboarding();

    try {
      render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);
      expect(await screen.findByRole("heading", { name: "New thread" })).toBeVisible();

      const user = userEvent.setup();
      await user.keyboard("{Control>}k{/Control}");
      await user.click(await screen.findByText("Toggle theme"));
      expect(await screen.findByText("Settings saved.")).toBeVisible();
      expect(callsTo(fetcher, "/session/handoff")).toHaveLength(1);

      // The group list was requested with the old token and is still pending.
      await act(async () => {
        releaseSlowGroups(respond({ detail: "Local session expired." }, 401));
      });

      await waitFor(() => expect(useChatStore.getState().groups.map((item) => item.id)).toEqual(["group-filed"]));
      expect(callsTo(fetcher, "/session/handoff")).toHaveLength(1);
      expect(callsTo(fetcher, "/session/exchange")).toHaveLength(1);
      expect(onboarding.wasShown()).toBe(false);
      expect(window.sessionStorage.getItem("cortex.session.token")).toBe("recovered-session");
    } finally {
      onboarding.stop();
    }
  });

  it("opens the workspace when the memory store fails to load", async () => {
    // Memories are read only by Settings. An unreadable memory store used to
    // fail the workspace load and put "Workspace unavailable" between the user
    // and chat -- the one place they could still get on.
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    let memoryFails = true;
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url.endsWith("/memories")) {
        return memoryFails
          ? respond({ detail: "Cortex could not load memories." }, 500)
          : respond({ memos: ["Likes tea"] });
      }
      return workspaceRoute(url) ?? respond({ detail: "Unexpected test route." }, 404);
    });

    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);

    expect(await screen.findByRole("heading", { name: "New thread" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Workspace unavailable" })).not.toBeInTheDocument();
    expect(callsTo(fetcher, "/memories")).toHaveLength(0);

    const user = userEvent.setup();
    await user.click(screen.getByRole("link", { name: "Settings" }));
    await user.click(await screen.findByRole("button", { name: "Memory" }, { timeout: 10_000 }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Cortex could not load memories.");
    // Nothing loaded, so nothing editable that could be mistaken for the store.
    expect(screen.queryByText("No permanent memories stored.")).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "New memory" })).not.toBeInTheDocument();

    memoryFails = false;
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("textbox", { name: "Memory 1" })).toHaveValue("Likes tea");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("does not let a slow memory refresh overwrite a memory saved meanwhile", async () => {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    let releaseRefresh!: (response: Response) => void;
    const slowRefresh = new Promise<Response>((resolve) => { releaseRefresh = resolve; });
    let memoryReads = 0;
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/memories") && init?.method === "POST") return respond({ memos: ["First", "Second"] });
      if (url.endsWith("/memories")) {
        memoryReads += 1;
        return memoryReads === 1 ? respond({ memos: ["First"] }) : slowRefresh;
      }
      return workspaceRoute(url) ?? respond({ detail: "Unexpected test route." }, 404);
    });

    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);
    expect(await screen.findByRole("heading", { name: "New thread" })).toBeVisible();

    const user = userEvent.setup();
    await user.click(screen.getByRole("link", { name: "Settings" }));
    await user.click(await screen.findByRole("button", { name: "Memory" }, { timeout: 10_000 }));
    expect(await screen.findByRole("textbox", { name: "Memory 1" })).toHaveValue("First");

    // Leave Settings and come back: the list is refreshed, and while that read
    // is slow the last list stays on screen and stays editable.
    await user.click(screen.getByRole("button", { name: "Close settings" }));
    await user.click(await screen.findByRole("link", { name: "Settings" }));
    await user.click(await screen.findByRole("button", { name: "Memory" }, { timeout: 10_000 }));
    expect(await screen.findByRole("textbox", { name: "Memory 1" })).toHaveValue("First");
    await waitFor(() => expect(memoryReads).toBe(2));

    await user.type(screen.getByRole("textbox", { name: "New memory" }), "Second");
    await user.click(screen.getByRole("button", { name: "Add memory" }));
    expect(await screen.findByRole("textbox", { name: "Memory 2" })).toHaveValue("Second");

    // The older read finishes last and must not erase what was just saved.
    await act(async () => { releaseRefresh(respond({ memos: ["First"] })); });
    expect(screen.getByRole("textbox", { name: "Memory 2" })).toHaveValue("Second");
  });

  it("opens the workspace when the model service is unavailable", async () => {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url.endsWith("/system")) return json({ status: "ok", preview: true, session_required: true, started_at: "2026-07-21T18:00:00Z" });
      if (url.endsWith("/chat-groups")) return json([]);
      if (url.endsWith("/chats")) return json([]);
      if (url.endsWith("/settings")) return json({ settings: { models: { chat: null, title: null }, appearance: { theme: "dark" } } });
      if (url.endsWith("/memories")) return json({ memos: [] });
      if (url.endsWith("/models")) return json({ required_models: [], optional_models: [], installed_models: [], connection: { success: false, status: "error", message: "Ollama is not running." } });
      return json({ detail: "Unexpected test route." }, 404);
    });

    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);

    expect(await screen.findByRole("heading", { name: "New thread" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Ollama is unavailable" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "No local models found" })).not.toBeInTheDocument();
  });

  it("does not let a stale initial group response replace a newly created group", async () => {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
    let resolveInitialGroups: ((response: Response) => void) | undefined;
    const initialGroups = new Promise<Response>((resolve) => { resolveInitialGroups = resolve; });
    const createdGroup = { id: "group-created", name: "Created while loading", collapsed: false, created_at: "2026-07-21T18:00:00Z", updated_at: "2026-07-21T18:00:00Z" };
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/system")) return json({ status: "ok", preview: true, session_required: true, started_at: "2026-07-21T18:00:00Z" });
      if (url.endsWith("/chats")) return json([]);
      if (url.endsWith("/settings")) return json({ settings: { models: { chat: null, title: null }, appearance: { theme: "dark" } } });
      if (url.endsWith("/memories")) return json({ memos: [] });
      if (url.endsWith("/chat-groups") && init?.method === "POST") return json(createdGroup, 201);
      if (url.endsWith("/chat-groups")) return initialGroups;
      if (url.endsWith("/models")) return json({ required_models: [], optional_models: [], installed_models: [], connection: { success: true, status: "connected", message: "Ready" } });
      return json({ detail: "Unexpected test route." }, 404);
    });

    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);
    expect(await screen.findByRole("heading", { name: "New thread" })).toBeVisible();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "New group" }));
    await user.type(screen.getByLabelText("Group name"), createdGroup.name);
    await user.click(screen.getByRole("button", { name: "Create group" }));
    expect(await screen.findByText(createdGroup.name)).toBeVisible();

    await act(async () => { resolveInitialGroups?.(json([])); });
    expect(useChatStore.getState().groups).toEqual([createdGroup]);
  });

  it("does not let a stale initial model response replace a post-rescan inventory", async () => {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
    let resolveInitialModels: ((response: Response) => void) | undefined;
    const initialModels = new Promise<Response>((resolve) => { resolveInitialModels = resolve; });
    let modelRequests = 0;
    const latestModels = { required_models: [], optional_models: [], installed_models: ["rescanned-model"], connection: { success: true, status: "connected", message: "Ready" } };
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/system")) return json({ status: "ok", preview: true, session_required: true, started_at: "2026-07-21T18:00:00Z" });
      if (url.endsWith("/chat-groups")) return json([]);
      if (url.endsWith("/chats")) return json([]);
      if (url.endsWith("/settings")) return json({ settings: { models: { chat: null, title: null }, appearance: { theme: "dark" } } });
      if (url.endsWith("/memories")) return json({ memos: [] });
      if (url.endsWith("/jobs/models") && init?.method === "POST") return json({ job_id: "model-race", kind: "models", status: "queued" }, 202);
      if (url.endsWith("/jobs/model-race/events")) {
        const event = { id: 1, job_id: "model-race", kind: "completed", status: "succeeded", phase: null, data: {} };
        return new Response(`data: ${JSON.stringify(event)}\n\n`, { headers: { "Content-Type": "text/event-stream" } });
      }
      if (url.endsWith("/models")) {
        modelRequests += 1;
        return modelRequests === 1 ? initialModels : json(latestModels);
      }
      return json({ detail: "Unexpected test route." }, 404);
    });

    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);
    expect(await screen.findByRole("heading", { name: "New thread" })).toBeVisible();
    const user = userEvent.setup();
    await openModelSettings(user);
    await user.click(screen.getByRole("button", { name: "Rescan local models" }));
    await waitFor(() => expect(useModelStore.getState().models?.installed_models).toEqual(["rescanned-model"]));

    await act(async () => { resolveInitialModels?.(json({ required_models: [], optional_models: [], installed_models: ["stale-model"], connection: { success: true, status: "connected", message: "Stale" } })); });
    expect(useModelStore.getState().models?.installed_models).toEqual(["rescanned-model"]);
  });

  it("ignores a deferred initial inventory response after the workspace unmounts", async () => {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
    let resolveInitialGroups: ((response: Response) => void) | undefined;
    const initialGroups = new Promise<Response>((resolve) => { resolveInitialGroups = resolve; });
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url.endsWith("/system")) return json({ status: "ok", preview: true, session_required: true, started_at: "2026-07-21T18:00:00Z" });
      if (url.endsWith("/chat-groups")) return initialGroups;
      if (url.endsWith("/chats")) return json([]);
      if (url.endsWith("/settings")) return json({ settings: { models: { chat: null, title: null }, appearance: { theme: "dark" } } });
      if (url.endsWith("/memories")) return json({ memos: [] });
      if (url.endsWith("/models")) return json({ required_models: [], optional_models: [], installed_models: [], connection: { success: true, status: "connected", message: "Ready" } });
      return json({ detail: "Unexpected test route." }, 404);
    });

    const { unmount } = render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);
    expect(await screen.findByRole("heading", { name: "New thread" })).toBeVisible();
    unmount();
    await act(async () => { resolveInitialGroups?.(json([{ id: "late-group", name: "Late", collapsed: false }])); });
    expect(useChatStore.getState().groups).toEqual([]);
  });

  it("reports a specific disable reason when no chat model is selected", () => {
    expect(resolveRuntimeAvailability({
      selectedModel: null,
      selectedModelAvailable: false,
      ollamaConnected: false,
      ollamaMessage: "Ollama is not running.",
      llamacppStatus: { state: "idle" },
    })).toEqual({
      ready: false,
      reason: "no-model-selected",
      message: "Select a local model before sending a message.",
    });
  });

  it("ignores Ollama availability for an installed GGUF selection", () => {
    expect(resolveRuntimeAvailability({
      selectedModel: "gguf:demo.Q4_K_M.gguf",
      selectedModelAvailable: true,
      ollamaConnected: false,
      ollamaMessage: "Ollama is not running.",
      llamacppStatus: { state: "idle" },
    })).toEqual({ ready: true, reason: null, message: null });
  });

  it("renders live llama.cpp status updates in mounted Settings", async () => {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url.endsWith("/system")) return json({
        status: "ok",
        preview: true,
        session_required: true,
        started_at: "2026-07-21T18:00:00Z",
        llamacpp: { state: "idle", binary_present: true, models_directory: "C:\\models" },
      });
      if (url.endsWith("/chat-groups")) return json([]);
      if (url.endsWith("/chats")) return json([]);
      if (url.endsWith("/settings")) return json({ settings: { models: { chat: "gguf:demo.Q4_K_M.gguf", title: null }, appearance: { theme: "dark" } } });
      if (url.endsWith("/memories")) return json({ memos: [] });
      if (url.endsWith("/models")) return json({ required_models: [], optional_models: [], installed_models: ["gguf:demo.Q4_K_M.gguf"], models: [{ name: "gguf:demo.Q4_K_M.gguf" }], connection: { success: true, status: "connected", message: "Ready" } });
      return json({ detail: "Unexpected test route." }, 404);
    });

    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);
    expect(await screen.findByRole("heading", { name: "New thread" })).toBeVisible();
    await userEvent.setup().click(screen.getByRole("link", { name: "Settings" }));
    await userEvent.setup().click(await screen.findByRole("button", { name: "System" }));
    expect(screen.getByText(/Cortex downloads and runs the local model runtime/)).toBeVisible();

    act(() => {
      useModelStore.getState().setLlamacppStatus({
        state: "ready",
        binary_present: true,
        loaded_model: "gguf:demo.Q4_K_M.gguf",
        models_directory: "C:\\models",
        models_directory_exists: true,
        active_backend: "vulkan",
      });
    });

    expect(await screen.findByText(/Local runtime:/)).toHaveTextContent("GPU (Vulkan)");
    expect(screen.getByText(/currently running demo/)).toBeVisible();
  });

  it("updates the document theme when the system color scheme changes", async () => {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    const originalMatchMedia = window.matchMedia;
    let matches = false;
    const listeners = new Set<(event: MediaQueryListEvent) => void>();
    const mediaQuery = {
      get matches() { return matches; },
      media: "(prefers-color-scheme: dark)",
      addEventListener: (_type: "change", listener: (event: MediaQueryListEvent) => void) => listeners.add(listener),
      removeEventListener: (_type: "change", listener: (event: MediaQueryListEvent) => void) => listeners.delete(listener),
    } as unknown as MediaQueryList;
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: vi.fn(() => mediaQuery),
    });
    const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url.endsWith("/system")) return json({ status: "ok", preview: true, session_required: true, started_at: "2026-07-21T18:00:00Z" });
      if (url.endsWith("/chat-groups")) return json([]);
      if (url.endsWith("/chats")) return json([]);
      if (url.endsWith("/settings")) return json({ settings: { models: { chat: null, title: null }, appearance: { theme: "system" } } });
      if (url.endsWith("/memories")) return json({ memos: [] });
      if (url.endsWith("/models")) return json({ required_models: [], optional_models: [], installed_models: [], connection: { success: true, status: "connected", message: "Ready" } });
      return json({ detail: "Unexpected test route." }, 404);
    });

    try {
      render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);

      await screen.findByRole("heading", { name: "New thread" });
      expect(document.documentElement.dataset.theme).toBe("light");
      act(() => {
        matches = true;
        listeners.forEach((listener) => listener({ matches } as MediaQueryListEvent));
      });
      expect(document.documentElement.dataset.theme).toBe("dark");
    } finally {
      if (originalMatchMedia) {
        Object.defineProperty(window, "matchMedia", { configurable: true, value: originalMatchMedia });
      } else {
        delete (window as Partial<Window>).matchMedia;
      }
    }
  });

  it("keeps the shell selection aligned with browser route changes", async () => {
    const user = userEvent.setup();
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    window.history.replaceState({}, "", "/chat/thread-a");
    const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
    const chats = [
      { id: "thread-a", title: "Alpha thread", timestamp: "2026-07-21T18:00:00Z" },
      { id: "thread-b", title: "Beta thread", timestamp: "2026-07-21T18:01:00Z" },
    ];
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url.endsWith("/system")) return json({ status: "ok", preview: true, session_required: true, started_at: "2026-07-21T18:00:00Z" });
      if (url.endsWith("/chat-groups")) return json([]);
      if (url.endsWith("/chats")) return json(chats);
      if (url.endsWith("/chats/thread-a")) {
        return json({ ...chats[0], revision: 1, messages: [{ id: "message-a", role: "assistant", content: "Alpha transcript" }] });
      }
      if (url.endsWith("/chats/thread-b")) {
        return json({ ...chats[1], revision: 1, messages: [{ id: "message-b", role: "assistant", content: "Beta transcript" }] });
      }
      if (url.endsWith("/settings")) return json({ settings: { models: { chat: "model-a", title: null }, appearance: { theme: "dark" } } });
      if (url.endsWith("/memories")) return json({ memos: [] });
      if (url.endsWith("/models")) return json({ required_models: [], optional_models: [], installed_models: ["model-a"], connection: { success: true, status: "connected", message: "Ready" } });
      return json({ detail: "Unexpected test route." }, 404);
    });

    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);

    expect(await screen.findByText("Alpha transcript")).toBeVisible();
    expect(screen.getByRole("button", { name: "Alpha thread" })).toHaveAttribute("aria-current", "page");

    act(() => {
      window.history.pushState({}, "", "/chat/thread-b");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });

    expect(await screen.findByText("Beta transcript")).toBeVisible();
    await waitFor(() => expect(screen.getByRole("button", { name: "Beta thread" })).toHaveAttribute("aria-current", "page"));
    expect(screen.getByRole("button", { name: "Alpha thread" })).not.toHaveAttribute("aria-current");
    expect(document.querySelector("h1.window-title")).toHaveTextContent("Beta thread");

    await user.click(screen.getByRole("link", { name: "Settings" }));
    await user.click(screen.getByRole("link", { name: "Settings" }));
    await user.click(await screen.findByRole("button", { name: "Close settings" }));
    expect(window.location.pathname).toBe("/chat/thread-b");
  });

  it("returns to the current chat after opening settings from the command palette", async () => {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    window.history.replaceState({}, "", "/chat/thread-command-palette");
    const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
    const chat = {
      id: "thread-command-palette",
      title: "Command palette thread",
      timestamp: "2026-07-21T18:00:00Z",
    };
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url.endsWith("/system")) return json({ status: "ok", preview: true, session_required: true, started_at: "2026-07-21T18:00:00Z" });
      if (url.endsWith("/chat-groups")) return json([]);
      if (url.endsWith("/chats")) return json([chat]);
      if (url.endsWith("/chats/thread-command-palette")) return json({ ...chat, revision: 1, messages: [{ id: "message-command-palette", role: "assistant", content: "Command palette transcript" }] });
      if (url.endsWith("/settings")) return json({ settings: { models: { chat: "model-a", title: null }, appearance: { theme: "dark" } } });
      if (url.endsWith("/memories")) return json({ memos: [] });
      if (url.endsWith("/models")) return json({ required_models: [], optional_models: [], installed_models: ["model-a"], connection: { success: true, status: "connected", message: "Ready" } });
      return json({ detail: "Unexpected test route." }, 404);
    });

    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);
    expect(await screen.findByText("Command palette transcript")).toBeVisible();

    const user = userEvent.setup();
    await user.keyboard("{Control>}k{/Control}");
    await user.click(await screen.findByText("Open settings"));
    expect(await screen.findByRole("heading", { name: "Settings", level: 2 })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Close settings" }));
    expect(window.location.pathname).toBe("/chat/thread-command-palette");
  });

  it("keeps an approval actionable and reports a safe API failure", async () => {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
    const pendingTask = {
      job_id: "approval-job",
      profile: "artifact.extended.v1",
      status: "queued",
      sequence: 2,
      phase: "approval",
      message: "Approval required.",
      approval_state: "pending",
      approval_reason: "Create a larger staged image preview.",
      approval_expires_at: "2026-07-21T18:30:00Z",
      can_cancel: false,
      created_at: "2026-07-21T18:00:00Z",
      updated_at: "2026-07-21T18:00:01Z",
    };
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/system")) return json({ status: "ok", preview: true, session_required: true, execution_preview_available: true, started_at: "2026-07-21T18:00:00Z" });
      if (url.endsWith("/chat-groups")) return json([]);
      if (url.endsWith("/chats")) return json([]);
      if (url.endsWith("/settings")) return json({ settings: { models: { chat: "model-a", title: null }, appearance: { theme: "dark" } } });
      if (url.endsWith("/memories")) return json({ memos: [] });
      if (url.endsWith("/models")) return json({ required_models: [], optional_models: [], installed_models: ["model-a"], connection: { success: true, status: "connected", message: "Ready" } });
      if (url.includes("/execution/tasks")) return json({ tasks: [pendingTask] });
      if (url.endsWith("/execution/approval-job/approval") && init?.method === "POST") {
        return json({ detail: "Approval has expired." }, 409);
      }
      return json({ detail: "Unexpected test route." }, 404);
    });

    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);
    const user = userEvent.setup();
    const allow = await screen.findByRole("button", { name: "Allow background task approval-job once" });
    await user.click(allow);

    expect(await screen.findByText("Approval has expired.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Allow background task approval-job once" })).toBeEnabled();
    expect(screen.getByText("Create a larger staged image preview.")).toBeVisible();
  });

  it("does not start a second execution-task poll while the first is pending", async () => {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
    let executionTaskCalls = 0;
    let resolveExecutionTasks: ((response: Response) => void) | undefined;
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url.endsWith("/system")) return json({ status: "ok", preview: true, session_required: true, execution_preview_available: true, started_at: "2026-07-21T18:00:00Z" });
      if (url.endsWith("/chat-groups")) return json([]);
      if (url.endsWith("/chats")) return json([]);
      if (url.endsWith("/settings")) return json({ settings: { models: { chat: "model-a", title: null }, appearance: { theme: "dark" } } });
      if (url.endsWith("/memories")) return json({ memos: [] });
      if (url.endsWith("/models")) return json({ required_models: [], optional_models: [], installed_models: ["model-a"], connection: { success: true, status: "connected", message: "Ready" } });
      if (url.includes("/execution/tasks")) {
        executionTaskCalls += 1;
        return new Promise<Response>((resolve) => { resolveExecutionTasks = resolve; });
      }
      return json({ detail: "Unexpected test route." }, 404);
    });
    let intervalHandler: (() => void) | undefined;
    const intervalSpy = vi.spyOn(window, "setInterval").mockImplementation((handler) => {
      intervalHandler = handler as () => void;
      return 1;
    });

    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);

    await waitFor(() => expect(executionTaskCalls).toBe(1));
    expect(intervalHandler).toBeDefined();
    act(() => intervalHandler?.());
    expect(executionTaskCalls).toBe(1);

    await act(async () => {
      resolveExecutionTasks?.(json({ tasks: [] }));
      await Promise.resolve();
    });
    intervalSpy.mockRestore();
  });

  it("pauses execution-task polling while the page is hidden and refreshes immediately on return", async () => {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
    let executionTaskCalls = 0;
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url.endsWith("/system")) return json({ status: "ok", preview: true, session_required: true, execution_preview_available: true, started_at: "2026-07-21T18:00:00Z" });
      if (url.endsWith("/chat-groups")) return json([]);
      if (url.endsWith("/chats")) return json([]);
      if (url.endsWith("/settings")) return json({ settings: { models: { chat: "model-a", title: null }, appearance: { theme: "dark" } } });
      if (url.endsWith("/memories")) return json({ memos: [] });
      if (url.endsWith("/models")) return json({ required_models: [], optional_models: [], installed_models: ["model-a"], connection: { success: true, status: "connected", message: "Ready" } });
      if (url.includes("/execution/tasks")) {
        executionTaskCalls += 1;
        return json({ tasks: [] });
      }
      return json({ detail: "Unexpected test route." }, 404);
    });
    let intervalHandler: (() => void) | undefined;
    const intervalSpy = vi.spyOn(window, "setInterval").mockImplementation((handler) => {
      intervalHandler = handler as () => void;
      return 1;
    });
    const visibilityDescriptor = Object.getOwnPropertyDescriptor(document, "visibilityState");

    try {
      render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);

      await waitFor(() => expect(executionTaskCalls).toBe(1));
      expect(intervalHandler).toBeDefined();

      // Hidden: a poll tick must not fetch at all.
      Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
      act(() => intervalHandler?.());
      expect(executionTaskCalls).toBe(1);
      act(() => intervalHandler?.());
      expect(executionTaskCalls).toBe(1);

      // Visible again: the visibilitychange listener refreshes immediately,
      // it doesn't wait for the next 1s tick.
      Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
      act(() => { document.dispatchEvent(new Event("visibilitychange")); });
      await waitFor(() => expect(executionTaskCalls).toBe(2));
    } finally {
      if (visibilityDescriptor) Object.defineProperty(document, "visibilityState", visibilityDescriptor);
      intervalSpy.mockRestore();
    }
  });

  it("does not replay terminal tasks from before the current backend session", async () => {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
    const oldTask = {
      job_id: "old-attachment",
      profile: "chat.attachment.v1",
      status: "succeeded",
      sequence: 2,
      phase: "completed",
      message: "Old attachment staged.",
      can_cancel: false,
      created_at: "2026-07-20T18:00:00Z",
      updated_at: "2026-07-20T18:00:01Z",
    };
    const currentTask = {
      ...oldTask,
      job_id: "current-attachment",
      message: "Current attachment staged.",
      created_at: "2026-07-21T18:00:01Z",
      updated_at: "2026-07-21T18:00:02Z",
    };
    const malformedTask = {
      ...oldTask,
      job_id: "malformed-attachment",
      message: "Malformed legacy attachment staged.",
      updated_at: "not-a-timestamp",
    };
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url.endsWith("/system")) return json({ status: "ok", preview: true, session_required: true, execution_preview_available: true, started_at: "2026-07-21T18:00:00Z" });
      if (url.endsWith("/chat-groups")) return json([]);
      if (url.endsWith("/chats")) return json([]);
      if (url.endsWith("/settings")) return json({ settings: { models: { chat: "model-a", title: null }, appearance: { theme: "dark" } } });
      if (url.endsWith("/memories")) return json({ memos: [] });
      if (url.endsWith("/models")) return json({ required_models: [], optional_models: [], installed_models: ["model-a"], connection: { success: true, status: "connected", message: "Ready" } });
      if (url.includes("/execution/tasks")) return json({ tasks: [currentTask, oldTask, malformedTask] });
      return json({ detail: "Unexpected test route." }, 404);
    });

    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);

    expect(await screen.findByText("Current attachment staged.")).toBeVisible();
    expect(screen.queryByText("Old attachment staged.")).not.toBeInTheDocument();
    expect(screen.queryByText("Malformed legacy attachment staged.")).not.toBeInTheDocument();
  });

  it.each([
    { status: "succeeded", message: null, expected: "Local model inventory refreshed." },
    { status: "failed", message: "Model pull failed safely.", expected: "Model pull failed safely." },
    { status: "cancelled", message: "Job cancelled.", expected: "Job cancelled." },
    { status: "running", message: null, expected: "completion was not confirmed" },
  ] as const)("reconciles a model-job SSE EOF reported as $status", async ({ status, message, expected }) => {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    const json = (body: unknown, responseStatus = 200) => new Response(JSON.stringify(body), {
      status: responseStatus,
      headers: { "Content-Type": "application/json" },
    });
    let modelRefreshes = 0;
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/system")) return json({ status: "ok", preview: true, session_required: true, started_at: "2026-07-21T18:00:00Z" });
      if (url.endsWith("/chat-groups")) return json([]);
      if (url.endsWith("/chats")) return json([]);
      if (url.endsWith("/settings")) return json({ settings: { models: { chat: null, title: null }, appearance: { theme: "dark" } } });
      if (url.endsWith("/memories")) return json({ memos: [] });
      if (url.endsWith("/jobs/models") && init?.method === "POST") return json({ job_id: "model-job-eof", kind: "models", status: "queued" }, 202);
      if (url.endsWith("/jobs/model-job-eof/events")) {
        const event = { id: 1, job_id: "model-job-eof", kind: "progress", status: "running", phase: "model_check", data: {} };
        return new Response(`data: ${JSON.stringify(event)}\n\n`, { headers: { "Content-Type": "text/event-stream" } });
      }
      if (url.endsWith("/jobs/model-job-eof")) {
        return json({
          job_id: "model-job-eof",
          kind: "models",
          status,
          sequence: 2,
          error: message,
          result: status === "succeeded" ? { connection: { success: true } } : null,
        });
      }
      if (url.endsWith("/models")) {
        modelRefreshes += 1;
        return json({ required_models: [], optional_models: [], installed_models: [], connection: { success: true, status: "connected", message: "Ready" } });
      }
      return json({ detail: "Unexpected test route." }, 404);
    });

    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);
    expect(await screen.findByRole("heading", { name: "New thread" })).toBeVisible();
    const user = userEvent.setup();
    await openModelSettings(user);
    await user.click(screen.getByRole("button", { name: "Rescan local models" }));

    expect(await screen.findByText((content) => content.includes(expected))).toBeVisible();
    expect(fetcher.mock.calls.filter(([input]) => String(input).endsWith("/jobs/model-job-eof"))).toHaveLength(1);
    expect(modelRefreshes).toBe(status === "succeeded" ? 2 : 1);
    if (status !== "succeeded") expect(screen.queryByText("Local model inventory refreshed.")).not.toBeInTheDocument();
  });

  it("selects a downloaded model without reverting settings saved while it downloaded", async () => {
    // Regression test: a GGUF download runs for minutes and then selects
    // what it fetched. Settings stays editable the whole time (Save is
    // gated on `saving`, not on `modelBusy`), so the selection must send
    // the *current* settings -- not the snapshot captured in the render
    // that started the download, which silently reverted anything saved
    // in between.
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    const storedSettings = {
      models: { chat: null, title: null },
      appearance: { theme: "dark" },
      memory: { enabled: true },
    };
    const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
    const settingsWrites: Record<string, unknown>[] = [];
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/system")) return json({ status: "ok", preview: true, session_required: true, started_at: "2026-07-21T18:00:00Z" });
      if (url.endsWith("/chat-groups")) return json([]);
      if (url.endsWith("/chats")) return json([]);
      if (url.endsWith("/memories")) return json({ memos: [] });
      if (url.endsWith("/settings") && init?.method === "PUT") {
        const body = JSON.parse(String(init.body)) as { settings: Record<string, unknown> };
        settingsWrites.push(body.settings);
        return json({ settings: body.settings });
      }
      if (url.endsWith("/settings")) return json({ settings: storedSettings });
      if (url.endsWith("/models/gguf/downloads") && init?.method === "POST") {
        return json({ job_id: "gguf-job", kind: "gguf_download", status: "queued" }, 202);
      }
      if (url.endsWith("/jobs/gguf-job/events")) {
        // The user saves an unrelated settings change while the download is
        // still streaming.
        useSettingsStore.getState().setSettings({ ...storedSettings, memory: { enabled: false } } as never);
        const event = { id: 1, job_id: "gguf-job", kind: "completed", status: "succeeded", phase: null, data: { filename: "demo.Q4_K_M.gguf" } };
        return new Response(`data: ${JSON.stringify(event)}

`, {
          headers: { "Content-Type": "text/event-stream" },
        });
      }
      if (url.endsWith("/models")) return json({ required_models: [], optional_models: [], installed_models: [], connection: { success: true, status: "connected", message: "Ready" } });
      return json({ detail: "Unexpected test route." }, 404);
    });

    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);

    expect(await screen.findByRole("heading", { name: "New thread" })).toBeVisible();
    const user = userEvent.setup();
    await user.click(screen.getByRole("link", { name: "Settings" }));
    await user.click(await screen.findByRole("button", { name: "System" }));
    await user.type(screen.getByLabelText(/Repo id/), "vendor/demo-GGUF");
    await user.type(screen.getByLabelText(/File name/), "demo.Q4_K_M.gguf");
    await user.click(screen.getByRole("button", { name: /Download model/ }));

    await waitFor(() => expect(settingsWrites).toHaveLength(1));
    expect(settingsWrites[0]).toMatchObject({
      models: { chat: "gguf:demo.Q4_K_M.gguf", title: null },
      memory: { enabled: false },
    });
  });
});
