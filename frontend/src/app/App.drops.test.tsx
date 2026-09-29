import { render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { App } from "./App";
import { CortexApi } from "../api/client";

/** Fire a drag event at the document the way the browser does, with a stand-in for the transfer. */
function dragAtDocument(type: "dragover" | "drop", types: string[]) {
  const event = new Event(type, { bubbles: true, cancelable: true });
  const dataTransfer = { types, dropEffect: "copy" };
  Object.defineProperty(event, "dataTransfer", { value: dataTransfer });
  document.body.dispatchEvent(event);
  return { event, dataTransfer };
}

describe("App file drops", () => {
  afterEach(() => {
    window.sessionStorage.clear();
    window.history.replaceState({}, "", "/");
  });

  it("drop events on the document are default-prevented", () => {
    // Left alone, WebView2 opens a dropped file in place of Cortex and there
    // is no way back. The onboarding screen is rendered here on purpose: the
    // protection must not depend on the workspace having loaded.
    render(<App api={new CortexApi("/api/v1", window.fetch.bind(window))} />);

    const over = dragAtDocument("dragover", ["Files"]);
    const dropped = dragAtDocument("drop", ["Files"]);

    expect(over.event.defaultPrevented).toBe(true);
    expect(over.dataTransfer.dropEffect).toBe("none");
    expect(dropped.event.defaultPrevented).toBe(true);
  });

  it("leaves dragged text alone so it can still be dropped into a field", () => {
    render(<App api={new CortexApi("/api/v1", window.fetch.bind(window))} />);

    expect(dragAtDocument("dragover", ["text/plain"]).event.defaultPrevented).toBe(false);
    expect(dragAtDocument("drop", ["text/plain"]).event.defaultPrevented).toBe(false);
  });

  it("stops intercepting drops once the app is gone", () => {
    const { unmount } = render(<App api={new CortexApi("/api/v1", window.fetch.bind(window))} />);
    unmount();

    expect(dragAtDocument("drop", ["Files"]).event.defaultPrevented).toBe(false);
  });
});
