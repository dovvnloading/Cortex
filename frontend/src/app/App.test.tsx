import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { App } from "./App";
import { CortexApi } from "../api/client";

describe("App", () => {
  it("starts at the authenticated local-session boundary", () => {
    window.sessionStorage.clear();
    render(<App api={new CortexApi("/api/v1", window.fetch.bind(window))} />);
    expect(screen.getByRole("heading", { name: "Connect to Cortex" })).toBeInTheDocument();
  });
});
