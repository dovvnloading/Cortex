import { act, render, screen, waitFor } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ExecutionTaskSummary } from "../../../contracts/cortex-api";
import { CortexApi } from "../api/client";
import { useModelStore } from "../stores/useModelStore";
import { App } from "./App";
import { ToastProvider } from "./ToastProvider";

// A render counter around the real shell: every re-render of the authenticated
// workspace re-renders AppShell, so its count is the workspace's.
const shell = vi.hoisted(() => ({ renders: 0 }));

vi.mock("../features/shell/AppShell", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../features/shell/AppShell")>();
  return {
    ...actual,
    AppShell: (props: ComponentProps<typeof actual.AppShell>) => {
      shell.renders += 1;
      return actual.AppShell(props);
    },
  };
});

const respond = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { "Content-Type": "application/json" },
});

const runningTask: ExecutionTaskSummary = {
  job_id: "job-1",
  profile: "python",
  status: "running",
  sequence: 1,
  created_at: "2026-07-21T18:00:01Z",
  updated_at: "2026-07-21T18:00:02Z",
};

type Backend = {
  tasks: ExecutionTaskSummary[];
  llamacpp: Record<string, unknown>;
};

/** A workspace with the execution tray and a GGUF model selected, so both polls are live. */
function backend(state: Backend) {
  return vi.fn<typeof fetch>(async (input) => {
    const url = String(input);
    if (url.endsWith("/system")) {
      return respond({ status: "ok", preview: true, session_required: true, execution_preview_available: true, started_at: "2026-07-21T18:00:00Z", llamacpp: state.llamacpp });
    }
    if (url.includes("/execution/tasks")) return respond({ tasks: state.tasks });
    if (url.endsWith("/chat-groups")) return respond([]);
    if (url.endsWith("/chats")) return respond([]);
    if (url.endsWith("/settings")) return respond({ settings: { models: { chat: "gguf:demo.Q4_K_M.gguf", title: null }, appearance: { theme: "dark" } } });
    if (url.endsWith("/models")) return respond({ required_models: [], optional_models: [], installed_models: ["gguf:demo.Q4_K_M.gguf"], models: [{ name: "gguf:demo.Q4_K_M.gguf" }], connection: { success: true, status: "connected", message: "Ready" } });
    return respond({ detail: "Unexpected test route." }, 404);
  });
}

const callsTo = (fetcher: ReturnType<typeof backend>, fragment: string) =>
  fetcher.mock.calls.filter(([input]) => String(input).includes(fragment)).length;

/** Fire the visibility poll and wait until both endpoints have answered it and React has settled. */
async function pollOnce(fetcher: ReturnType<typeof backend>) {
  const tasksBefore = callsTo(fetcher, "/execution/tasks");
  const systemBefore = callsTo(fetcher, "/system");
  act(() => { document.dispatchEvent(new Event("visibilitychange")); });
  await waitFor(() => {
    expect(callsTo(fetcher, "/execution/tasks")).toBeGreaterThan(tasksBefore);
    expect(callsTo(fetcher, "/system")).toBeGreaterThan(systemBefore);
  });
  // Let the responses resolve into state (or not).
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 30)); });
}

describe("App polling", () => {
  afterEach(() => {
    window.sessionStorage.clear();
    useModelStore.getState().setLlamacppStatus(null);
    shell.renders = 0;
  });

  it("does not re-render the workspace for a poll that returned what it already had", async () => {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    const state: Backend = { tasks: [runningTask], llamacpp: { state: "idle", binary_present: true, models_directory: "C:\\models" } };
    const fetcher = backend(state);
    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);
    expect(await screen.findByRole("heading", { name: "New thread" })).toBeVisible();

    // Settle: the first polls legitimately change state from empty to populated.
    await pollOnce(fetcher);
    await pollOnce(fetcher);
    const rendersAfterSettling = shell.renders;

    // Same tasks, same runtime status, ten more polls. Each response is a
    // freshly parsed array/object, which used to be a state write and a full
    // shell render every time.
    for (let poll = 0; poll < 10; poll += 1) await pollOnce(fetcher);

    expect(shell.renders).toBe(rendersAfterSettling);
  });

  it("still re-renders when a poll brings something new", async () => {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    const state: Backend = { tasks: [], llamacpp: { state: "idle", binary_present: true, models_directory: "C:\\models" } };
    const fetcher = backend(state);
    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);
    expect(await screen.findByRole("heading", { name: "New thread" })).toBeVisible();
    await pollOnce(fetcher);
    await pollOnce(fetcher);
    const before = shell.renders;

    state.tasks = [runningTask];
    await pollOnce(fetcher);
    expect(shell.renders).toBeGreaterThan(before);

    const afterTask = shell.renders;
    state.llamacpp = { state: "ready", binary_present: true, loaded_model: "gguf:demo.Q4_K_M.gguf", models_directory: "C:\\models" };
    await pollOnce(fetcher);
    expect(useModelStore.getState().llamacppStatus?.state).toBe("ready");
    expect(shell.renders).toBeGreaterThan(afterTask);

    // A task changing status (same id, same length) is a change too.
    const afterStatus = shell.renders;
    state.tasks = [{ ...runningTask, status: "succeeded", sequence: 2, updated_at: "2026-07-21T18:00:09Z" }];
    await pollOnce(fetcher);
    expect(shell.renders).toBeGreaterThan(afterStatus);
  });
});
