import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";
import { useChatStore } from "../stores/useChatStore";
import { useModelStore } from "../stores/useModelStore";
import { useSettingsStore } from "../stores/useSettingsStore";
import { useUiStore } from "../stores/useUiStore";

// jsdom does not implement ResizeObserver. react-virtuoso (and the
// composer's own textarea auto-resize) rely on it purely to measure
// elements; a no-op observer is enough for both to mount without throwing.
if (typeof globalThis.ResizeObserver === "undefined") {
  class ResizeObserverMock implements ResizeObserver {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  globalThis.ResizeObserver = ResizeObserverMock;
}

// jsdom does not implement scrollIntoView. cmdk calls it whenever the
// selected command item changes; a no-op is enough for it to run in tests.
if (typeof Element.prototype.scrollIntoView !== "function") {
  Element.prototype.scrollIntoView = () => {};
}

// Zustand stores are module-level singletons: Vitest isolates modules per
// test *file*, not per test, so without an explicit reset a store mutated
// by one test would leak into every later test in the same file.
const initialChatState = useChatStore.getState();
const initialModelState = useModelStore.getState();
const initialSettingsState = useSettingsStore.getState();
const initialUiState = useUiStore.getState();

afterEach(() => {
  cleanup();
  useChatStore.setState(initialChatState, true);
  useModelStore.setState(initialModelState, true);
  useSettingsStore.setState(initialSettingsState, true);
  useUiStore.setState(initialUiState, true);
});
