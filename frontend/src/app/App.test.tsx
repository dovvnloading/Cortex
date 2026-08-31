import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import { CortexApi } from "../api/client";
import { useChatStore } from "../stores/useChatStore";
import { useSettingsStore } from "../stores/useSettingsStore";
import { useModelStore } from "../stores/useModelStore";
import { ToastProvider } from "./ToastProvider";

describe("App", () => {
  afterEach(() => {
    useModelStore.getState().setLlamacppStatus(null);
    window.sessionStorage.clear();
    window.history.replaceState({}, "", "/");
  });

  it("starts without exposing a manual sign-in boundary", () => {
    window.sessionStorage.clear();
    render(<App api={new CortexApi("/api/v1", window.fetch.bind(window))} />);
    expect(screen.getByRole("heading", { name: "Start local workspace" })).toBeInTheDocument();
    expect(screen.queryByLabelText(/token/i)).not.toBeInTheDocument();
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

  it("returns to onboarding and clears a persisted generation when its stream session expires", async () => {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    window.sessionStorage.setItem("cortex.active.generation", JSON.stringify({
      jobId: "job-expired",
      threadId: "thread-expired",
      lastEventId: 3,
    }));
    window.history.replaceState({}, "", "/chat/thread-expired");
    const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url.endsWith("/system")) return json({ status: "ok", preview: true, session_required: true, started_at: "2026-07-21T18:00:00Z" });
      if (url.endsWith("/chat-groups")) return json([]);
      if (url.endsWith("/chats")) return json([{ id: "thread-expired", title: "Interrupted", timestamp: "2026-07-21T18:00:00Z" }]);
      if (url.endsWith("/chats/thread-expired")) return json({ id: "thread-expired", title: "Interrupted", timestamp: "2026-07-21T18:00:00Z", revision: 1, messages: [] });
      if (url.endsWith("/settings")) return json({ settings: { models: { chat: "model-a", title: null }, appearance: { theme: "dark" } } });
      if (url.endsWith("/memories")) return json({ memos: [] });
      if (url.endsWith("/models")) return json({ required_models: [], optional_models: [], installed_models: ["model-a"], connection: { success: true, status: "connected", message: "Ready" } });
      if (url.endsWith("/generations/job-expired/events")) return json({ detail: "Local session expired." }, 401);
      return json({ detail: "Unexpected test route." }, 404);
    });

    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);

    expect(await screen.findByRole("heading", { name: "Start local workspace" })).toBeVisible();
    expect(fetcher.mock.calls.filter(([input]) => String(input).endsWith("/generations/job-expired/events"))).toHaveLength(1);
    expect(window.sessionStorage.getItem("cortex.session.token")).toBeNull();
    expect(window.sessionStorage.getItem("cortex.active.generation")).toBeNull();
    expect(useChatStore.getState().generation).toMatchObject({ jobId: null, phase: "idle" });
  });

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
    await user.click(screen.getByRole("link", { name: "Settings" }));
    await user.click(await screen.findByRole("button", { name: "AI Model" }));
    await user.click(screen.getByRole("button", { name: "Rescan local models" }));

    expect(await screen.findByRole("heading", { name: "Start local workspace" })).toBeVisible();
    expect(window.sessionStorage.getItem("cortex.session.token")).toBeNull();
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
    await user.click(screen.getByRole("link", { name: "Settings" }));
    await user.click(await screen.findByRole("button", { name: "AI Model" }));
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
