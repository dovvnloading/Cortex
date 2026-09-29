import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useThrottledText } from "./useThrottledText";

describe("useThrottledText", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("starts with the text it is given", () => {
    const { result } = renderHook(() => useThrottledText("hello", 100, true));
    expect(result.current).toBe("hello");
  });

  it("limits how often the returned text changes while text streams in", () => {
    let text = "a";
    const { result, rerender } = renderHook(() => useThrottledText(text, 100, true));
    const seen: string[] = [result.current];
    const record = () => {
      if (seen[seen.length - 1] !== result.current) seen.push(result.current);
    };

    // 60 frames in one second, each appending a token.
    for (let frame = 0; frame < 60; frame += 1) {
      text += "b";
      rerender();
      act(() => { vi.advanceTimersByTime(16); });
      record();
    }

    // About ten updates, not sixty: parsing the Markdown runs once per change.
    expect(seen.length - 1).toBeLessThanOrEqual(11);
    expect(seen.length - 1).toBeGreaterThanOrEqual(8);
  });

  it("always catches up to the latest text once the stream goes quiet", () => {
    let text = "";
    const { result, rerender } = renderHook(() => useThrottledText(text, 100, true));
    for (const token of ["The ", "quick ", "brown ", "fox"]) {
      text += token;
      rerender();
    }
    expect(result.current).not.toBe("The quick brown fox");

    act(() => { vi.advanceTimersByTime(100); });

    expect(result.current).toBe("The quick brown fox");
  });

  it("returns the exact text immediately when throttling is switched off", () => {
    let text = "partial";
    let enabled = true;
    const { result, rerender } = renderHook(() => useThrottledText(text, 100, enabled));
    text = "partial answer, now complete.";
    rerender();
    expect(result.current).toBe("partial");

    // The stream finished: the final text must not wait out the interval.
    enabled = false;
    rerender();
    expect(result.current).toBe("partial answer, now complete.");
  });

  it("does not leave a timer running after unmount", () => {
    let text = "a";
    const { rerender, unmount } = renderHook(() => useThrottledText(text, 100, true));
    text = "ab";
    rerender();
    expect(vi.getTimerCount()).toBe(1);

    unmount();

    expect(vi.getTimerCount()).toBe(0);
  });

  it("follows text that is replaced rather than appended", () => {
    let text = "first job output";
    const { result, rerender } = renderHook(() => useThrottledText(text, 100, true));
    text = "";
    rerender();
    act(() => { vi.advanceTimersByTime(100); });
    expect(result.current).toBe("");
  });
});
