import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useUiStore } from "../stores/useUiStore";
import { ToastProvider } from "./ToastProvider";

const notify = (...args: Parameters<ReturnType<typeof useUiStore.getState>["notify"]>) => {
  let id = 0;
  act(() => { id = useUiStore.getState().notify(...args); });
  return id;
};

const advance = (ms: number) => act(() => { vi.advanceTimersByTime(ms); });

describe("ToastProvider", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    useUiStore.setState({ toasts: [] });
  });

  afterEach(() => {
    act(() => { useUiStore.setState({ toasts: [] }); });
    vi.useRealTimers();
  });

  it("keeps an error toast past ten seconds and removes it with its Dismiss button", () => {
    render(<ToastProvider><p>app</p></ToastProvider>);

    notify("The download failed: the checksum did not match.", "error");
    advance(10_000);

    expect(screen.getByRole("alert")).toHaveTextContent("The download failed: the checksum did not match.");
    fireEvent.click(screen.getByRole("button", { name: "Dismiss notification" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("expires a success toast and announces it as a status without a dismiss control", () => {
    render(<ToastProvider><p>app</p></ToastProvider>);

    notify("Chat renamed.", "success");
    expect(screen.getByRole("status")).toHaveTextContent("Chat renamed.");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Dismiss notification" })).not.toBeInTheDocument();

    advance(4500);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("does not make the region itself a live region, so nothing is read twice", () => {
    const { container } = render(<ToastProvider><p>app</p></ToastProvider>);
    notify("Saved.", "success");

    const region = container.ownerDocument.querySelector(".toast-region");
    expect(region).not.toBeNull();
    expect(region).not.toHaveAttribute("aria-live");
    expect(region).not.toHaveAttribute("aria-atomic");
  });

  it("waits while a toast is hovered, then finishes its countdown", () => {
    render(<ToastProvider><p>app</p></ToastProvider>);

    notify("Model ready.", "info");
    advance(4000);
    fireEvent.mouseEnter(screen.getByRole("status"));
    advance(30_000);
    expect(screen.getByRole("status")).toBeVisible();

    fireEvent.mouseLeave(screen.getByRole("status"));
    advance(499);
    expect(screen.getByRole("status")).toBeVisible();
    advance(1);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("pauses while keyboard focus is on the toast's action and resumes when it leaves", () => {
    render(<ToastProvider><button type="button">elsewhere</button></ToastProvider>);

    notify("Chat deleted.", "success", { action: { label: "Undo", onAction: vi.fn() }, durationMs: 6000 });
    act(() => { screen.getByRole("button", { name: "Undo" }).focus(); });
    advance(60_000);
    expect(screen.getByRole("status")).toBeVisible();

    act(() => { screen.getByRole("button", { name: "elsewhere" }).focus(); });
    advance(6000);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("runs a toast's action from its button", () => {
    const onAction = vi.fn();
    render(<ToastProvider><p>app</p></ToastProvider>);

    notify("Chat deleted.", "success", { action: { label: "Undo", onAction } });
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));

    expect(onAction).toHaveBeenCalledOnce();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
