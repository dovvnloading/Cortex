import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { usePageEscape } from "./usePageEscape";

function press(options: Partial<KeyboardEventInit> = {}, target: EventTarget = document.body) {
  const event = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true, ...options });
  target.dispatchEvent(event);
  return event;
}

const added: HTMLElement[] = [];
function attach<T extends HTMLElement>(element: T): T {
  document.body.appendChild(element);
  added.push(element);
  return element;
}

afterEach(() => {
  for (const element of added.splice(0)) element.remove();
});

describe("usePageEscape", () => {
  it("runs the handler on Escape from the page, and takes the key", () => {
    const handler = vi.fn();
    renderHook(() => usePageEscape(handler, true));

    const event = press();

    expect(handler).toHaveBeenCalledTimes(1);
    expect(event.defaultPrevented).toBe(true);
  });

  it("does nothing while disabled, and starts when enabled", () => {
    const handler = vi.fn();
    const { rerender } = renderHook(({ enabled }) => usePageEscape(handler, enabled), { initialProps: { enabled: false } });

    const ignored = press();
    expect(handler).not.toHaveBeenCalled();
    expect(ignored.defaultPrevented).toBe(false);

    rerender({ enabled: true });
    press();
    expect(handler).toHaveBeenCalledTimes(1);

    rerender({ enabled: false });
    press();
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("ignores other keys", () => {
    const handler = vi.fn();
    renderHook(() => usePageEscape(handler, true));

    press({ key: "Enter" });
    press({ key: "e" });

    expect(handler).not.toHaveBeenCalled();
  });

  it("leaves an Escape that something already handled", () => {
    const handler = vi.fn();
    renderHook(() => usePageEscape(handler, true));
    const button = attach(document.createElement("button"));
    // What the composer (and any control that owns Escape) does before it gets here.
    button.addEventListener("keydown", (event) => event.preventDefault());

    press({}, button);

    expect(handler).not.toHaveBeenCalled();
  });

  it.each(["dialog", "alertdialog", "menu", "listbox"])("leaves Escape to an open %s", (role) => {
    const handler = vi.fn();
    renderHook(() => usePageEscape(handler, true));
    const overlay = attach(document.createElement("div"));
    overlay.setAttribute("role", role);

    const event = press();

    expect(handler).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);

    overlay.remove();
    press();
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("leaves Escape to a text field, where it cancels the edit", () => {
    const handler = vi.fn();
    renderHook(() => usePageEscape(handler, true));

    press({}, attach(document.createElement("input")));
    press({}, attach(document.createElement("textarea")));
    const editable = attach(document.createElement("div"));
    Object.defineProperty(editable, "isContentEditable", { value: true });
    press({}, editable);

    expect(handler).not.toHaveBeenCalled();
  });

  it("leaves Escape to a notification whose button has focus, and takes it again elsewhere", () => {
    const handler = vi.fn();
    renderHook(() => usePageEscape(handler, true));
    const notification = attach(document.createElement("div"));
    notification.setAttribute("data-keeps-escape", "");
    const undo = notification.appendChild(document.createElement("button"));

    const inside = press({}, undo);

    expect(handler).not.toHaveBeenCalled();
    expect(inside.defaultPrevented).toBe(false);

    press({}, attach(document.createElement("button")));
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("ignores auto-repeat, composition, and modifiers", () => {
    const handler = vi.fn();
    renderHook(() => usePageEscape(handler, true));

    press({ repeat: true });
    press({ isComposing: true });
    press({ ctrlKey: true });
    press({ metaKey: true });
    press({ altKey: true });
    press({ shiftKey: true });

    expect(handler).not.toHaveBeenCalled();
  });

  it("calls the latest handler without re-registering", () => {
    const first = vi.fn();
    const second = vi.fn();
    const add = vi.spyOn(document, "addEventListener");
    const { rerender } = renderHook(({ handler }) => usePageEscape(handler, true), { initialProps: { handler: first } });
    const registrations = add.mock.calls.filter(([type]) => type === "keydown").length;

    rerender({ handler: second });
    press();

    expect(add.mock.calls.filter(([type]) => type === "keydown")).toHaveLength(registrations);
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
    add.mockRestore();
  });

  it("removes its listener on unmount", () => {
    const handler = vi.fn();
    const { unmount } = renderHook(() => usePageEscape(handler, true));

    unmount();
    press();

    expect(handler).not.toHaveBeenCalled();
  });
});
