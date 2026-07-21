import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import { CortexApi } from "../api/client";
import { ToastProvider } from "./ToastProvider";

describe("App", () => {
  afterEach(() => {
    window.sessionStorage.clear();
    window.history.replaceState({}, "", "/");
  });

  it("starts at the authenticated local-session boundary", () => {
    window.sessionStorage.clear();
    render(<App api={new CortexApi("/api/v1", window.fetch.bind(window))} />);
    expect(screen.getByRole("heading", { name: "Connect to Cortex" })).toBeInTheDocument();
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

    await waitFor(() => expect(screen.getByRole("heading", { name: "Connect to Cortex" })).toBeVisible());
    expect(fetcher.mock.calls.filter(([input]) => String(input).endsWith("/session/exchange"))).toHaveLength(1);
    expect(screen.getByLabelText("Launcher token")).toHaveValue("");
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
});
