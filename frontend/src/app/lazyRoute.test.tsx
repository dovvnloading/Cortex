import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Suspense } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RouteBoundary } from "./ErrorBoundary";
import { lazyRoute } from "./lazyRoute";

function Loaded({ label }: { label: string }) {
  return <p>{label} route loaded</p>;
}

describe("lazyRoute", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.sessionStorage.clear();
  });

  it("passes props through to the loaded route", async () => {
    const Route = lazyRoute(async () => ({ default: Loaded }));

    render(<Suspense fallback={<p>Loading</p>}><Route label="Chat" /></Suspense>);

    expect(await screen.findByText("Chat route loaded")).toBeVisible();
  });

  it("imports the code again on Try again, which a plain React.lazy never does", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const user = userEvent.setup();
    const load = vi.fn<() => Promise<{ default: typeof Loaded }>>()
      .mockRejectedValueOnce(new TypeError("Failed to fetch dynamically imported module"))
      .mockResolvedValue({ default: Loaded });
    const Route = lazyRoute(load);

    render(
      <Suspense fallback={<p>Loading</p>}>
        <RouteBoundary name="Settings" scope="settings" resetKey="/settings" onRetry={Route.reload}>
          <Route label="Settings" />
        </RouteBoundary>
      </Suspense>,
    );

    // The failed import is what the boundary catches: nothing about it is lost.
    expect(await screen.findByRole("alert")).toHaveTextContent("Settings hit a problem");
    expect(load).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByText("Settings route loaded")).toBeVisible();
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("offers a full reload when importing again fails as well", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const user = userEvent.setup();
    const load = vi.fn<() => Promise<{ default: typeof Loaded }>>()
      .mockRejectedValue(new TypeError("Failed to fetch dynamically imported module"));
    const Route = lazyRoute(load);

    render(
      <Suspense fallback={<p>Loading</p>}>
        <RouteBoundary name="Chat" scope="chat" resetKey="/chat/new" onRetry={Route.reload}>
          <Route label="Chat" />
        </RouteBoundary>
      </Suspense>,
    );
    await screen.findByRole("alert");
    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Chat hit a problem");
    expect(load).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: "Reload workspace" })).toBeVisible();
  });
});
