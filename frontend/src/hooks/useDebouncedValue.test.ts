import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useDebouncedValue } from "./useDebouncedValue";

describe("useDebouncedValue", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("starts with the value it was given", () => {
    const { result } = renderHook(() => useDebouncedValue("first", 100));
    expect(result.current).toBe("first");
  });

  it("follows a change only after it has stopped changing for the delay", () => {
    const { result, rerender } = renderHook(({ value }) => useDebouncedValue(value, 100), { initialProps: { value: "a" } });

    rerender({ value: "ab" });
    act(() => { vi.advanceTimersByTime(99); });
    expect(result.current).toBe("a");

    act(() => { vi.advanceTimersByTime(1); });
    expect(result.current).toBe("ab");
  });

  it("restarts the wait for every change, and settles on the last one", () => {
    const { result, rerender } = renderHook(({ value }) => useDebouncedValue(value, 100), { initialProps: { value: "a" } });

    rerender({ value: "ab" });
    act(() => { vi.advanceTimersByTime(60); });
    rerender({ value: "abc" });
    act(() => { vi.advanceTimersByTime(60); });
    // 120ms since the first change, but only 60 since the latest.
    expect(result.current).toBe("a");

    act(() => { vi.advanceTimersByTime(40); });
    expect(result.current).toBe("abc");
  });

  it("does not update after it is unmounted", () => {
    const { rerender, unmount } = renderHook(({ value }) => useDebouncedValue(value, 100), { initialProps: { value: "a" } });
    rerender({ value: "b" });

    unmount();

    expect(vi.getTimerCount()).toBe(0);
  });
});
