import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { CortexSettings } from "../../../contracts/cortex-api";
import { CortexApi } from "../api/client";
import { App } from "./App";
import { ToastProvider } from "./ToastProvider";

const json = (value: unknown, status = 200) => Response.json(value, { status });
const alpha = "cloud:test-lab:alpha:v1";
const beta = "cloud:test-lab:beta";
const readyProvider = { id: "test-lab", label: "Test Lab", configured: true, setup: "Set TEST_LAB_KEY." };
const otherProvider = { id: "other", label: "Other Lab", configured: false, setup: "Set OTHER_LAB_KEY." };

function workspace(initial: string | null = null) {
  let settings: CortexSettings = { revision: 3, models: { chat: initial }, appearance: { theme: "dark" } };
  let models = [alpha];
  let fail = false;
  let conflict: string | null = null;
  const fetcher = vi.fn<typeof fetch>(async (input, init) => {
    const path = String(input).replace(/^.*\/api\/v1/, "");
    switch (path) {
      case "/system": return json({ status: "ok", preview: false, session_required: true });
      case "/settings":
        if (init?.method === "PUT" && conflict) {
          settings = { ...settings, revision: 4, models: { chat: conflict } };
          conflict = null;
          return json({ detail: "Settings changed elsewhere." }, 409);
        }
        if (init?.method === "PUT") settings = JSON.parse(String(init.body)).settings as CortexSettings;
        return json({ settings });
      case "/chats":
      case "/chat-groups": return json([]);
      case "/memories": return json({ memos: [] });
      case "/models": return json({ required_models: [], optional_models: [], installed_models: [], connection: { success: false, status: "error", message: "Ollama is offline." } });
      case "/cloud/providers": return json({ providers: [readyProvider, otherProvider] });
      case "/cloud/providers/test-lab/models": return fail ? json({ detail: "Test Lab authentication failed." }, 502) : json({ models });
      default: return json({ detail: "Unexpected test route." }, 404);
    }
  });
  const open = () => {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    return render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);
  };
  return { open, fetcher, conflictOnce: (id: string) => { conflict = id; }, getSelection: () => settings.models?.chat, setModels: (names: string[]) => { models = names; }, setFailed: () => { fail = true; } };
}

describe("cloud models in the chat workspace", () => {
  beforeAll(async () => {
    await Promise.all([import("../features/chat/ChatPage"), import("../features/settings/SettingsPanel")]);
  }, 120_000);
  afterEach(() => {
    window.sessionStorage.clear();
    window.history.replaceState({}, "", "/");
  });

  it("persists an explicit selection, restores it without Ollama, and reflects model retirement without switching providers", async () => {
    const app = workspace();
    const user = userEvent.setup();
    const first = app.open();
    await user.click(await screen.findByRole("button", { name: "Select a model" }));
    expect(app.getSelection()).toBeNull();
    expect(screen.getByRole("group", { name: "Cloud · test-lab" })).toBeVisible();
    await user.click(screen.getByRole("option", { name: "test-lab / alpha:v1" }));
    await waitFor(() => expect(app.getSelection()).toBe(alpha));
    first.unmount();
    app.open();
    const picker = await screen.findByRole("button", { name: "Selected cloud model: test-lab / alpha:v1" });
    await user.type(screen.getByRole("textbox", { name: "Message Cortex" }), "Hi");
    expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled();
    expect(screen.getByText(/Sending to test-lab includes this conversation/)).toBeVisible();

    app.setModels([beta]);
    await user.click(picker);
    await user.click(screen.getByRole("button", { name: "Rescan models" }));
    await screen.findByRole("option", { name: "test-lab / beta" });
    expect(screen.queryByRole("option", { name: "test-lab / alpha:v1" })).toBeNull();
    expect(screen.getByRole("button", { name: "Send message", hidden: true })).toBeDisabled();
    expect(app.getSelection()).toBe(alpha);
    expect(app.fetcher.mock.calls.some(([url]) => String(url).includes("/other/models"))).toBe(false);
  });

  it("opens provider setup from an empty chat and restores its draft on return", async () => {
    const user = userEvent.setup();
    const app = workspace();
    app.setModels([]);
    app.open();
    const input = await screen.findByRole("textbox", { name: "Message Cortex" });
    await user.type(input, "A draft before setup");
    await user.click(screen.getByRole("button", { name: "No local models available" }));
    await user.click(await screen.findByRole("button", { name: "Open AI Model settings" }));
    expect(await screen.findByRole("heading", { name: "Model and generation" })).toBeVisible();
    expect(window.location.search).toBe("?section=model");
    await user.click(screen.getByRole("button", { name: "Close settings" }));
    expect(await screen.findByRole("textbox", { name: "Message Cortex" })).toHaveValue("A draft before setup");
  });

  it("reloads the authoritative selection and revision after a rejected picker write before retrying", async () => {
    const user = userEvent.setup();
    const app = workspace(alpha);
    const gamma = "cloud:test-lab:gamma";
    app.setModels([alpha, beta, gamma]);
    app.conflictOnce(gamma);
    app.open();
    await user.click(await screen.findByRole("button", { name: /Selected cloud model: test-lab \/ alpha/ }));
    const choice = await screen.findByRole("option", { name: "test-lab / beta" });
    await user.click(choice);
    await waitFor(() => expect(screen.getByRole("option", { name: "test-lab / gamma" })).toHaveAttribute("aria-selected", "true"));
    expect(app.getSelection()).toBe(gamma);
    await user.click(choice);
    await waitFor(() => expect(app.getSelection()).toBe(beta));
    const writes = app.fetcher.mock.calls.filter(([, init]) => init?.method === "PUT");
    expect(writes.map(([, init]) => JSON.parse(String(init?.body)).expected_revision)).toEqual([3, 4]);
  });

  it("keeps the recipient and the stale catalog warning available beside the draft until discovery recovers", async () => {
    const user = userEvent.setup();
    const app = workspace(alpha);
    app.open();
    const input = await screen.findByRole("textbox", { name: "Message Cortex" });
    await user.type(input, "Review this text");
    await waitFor(() => expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled());
    app.setFailed();
    await act(async () => { document.dispatchEvent(new Event("visibilitychange")); });
    await waitFor(() => expect(input).toHaveAccessibleDescription(/Sending to test-lab.*last model list.*authentication failed/));
    expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled();
    expect(app.getSelection()).toBe(alpha);
  });

  it("refreshes on visibility, shows provider setup and reports discovery failure while retaining the saved ID", async () => {
    const app = workspace(alpha);
    const user = userEvent.setup();
    app.open();
    await screen.findByRole("button", { name: "Selected cloud model: test-lab / alpha:v1" });
    await user.click(screen.getByRole("link", { name: "Settings" }));
    await user.click(await screen.findByRole("button", { name: "AI Model" }));
    expect(await screen.findByText("1 chat model available")).toBeVisible();
    expect(screen.getByText("Set TEST_LAB_KEY.")).toBeVisible();
    expect(screen.getByText(/The cloud provider controls sampling/)).toBeVisible();
    expect(screen.queryByText("Temperature")).toBeNull();
    app.setFailed();
    await act(async () => { document.dispatchEvent(new Event("visibilitychange")); });
    expect(await screen.findByText("Test Lab authentication failed.")).toBeVisible();
    expect(app.getSelection()).toBe(alpha);
    await user.click(screen.getByRole("combobox", { name: "Cloud provider" }));
    await user.click(await screen.findByRole("option", { name: "Other Lab" }));
    expect(await screen.findByText("Set OTHER_LAB_KEY.")).toBeVisible();
  });
});
