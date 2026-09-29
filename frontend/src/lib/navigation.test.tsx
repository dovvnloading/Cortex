import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NavigationLink } from "../features/shell/NavigationLink";
import {
  chatPath,
  navigate,
  parseAppRoute,
  registerNavigationGuard,
  usePathname,
  type NavigationGuard,
} from "./navigation";

afterEach(() => {
  window.history.replaceState({}, "", "/");
});

describe("application navigation", () => {
  it("parses the supported local routes and safely decodes chat identifiers", () => {
    expect(parseAppRoute("/")).toEqual({ kind: "chat", threadId: null });
    expect(parseAppRoute("/chat/new")).toEqual({ kind: "chat", threadId: null });
    expect(parseAppRoute("/settings/")).toEqual({ kind: "settings" });
    expect(parseAppRoute("/chat/thread%20one")).toEqual({ kind: "chat", threadId: "thread one" });
    expect(parseAppRoute("/chat/%E0%A4%A")).toEqual({ kind: "not-found" });
    expect(parseAppRoute("/outside")).toEqual({ kind: "not-found" });
    expect(chatPath("thread/one")).toBe("/chat/thread%2Fone");
  });

  it("updates subscribers for programmatic and link navigation", async () => {
    const user = userEvent.setup();
    function Location() {
      return <output aria-label="Current path">{usePathname()}</output>;
    }

    render(
      <>
        <NavigationLink to="/settings">Open settings</NavigationLink>
        <Location />
      </>,
    );

    await user.click(screen.getByRole("link", { name: "Open settings" }));
    expect(screen.getByLabelText("Current path")).toHaveTextContent("/settings");
    expect(window.location.pathname).toBe("/settings");

    act(() => navigate("/chat/new", { replace: true }));
    expect(screen.getByLabelText("Current path")).toHaveTextContent("/chat/new");
  });

  it("rejects external and protocol-relative destinations", () => {
    expect(() => navigate("https://example.com")).toThrow(/same-origin/);
    expect(() => navigate("//example.com")).toThrow(/same-origin/);
    expect(() => navigate("/\\example.com")).toThrow(/same-origin/);
  });
});

describe("navigation guards", () => {
  const unregister: (() => void)[] = [];
  const guard = (check: NavigationGuard) => {
    const remove = registerNavigationGuard(check);
    unregister.push(remove);
    return remove;
  };
  afterEach(() => {
    while (unregister.length) unregister.pop()?.();
  });

  /** Let a settled guard promise's continuation run. */
  const settle = () => act(async () => { await Promise.resolve(); await Promise.resolve(); });

  it("navigate() stays synchronous when no guard objects", () => {
    guard(() => true);

    navigate("/settings");

    expect(window.location.pathname).toBe("/settings");
  });

  it("navigate() stays where it is when a guard refuses", () => {
    const refuse = vi.fn(() => false);
    guard(refuse);

    navigate("/settings");

    expect(refuse).toHaveBeenCalledWith("/settings");
    expect(window.location.pathname).toBe("/");
  });

  it("navigate() defers to a registered guard", async () => {
    let answer!: (allowed: boolean) => void;
    guard(() => new Promise<boolean>((resolve) => { answer = resolve; }));
    const events = vi.fn();
    window.addEventListener("cortex:navigation", events);

    navigate("/settings?tab=model#top");
    // Nothing moves while the guard is still asking.
    expect(window.location.pathname).toBe("/");
    expect(events).not.toHaveBeenCalled();

    answer(true);
    await settle();

    expect(window.location.pathname).toBe("/settings");
    expect(window.location.search).toBe("?tab=model");
    expect(window.location.hash).toBe("#top");
    expect(events).toHaveBeenCalledTimes(1);
    window.removeEventListener("cortex:navigation", events);
  });

  it("drops a deferred navigation the guard turns down", async () => {
    let answer!: (allowed: boolean) => void;
    guard(() => new Promise<boolean>((resolve) => { answer = resolve; }));

    navigate("/settings");
    answer(false);
    await settle();

    expect(window.location.pathname).toBe("/");
  });

  it("keeps replace semantics when the guard answers later", async () => {
    let answer!: (allowed: boolean) => void;
    guard(() => new Promise<boolean>((resolve) => { answer = resolve; }));
    const push = vi.spyOn(window.history, "pushState");
    const replace = vi.spyOn(window.history, "replaceState");

    navigate("/settings", { replace: true });
    answer(true);
    await settle();

    expect(replace).toHaveBeenCalledTimes(1);
    expect(push).not.toHaveBeenCalled();
  });

  it("lets only the newest navigation complete", async () => {
    const answers: ((allowed: boolean) => void)[] = [];
    guard(() => new Promise<boolean>((resolve) => { answers.push(resolve); }));

    navigate("/settings");
    navigate("/chat/new");
    // The older question is answered last; it must not win.
    answers[1](true);
    await settle();
    answers[0](true);
    await settle();

    expect(window.location.pathname).toBe("/chat/new");
  });

  it("does not ask about a navigation to the page it is already on", () => {
    const check = vi.fn(() => false);
    guard(check);

    navigate("/");

    expect(check).not.toHaveBeenCalled();
  });

  it("asks every guard in turn and stops at the first refusal", () => {
    const first = vi.fn(() => true);
    const second = vi.fn(() => false);
    const third = vi.fn(() => true);
    guard(first);
    guard(second);
    guard(third);

    navigate("/settings");

    expect(first).toHaveBeenCalledTimes(1);
    expect(second).toHaveBeenCalledTimes(1);
    expect(third).not.toHaveBeenCalled();
    expect(window.location.pathname).toBe("/");
  });

  it("does not trap the user behind a guard that throws or rejects", async () => {
    guard(() => { throw new Error("broken guard"); });
    navigate("/settings");
    expect(window.location.pathname).toBe("/settings");

    guard(() => Promise.reject(new Error("broken async guard")));
    navigate("/chat/new");
    await settle();
    expect(window.location.pathname).toBe("/chat/new");
  });

  it("stops asking once the guard is removed", () => {
    const check = vi.fn(() => false);
    const remove = guard(check);
    remove();

    navigate("/settings");

    expect(check).not.toHaveBeenCalled();
    expect(window.location.pathname).toBe("/settings");
  });

  it("holds back a link click as well as a programmatic navigation", async () => {
    const user = userEvent.setup();
    guard(() => false);
    render(<NavigationLink to="/settings">Open settings</NavigationLink>);

    await user.click(screen.getByRole("link", { name: "Open settings" }));

    expect(window.location.pathname).toBe("/");
  });
});
