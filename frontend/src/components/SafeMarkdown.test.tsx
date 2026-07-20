import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SafeMarkdown } from "./SafeMarkdown";

describe("SafeMarkdown", () => {
  it("renders markdown as safe text and allows only controlled links", () => {
    render(<><SafeMarkdown content={'<script>alert(1)</script>'} /><SafeMarkdown content={'hello [bad](javascript:alert(1)) [good](https://example.com) ![image](https://example.com/a.png)'} /></>);

    expect(document.querySelector("script")).not.toBeInTheDocument();
    expect(document.querySelector("img")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "good" })).toHaveAttribute("href", "https://example.com/");
    expect(screen.queryByRole("link", { name: "bad" })).not.toBeInTheDocument();
  });
});
