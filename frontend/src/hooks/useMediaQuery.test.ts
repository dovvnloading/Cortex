import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { REDUCED_MOTION_QUERY, useMediaQuery, usePrefersReducedMotion } from "./useMediaQuery";

type Listener = () => void;

/** A controllable stand-in for one media query; `flip` changes the answer and notifies listeners. */
function installMatchMedia(initial: boolean, { modern = true }: { modern?: boolean } = {}) {
  let matches = initial;
  const listeners = new Set<Listener>();
  const list = {
    get matches() { return matches; },
    media: "",
    onchange: null,
    addEventListener: modern ? vi.fn((_type: string, listener: Listener) => { listeners.add(listener); }) : undefined,
    removeEventListener: modern ? vi.fn((_type: string, listener: Listener) => { listeners.delete(listener); }) : undefined,
    addListener: vi.fn((listener: Listener) => { listeners.add(listener); }),
    removeListener: vi.fn((listener: Listener) => { listeners.delete(listener); }),
    dispatchEvent: vi.fn(),
  };
  const matchMedia = vi.fn((query: string) => ({ ...list, media: query, get matches() { return matches; } }));
  Object.defineProperty(window, "matchMedia", { configurable: true, writable: true, value: matchMedia });
  return {
    matchMedia,
    list,
    listeners,
    flip(next: boolean) {
      matches = next;
      for (const listener of [...listeners]) listener();
    },
  };
}

describe("useMediaQuery", () => {
  afterEach(() => {
    Reflect.deleteProperty(window, "matchMedia");
  });

  it("reports the current answer and follows changes", () => {
    const media = installMatchMedia(false);
    const { result } = renderHook(() => useMediaQuery("(min-width: 900px)"));
    expect(result.current).toBe(false);
    expect(media.matchMedia).toHaveBeenCalledWith("(min-width: 900px)");

    act(() => media.flip(true));
    expect(result.current).toBe(true);

    act(() => media.flip(false));
    expect(result.current).toBe(false);
  });

  it("stops listening when the component goes away", () => {
    const media = installMatchMedia(false);
    const { unmount } = renderHook(() => useMediaQuery("(min-width: 900px)"));
    expect(media.listeners.size).toBe(1);

    unmount();

    expect(media.listeners.size).toBe(0);
  });

  it("does not re-subscribe on every render", () => {
    const media = installMatchMedia(false);
    const { rerender } = renderHook(() => useMediaQuery("(min-width: 900px)"));
    const subscriptions = media.list.addEventListener?.mock.calls.length;

    rerender();
    rerender();

    expect(media.list.addEventListener?.mock.calls.length).toBe(subscriptions);
  });

  it("falls back to the deprecated listener pair on older webviews", () => {
    const media = installMatchMedia(false, { modern: false });
    const { result, unmount } = renderHook(() => useMediaQuery("(min-width: 900px)"));

    act(() => media.flip(true));
    expect(result.current).toBe(true);

    unmount();
    expect(media.list.removeListener).toHaveBeenCalledTimes(1);
  });

  it("matches nothing where matchMedia does not exist", () => {
    expect(typeof window.matchMedia).not.toBe("function");
    const { result } = renderHook(() => useMediaQuery("(min-width: 900px)"));
    expect(result.current).toBe(false);
  });
});

describe("usePrefersReducedMotion", () => {
  afterEach(() => {
    Reflect.deleteProperty(window, "matchMedia");
  });

  it("asks for the reduced-motion preference", () => {
    const media = installMatchMedia(true);
    const { result } = renderHook(() => usePrefersReducedMotion());

    expect(media.matchMedia).toHaveBeenCalledWith(REDUCED_MOTION_QUERY);
    expect(REDUCED_MOTION_QUERY).toBe("(prefers-reduced-motion: reduce)");
    expect(result.current).toBe(true);
  });

  it("is false when the person has not asked for it", () => {
    installMatchMedia(false);
    const { result } = renderHook(() => usePrefersReducedMotion());
    expect(result.current).toBe(false);
  });
});
