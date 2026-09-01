import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ErrorBoundary } from "./ErrorBoundary";

function BrokenView(): never {
  throw new Error("synthetic render failure");
}

describe("ErrorBoundary", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("avoids claiming data was unchanged after a render crash", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);

    render(
      <ErrorBoundary>
        <BrokenView />
      </ErrorBoundary>,
    );

    expect(screen.getByRole("heading", { name: "Cortex needs a restart" })).toBeVisible();
    expect(screen.getByText(/the crashed view took no further action/i)).toBeVisible();
    expect(screen.getByText(/collect diagnostics after reloading/i)).toBeVisible();
    expect(screen.queryByText(/local data was not changed/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload workspace" })).toBeVisible();
  });
});
