import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LAST_CRASH_KEY, readLastCrash } from "../lib/crashLog";
import { ErrorBoundary, InlineBoundary, RouteBoundary } from "./ErrorBoundary";

function BrokenView(): never {
  throw new Error("synthetic render failure");
}

/** Renders fine until told to break, so a test can decide what a retry meets. */
function Fragile({ broken }: { broken: boolean }) {
  if (broken) throw new Error("synthetic render failure");
  return <p>View is fine</p>;
}

function stubClipboard(writeText: (text: string) => Promise<void>) {
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
}

describe("ErrorBoundary", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.sessionStorage.clear();
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
    expect(screen.getByText(/copy the details first/i)).toBeVisible();
    expect(screen.queryByText(/local data was not changed/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload workspace" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Copy details" })).toBeVisible();
  });

  it("records the crash for the session before offering a reload", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);

    render(
      <ErrorBoundary scope="workspace">
        <BrokenView />
      </ErrorBoundary>,
    );

    const record = readLastCrash();
    expect(record).toMatchObject({ scope: "workspace", name: "Error", message: "synthetic render failure" });
    expect(record?.componentStack).toContain("BrokenView");
    expect(Number.isNaN(Date.parse(record?.time ?? ""))).toBe(false);
  });

  it("keeps prompts, responses and props out of what it records", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    function Card({ text }: { text: string }): never {
      throw new Error("card failed to render", { cause: text });
    }

    render(
      <ErrorBoundary>
        <Card text="PRIVATE-PROMPT-TEXT" />
      </ErrorBoundary>,
    );

    const stored = window.sessionStorage.getItem(LAST_CRASH_KEY) ?? "";
    expect(stored).toContain("card failed to render");
    expect(stored).not.toContain("PRIVATE-PROMPT-TEXT");
    // Nothing on the crash screen quotes it either.
    expect(document.body).not.toHaveTextContent("PRIVATE-PROMPT-TEXT");
  });

  it("still shows the restart page when session storage refuses the record", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("Quota exceeded", "QuotaExceededError");
    });

    render(
      <ErrorBoundary>
        <BrokenView />
      </ErrorBoundary>,
    );

    expect(screen.getByRole("heading", { name: "Cortex needs a restart" })).toBeVisible();
  });

  it("tries its children again when resetKey changes, and not before", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const { rerender } = render(
      <ErrorBoundary resetKey="/settings" fallback={() => <p>Crashed</p>}>
        <Fragile broken />
      </ErrorBoundary>,
    );
    expect(screen.getByText("Crashed")).toBeVisible();

    // Same key: the crash stays, even though the children would now render.
    rerender(
      <ErrorBoundary resetKey="/settings" fallback={() => <p>Crashed</p>}>
        <Fragile broken={false} />
      </ErrorBoundary>,
    );
    expect(screen.getByText("Crashed")).toBeVisible();

    rerender(
      <ErrorBoundary resetKey="/chat/thread-a" fallback={() => <p>Crashed</p>}>
        <Fragile broken={false} />
      </ErrorBoundary>,
    );
    expect(screen.getByText("View is fine")).toBeVisible();
    expect(screen.queryByText("Crashed")).not.toBeInTheDocument();
  });

  it("crashes again, not silently, when the new key meets the same fault", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const { rerender } = render(
      <ErrorBoundary resetKey="a" fallback={() => <p>Crashed</p>}>
        <Fragile broken />
      </ErrorBoundary>,
    );

    rerender(
      <ErrorBoundary resetKey="b" fallback={() => <p>Crashed</p>}>
        <Fragile broken />
      </ErrorBoundary>,
    );

    expect(screen.getByText("Crashed")).toBeVisible();
  });

  it("runs onReset before it renders the children again", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const user = userEvent.setup();
    let broken = true;
    const onReset = vi.fn(() => {
      broken = false;
    });
    // Reads `broken` when it renders, not when the element is created, so the
    // retry meets whatever onReset changed.
    function Widget() {
      return <Fragile broken={broken} />;
    }
    render(
      <ErrorBoundary
        onReset={onReset}
        fallback={({ reset }) => <button type="button" onClick={reset}>Try again</button>}
      >
        <Widget />
      </ErrorBoundary>,
    );

    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(onReset).toHaveBeenCalledTimes(1);
    expect(screen.getByText("View is fine")).toBeVisible();
  });

  it("copies the recorded details, and says so", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const writeText = vi.fn<(text: string) => Promise<void>>().mockResolvedValue();
    stubClipboard(writeText);
    render(
      <ErrorBoundary scope="workspace">
        <BrokenView />
      </ErrorBoundary>,
    );

    await act(async () => {
      screen.getByRole("button", { name: "Copy details" }).click();
    });

    expect(writeText).toHaveBeenCalledTimes(1);
    const copied = writeText.mock.calls[0][0];
    expect(copied).toContain("View: workspace");
    expect(copied).toContain("Error: Error: synthetic render failure");
    expect(copied).toContain("BrokenView");
    expect(await screen.findByText("Copied.")).toBeVisible();
  });

  it("says so when the clipboard refuses", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    stubClipboard(vi.fn<(text: string) => Promise<void>>().mockRejectedValue(new DOMException("Denied", "NotAllowedError")));
    render(
      <ErrorBoundary>
        <BrokenView />
      </ErrorBoundary>,
    );

    await act(async () => {
      screen.getByRole("button", { name: "Copy details" }).click();
    });

    expect(await screen.findByText("Could not copy to the clipboard.")).toBeVisible();
  });
});

describe("RouteBoundary", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.sessionStorage.clear();
  });

  it("keeps a crash to its own area and offers to try again and to reload", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const user = userEvent.setup();
    const onRetry = vi.fn();
    render(
      <div>
        <nav aria-label="Elsewhere"><button type="button">Still here</button></nav>
        <RouteBoundary name="Settings" scope="settings" resetKey="/settings" onRetry={onRetry}>
          <BrokenView />
        </RouteBoundary>
      </div>,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("Settings hit a problem");
    expect(screen.getByRole("button", { name: "Still here" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Reload workspace" })).toBeVisible();
    expect(readLastCrash()).toMatchObject({ scope: "settings" });

    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});

describe("InlineBoundary", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.sessionStorage.clear();
  });

  it("replaces one widget with a short note and lets it be tried again", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const user = userEvent.setup();
    let broken = true;
    function Widget() {
      return <Fragile broken={broken} />;
    }
    render(
      <InlineBoundary scope="chat-library" label="The chat list">
        <Widget />
      </InlineBoundary>,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("The chat list could not be shown.");
    expect(readLastCrash()).toMatchObject({ scope: "chat-library" });

    broken = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(screen.getByText("View is fine")).toBeVisible();
  });
});
