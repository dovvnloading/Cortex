import { afterEach } from "vitest";
import { useChatStore } from "../stores/useChatStore";
import { useModelStore } from "../stores/useModelStore";
import { useSettingsStore } from "../stores/useSettingsStore";
import { useUiStore } from "../stores/useUiStore";

// Files that never touch a DOM opt out of jsdom with a
// `// @vitest-environment node` docblock. Constructing a jsdom for them costs
// far more than they take to run, so everything below that needs a document
// is loaded only when there is one.
const hasDom = typeof document !== "undefined";

let cleanup: (() => void) | undefined;

if (hasDom) {
  await import("@testing-library/jest-dom/vitest");
  ({ cleanup } = await import("@testing-library/react"));

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
}

// Zustand stores are module-level singletons: Vitest isolates modules per
// test *file*, not per test, so without an explicit reset a store mutated
// by one test would leak into every later test in the same file. This holds
// in both environments.
const initialChatState = useChatStore.getState();
const initialModelState = useModelStore.getState();
const initialSettingsState = useSettingsStore.getState();
const initialUiState = useUiStore.getState();

afterEach(() => {
  cleanup?.();
  useChatStore.setState(initialChatState, true);
  useModelStore.setState(initialModelState, true);
  useSettingsStore.setState(initialSettingsState, true);
  useUiStore.setState(initialUiState, true);
});
