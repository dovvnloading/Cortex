import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useVisiblePolling } from "./useVisiblePolling";

function setVisibility(state: DocumentVisibilityState) {
  Object.defineProperty(document, "visibilityState", { value: state, configurable: true });
  document.dispatchEvent(new Event("visibilitychange"));
}

describe("useVisiblePolling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("refreshes immediately and then on the interval", () => {
    const poll = vi.fn();
    renderHook(() => useVisiblePolling(poll, 1000, true));

    expect(poll).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(3000);
    expect(poll).toHaveBeenCalledTimes(4);
  });

  it("does nothing at all while disabled", () => {
    const poll = vi.fn();
    renderHook(() => useVisiblePolling(poll, 1000, false));

    vi.advanceTimersByTime(5000);
    expect(poll).not.toHaveBeenCalled();
  });

  it("skips ticks while the window is hidden and catches up on return", () => {
    const poll = vi.fn();
    renderHook(() => useVisiblePolling(poll, 1000, true));
    poll.mockClear();

    setVisibility("hidden");
    vi.advanceTimersByTime(5000);
    expect(poll).not.toHaveBeenCalled();

    // Returning refreshes straight away rather than leaving the UI stale
    // until the next tick lands.
    setVisibility("visible");
    expect(poll).toHaveBeenCalledTimes(1);
  });

  it("stops polling when it is disabled after having run", () => {
    const poll = vi.fn();
    const { rerender } = renderHook(
      ({ enabled }) => useVisiblePolling(poll, 1000, enabled),
      { initialProps: { enabled: true } },
    );
    vi.advanceTimersByTime(2000);
    const callsWhileEnabled = poll.mock.calls.length;

    rerender({ enabled: false });
    vi.advanceTimersByTime(5000);

    expect(poll).toHaveBeenCalledTimes(callsWhileEnabled);
  });

  it("calls the latest callback without restarting the interval", () => {
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = renderHook(
      ({ callback }) => useVisiblePolling(callback, 1000, true),
      { initialProps: { callback: first } },
    );
    expect(first).toHaveBeenCalledTimes(1);

    // A new inline callback each render must not re-run the immediate
    // refresh, which would turn every parent render into a request.
    rerender({ callback: second });
    expect(second).not.toHaveBeenCalled();

    vi.advanceTimersByTime(1000);
    expect(second).toHaveBeenCalledTimes(1);
    expect(first).toHaveBeenCalledTimes(1);
  });

  it("removes its listener and timer on unmount", () => {
    const poll = vi.fn();
    const { unmount } = renderHook(() => useVisiblePolling(poll, 1000, true));
    poll.mockClear();

    unmount();
    vi.advanceTimersByTime(5000);
    setVisibility("visible");

    expect(poll).not.toHaveBeenCalled();
  });
});
