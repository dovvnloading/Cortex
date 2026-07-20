import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
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
});
