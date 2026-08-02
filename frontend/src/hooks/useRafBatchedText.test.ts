import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useRafBatchedText } from "./useRafBatchedText";

describe("useRafBatchedText", () => {
  it("coalesces rapid push() calls into a single state update per animation frame", async () => {
    const { result } = renderHook(() => useRafBatchedText());
    expect(result.current.text).toBe("");

    act(() => {
      result.current.push("Hel");
      result.current.push("lo");
      result.current.push(" world");
    });

    await waitFor(() => expect(result.current.text).toBe("Hello world"));
  });

  it("reset() replaces the buffer and text immediately", async () => {
    const { result } = renderHook(() => useRafBatchedText());
    act(() => result.current.push("draft"));
    await waitFor(() => expect(result.current.text).toBe("draft"));

    act(() => result.current.reset());
    expect(result.current.text).toBe("");

    act(() => result.current.push("next"));
    await waitFor(() => expect(result.current.text).toBe("next"));
  });
});
