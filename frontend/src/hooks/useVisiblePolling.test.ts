import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { pollDelay, useVisiblePolling } from "./useVisiblePolling";

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

  it("changing intervalMs does not fire an immediate tick", () => {
    const poll = vi.fn();
    const { rerender } = renderHook(
      ({ intervalMs }) => useVisiblePolling(poll, intervalMs, true),
      { initialProps: { intervalMs: 1000 } },
    );
    expect(poll).toHaveBeenCalledTimes(1);

    // The caller flips the interval on the very result a poll just delivered;
    // asking again straight away would repeat a question that was just answered.
    rerender({ intervalMs: 5000 });
    expect(poll).toHaveBeenCalledTimes(1);

    // The old cadence is gone and the new one is in force.
    vi.advanceTimersByTime(4999);
    expect(poll).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(1);
    expect(poll).toHaveBeenCalledTimes(2);
    vi.advanceTimersByTime(5000);
    expect(poll).toHaveBeenCalledTimes(3);
  });

  it("speeds up as well as slows down without an extra request", () => {
    const poll = vi.fn();
    const { rerender } = renderHook(
      ({ intervalMs }) => useVisiblePolling(poll, intervalMs, true),
      { initialProps: { intervalMs: 15_000 } },
    );
    poll.mockClear();

    rerender({ intervalMs: 2000 });
    expect(poll).not.toHaveBeenCalled();
    vi.advanceTimersByTime(2000);
    expect(poll).toHaveBeenCalledTimes(1);
  });

  it("polls straight away again when it is re-enabled", () => {
    const poll = vi.fn();
    const { rerender } = renderHook(
      ({ enabled }) => useVisiblePolling(poll, 1000, enabled),
      { initialProps: { enabled: true } },
    );
    rerender({ enabled: false });
    poll.mockClear();

    rerender({ enabled: true });
    expect(poll).toHaveBeenCalledTimes(1);
  });

  it("keeps its cadence when a poll never answers", async () => {
    const poll = vi.fn(() => new Promise<void>(() => undefined));
    renderHook(() => useVisiblePolling(poll, 1000, true, { backoff: true }));

    await vi.advanceTimersByTimeAsync(3000);

    // The timer, not the request, sets the pace: a hung request must not stop the polls after it.
    expect(poll).toHaveBeenCalledTimes(4);
  });
});

describe("pollDelay", () => {
  it("keeps the base interval for the first two failures", () => {
    expect(pollDelay(2000, 0)).toBe(2000);
    expect(pollDelay(2000, 1)).toBe(2000);
    expect(pollDelay(2000, 2)).toBe(2000);
  });

  it("doubles from the third failure, up to thirty seconds", () => {
    expect([3, 4, 5, 6, 7, 8].map((failures) => pollDelay(2000, failures))).toEqual([4000, 8000, 16_000, 30_000, 30_000, 30_000]);
  });

  it("never waits less than the base interval, even when that already exceeds the cap", () => {
    expect(pollDelay(60_000, 0)).toBe(60_000);
    expect(pollDelay(60_000, 9)).toBe(60_000);
  });
});

describe("useVisiblePolling error backoff", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  /** Milliseconds since the hook mounted at which each call happened. */
  function recordCalls(outcome: (call: number) => void | Promise<void>) {
    const start = Date.now();
    const at: number[] = [];
    const poll = vi.fn(() => {
      at.push(Date.now() - start);
      return outcome(at.length);
    });
    return { poll, at };
  }

  it("doubles the wait after three failures in a row, up to thirty seconds", async () => {
    const { poll, at } = recordCalls(() => Promise.reject(new Error("backend down")));
    renderHook(() => useVisiblePolling(poll, 1000, true, { backoff: true }));

    await vi.advanceTimersByTimeAsync(70_000);

    // Three at the normal pace, then 2s, 4s, 8s, 16s, and 30s from there on.
    expect(at).toEqual([0, 1000, 2000, 4000, 8000, 16_000, 32_000, 62_000]);
  });

  it("returns to the normal interval after one success", async () => {
    const { poll, at } = recordCalls((call) => (call <= 4 ? Promise.reject(new Error("blip")) : Promise.resolve()));
    renderHook(() => useVisiblePolling(poll, 1000, true, { backoff: true }));

    // Calls 1-4 fail (at 0, 1s, 2s, 4s), so the wait grew to 4s; call 5 at 8s succeeds.
    await vi.advanceTimersByTimeAsync(8000);
    expect(at).toEqual([0, 1000, 2000, 4000, 8000]);

    await vi.advanceTimersByTimeAsync(3000);
    expect(at).toEqual([0, 1000, 2000, 4000, 8000, 9000, 10_000, 11_000]);
  });

  it("counts a callback that throws synchronously as a failure", async () => {
    const { poll, at } = recordCalls(() => { throw new Error("boom"); });
    renderHook(() => useVisiblePolling(poll, 1000, true, { backoff: true }));

    await vi.advanceTimersByTimeAsync(6000);

    expect(at).toEqual([0, 1000, 2000, 4000]);
  });

  it("does not slow a poll that never fails, or one that fails only twice", async () => {
    const { poll, at } = recordCalls((call) => (call === 1 || call === 2 ? Promise.reject(new Error("blip")) : Promise.resolve()));
    renderHook(() => useVisiblePolling(poll, 1000, true, { backoff: true }));

    await vi.advanceTimersByTimeAsync(5000);

    expect(at).toEqual([0, 1000, 2000, 3000, 4000, 5000]);
  });

  it("leaves the cadence alone without the option, and does not leave a rejection unhandled", async () => {
    const { poll, at } = recordCalls(() => Promise.reject(new Error("backend down")));
    renderHook(() => useVisiblePolling(poll, 1000, true));

    await vi.advanceTimersByTimeAsync(6000);

    // An unhandled rejection would fail this run, so reaching here also proves it was caught.
    expect(at).toEqual([0, 1000, 2000, 3000, 4000, 5000, 6000]);
  });

  it("starts again at the normal pace after being switched off and on", async () => {
    const { poll, at } = recordCalls(() => Promise.reject(new Error("backend down")));
    const { rerender } = renderHook(
      ({ enabled }) => useVisiblePolling(poll, 1000, enabled, { backoff: true }),
      { initialProps: { enabled: true } },
    );
    await vi.advanceTimersByTimeAsync(4000);
    expect(at).toEqual([0, 1000, 2000, 4000]);

    rerender({ enabled: false });
    poll.mockClear();
    rerender({ enabled: true });
    await vi.advanceTimersByTimeAsync(2000);

    // A fresh start owes nothing to the failures before it: an immediate poll,
    // then the normal one-second pace (a leftover four-second wait would give one).
    expect(poll).toHaveBeenCalledTimes(3);
  });

  it("still refreshes at once when the window comes back, however long the wait has grown", async () => {
    const { poll, at } = recordCalls(() => Promise.reject(new Error("backend down")));
    renderHook(() => useVisiblePolling(poll, 1000, true, { backoff: true }));
    await vi.advanceTimersByTimeAsync(20_000);
    const before = at.length;

    setVisibility("hidden");
    setVisibility("visible");

    expect(at.length).toBe(before + 1);
  });
});
