import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { CortexApi } from "../api/client";
import { useModelStore } from "../stores/useModelStore";
import { ToastProvider } from "./ToastProvider";

const respond = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { "Content-Type": "application/json" },
});

const loadedStatus = {
  state: "ready",
  binary_present: true,
  loaded_model: "gguf:demo.Q4_K_M.gguf",
  models_directory: "C:\\models",
  models_directory_exists: true,
  active_backend: "cpu",
};

const unloadedStatus = {
  state: "idle",
  binary_present: true,
  loaded_model: null,
  models_directory: "C:\\models",
  models_directory_exists: true,
  active_backend: "cpu",
  last_restart_reason: "the model was unloaded at your request",
};

/** A backend whose runtime status follows `runtime.current`, and whose unload answers as told. */
function backend(unload: () => Response) {
  const runtime = { current: loadedStatus as Record<string, unknown> };
  const fetcher = vi.fn<typeof fetch>(async (input, init) => {
    const url = String(input);
    if (url.endsWith("/llamacpp/unload") && init?.method === "POST") {
      const response = unload();
      if (response.ok) runtime.current = unloadedStatus;
      return response;
    }
    if (url.endsWith("/system")) {
      return respond({
        status: "ok",
        preview: true,
        session_required: true,
        started_at: "2026-07-21T18:00:00Z",
        llamacpp: runtime.current,
      });
    }
    if (url.endsWith("/chat-groups")) return respond([]);
    if (url.endsWith("/chats")) return respond([]);
    if (url.endsWith("/settings")) {
      return respond({ settings: { models: { chat: "gguf:demo.Q4_K_M.gguf", title: null }, appearance: { theme: "dark" } } });
    }
    if (url.endsWith("/memories")) return respond({ memos: [] });
    if (url.endsWith("/models")) {
      return respond({
        required_models: [],
        optional_models: [],
        installed_models: ["gguf:demo.Q4_K_M.gguf"],
        models: [{ name: "gguf:demo.Q4_K_M.gguf" }],
        connection: { success: true, status: "connected", message: "Ready" },
      });
    }
    return respond({ detail: "Unexpected test route." }, 404);
  });
  return { fetcher, runtime };
}

async function openSystemSettings(user: ReturnType<typeof userEvent.setup>) {
  expect(await screen.findByRole("heading", { name: "New thread" }, { timeout: 10_000 })).toBeVisible();
  await user.click(screen.getByRole("link", { name: "Settings" }));
  await user.click(await screen.findByRole("button", { name: "System" }, { timeout: 10_000 }));
}

describe("App local model unload", () => {
  // The settings route is lazy; load it once so a test's own time is not spent on it.
  beforeAll(async () => {
    await Promise.all([
      import("../features/chat/ChatPage"),
      import("../features/settings/SettingsPanel"),
    ]);
  }, 120_000);

  afterEach(() => {
    useModelStore.getState().setLlamacppStatus(null);
    window.sessionStorage.clear();
    window.history.replaceState({}, "", "/");
  });

  it("unloads the model, shows the new state at once, and says what happened", async () => {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    window.history.replaceState({}, "", "/chat/new");
    const { fetcher } = backend(() => respond(unloadedStatus));
    const user = userEvent.setup();
    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);
    await openSystemSettings(user);
    const button = await screen.findByRole("button", { name: "Unload model" });
    await waitFor(() => expect(button).toBeEnabled());

    await user.click(button);

    expect(await screen.findByText(/The local model was unloaded/)).toBeVisible();
    await waitFor(() => expect(screen.getByRole("button", { name: "Unload model" })).toBeDisabled());
    expect(screen.getByText("No local model is loaded right now.")).toBeVisible();
    const unloads = fetcher.mock.calls.filter(([input]) => String(input).endsWith("/llamacpp/unload"));
    expect(unloads).toHaveLength(1);
    expect(unloads[0]?.[1]).toMatchObject({ method: "POST" });
  });

  it("reports the backend's reason when the unload is refused, and leaves the model as it was", async () => {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    window.history.replaceState({}, "", "/chat/new");
    const { fetcher } = backend(() => respond(
      { detail: "A response is being generated. Stop it or wait for it to finish, then unload the model." },
      409,
    ));
    const user = userEvent.setup();
    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);
    await openSystemSettings(user);
    const button = await screen.findByRole("button", { name: "Unload model" });
    await waitFor(() => expect(button).toBeEnabled());

    await user.click(button);

    expect(await screen.findByText(/A response is being generated/)).toBeVisible();
    await waitFor(() => expect(screen.getByRole("button", { name: "Unload model" })).toBeEnabled());
    expect(useModelStore.getState().llamacppStatus?.state).toBe("ready");
  });
});
