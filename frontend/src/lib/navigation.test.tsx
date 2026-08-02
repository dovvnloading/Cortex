import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { NavigationLink } from "../features/shell/NavigationLink";
import {
  chatPath,
  navigate,
  parseAppRoute,
  usePathname,
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
