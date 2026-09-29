import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { CortexApi } from "../api/client";
import { ToastProvider } from "./ToastProvider";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { "Content-Type": "application/json" },
});

describe("App model download failures", () => {
  // The two lazy routes are loaded once up front so the first test's time
  // covers what it does rather than a cold module graph (see App.test.tsx).
  beforeAll(async () => {
    await Promise.all([
      import("../features/chat/ChatPage"),
      import("../features/settings/SettingsPanel"),
    ]);
  }, 120_000);

  afterEach(() => {
    window.sessionStorage.clear();
    window.history.replaceState({}, "", "/");
  });

  const workspace = (onDownload: (url: string, init?: RequestInit) => Response | null) => vi.fn<typeof fetch>(async (input, init) => {
    const url = String(input);
    const handled = onDownload(url, init);
    if (handled) return handled;
    if (url.endsWith("/system")) return json({ status: "ok", preview: true, session_required: true, started_at: "2026-07-21T18:00:00Z" });
    if (url.endsWith("/chat-groups")) return json([]);
    if (url.endsWith("/chats")) return json([]);
    if (url.endsWith("/memories")) return json({ memos: [] });
    if (url.endsWith("/settings")) return json({ settings: { models: { chat: null, title: null }, appearance: { theme: "dark" } } });
    if (url.endsWith("/models")) return json({ required_models: [], optional_models: [], installed_models: [], connection: { success: true, status: "connected", message: "Ready" } });
    return json({ detail: "Unexpected test route." }, 404);
  });

  const startDownload = async () => {
    const user = userEvent.setup();
    await user.click(screen.getByRole("link", { name: "Settings" }));
    await user.click(await screen.findByRole("button", { name: "System" }, { timeout: 10_000 }));
    await user.type(screen.getByLabelText(/Repo id/), "vendor/demo-GGUF");
    await user.type(screen.getByLabelText(/File name/), "demo.Q4_K_M.gguf");
    await user.click(screen.getByRole("button", { name: /Download model/ }));
  };

  it("keeps the failure reason on the form after the job reports it", async () => {
    // The reason used to appear only in a toast, while the form told the
    // person to look at that toast.
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    const reason = "Checksum mismatch: the downloaded file is not the published one.";
    const fetcher = workspace((url, init) => {
      if (url.endsWith("/models/gguf/downloads") && init?.method === "POST") {
        return json({ job_id: "gguf-job", kind: "gguf_download", status: "queued" }, 202);
      }
      if (url.endsWith("/jobs/gguf-job/events")) {
        const event = { id: 1, job_id: "gguf-job", kind: "error", status: "failed", phase: null, data: { message: reason } };
        return new Response(`data: ${JSON.stringify(event)}\n\n`, { headers: { "Content-Type": "text/event-stream" } });
      }
      return null;
    });

    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);
    expect(await screen.findByRole("heading", { name: "New thread" })).toBeVisible();
    await startDownload();

    await waitFor(() => expect(screen.getAllByRole("alert").filter((node) => node.textContent?.includes(reason))).toHaveLength(2));
    const inline = screen.getAllByRole("alert").find((node) => node.classList.contains("field-error") && node.textContent?.includes(reason));
    expect(inline).toBeDefined();
    expect(inline?.textContent).toBe(reason);
    expect(inline).not.toHaveTextContent("notification");
    // The toast is dismissible rather than timed.
    const toast = screen.getAllByRole("alert").find((node) => node.classList.contains("toast"));
    expect(toast).toBeDefined();
    expect(within(toast as HTMLElement).getByRole("button", { name: "Dismiss notification" })).toBeVisible();
  });

  it("keeps the reason when the download cannot be started", async () => {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    const fetcher = workspace((url, init) => {
      if (url.endsWith("/models/gguf/downloads") && init?.method === "POST") {
        return json({ detail: "That host is not on the download allow-list." }, 422);
      }
      return null;
    });

    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);
    expect(await screen.findByRole("heading", { name: "New thread" })).toBeVisible();
    await startDownload();

    const inline = await waitFor(() => {
      const node = screen.getAllByRole("alert").find((candidate) => candidate.classList.contains("field-error") && candidate.textContent?.includes("allow-list"));
      expect(node).toBeDefined();
      return node as HTMLElement;
    });
    expect(inline.textContent).toBe("That host is not on the download allow-list.");
  });
});
