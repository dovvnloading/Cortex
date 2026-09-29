import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { CortexApi } from "../api/client";
import { ToastProvider } from "./ToastProvider";

describe("App failure reporting", () => {
  afterEach(() => {
    window.sessionStorage.clear();
    window.history.replaceState({}, "", "/");
  });

  it("says the local backend is unreachable when the workspace load cannot connect", async () => {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    window.history.replaceState({}, "", "/chat/new");
    const fetcher = vi.fn<typeof fetch>(async () => {
      throw new TypeError("Failed to fetch");
    });

    render(<ToastProvider><App api={new CortexApi("/api/v1", fetcher)} /></ToastProvider>);

    expect(await screen.findByRole("heading", { name: "Workspace unavailable" }, { timeout: 10_000 })).toBeVisible();
    expect(screen.getByText(/could not reach the local backend/i)).toBeVisible();
    // The browser's own wording is not what the user should read, and a dead
    // backend is not an expired session: the app stays on the workspace error
    // with its Retry instead of falling back to onboarding.
    expect(screen.queryByText(/failed to fetch/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Start local workspace" })).not.toBeInTheDocument();
  });
});
