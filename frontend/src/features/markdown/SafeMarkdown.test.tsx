import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SafeMarkdown } from "./SafeMarkdown";

describe("SafeMarkdown", () => {
  it("renders markdown as safe text and allows only controlled links", () => {
    render(<><SafeMarkdown content={'<script>alert(1)</script>'} /><SafeMarkdown content={'hello [bad](javascript:alert(1)) [good](https://example.com) ![image](https://example.com/a.png)'} /></>);

    expect(document.querySelector("script")).not.toBeInTheDocument();
    expect(document.querySelector("img")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "good" })).toHaveAttribute("href", "https://example.com/");
    expect(screen.queryByRole("link", { name: "bad" })).not.toBeInTheDocument();
  });

  it("gives fenced code a language label and a valid copy control", () => {
    render(<SafeMarkdown content={"```ts\nconst answer = 42;\n```"} />);

    expect(screen.getByText("ts")).toBeInTheDocument();
    const copy = screen.getByRole("button", { name: "Copy ts code" });
    expect(copy).toBeInTheDocument();
    expect(copy.closest("code")).toBeNull();
    // Highlighting splits the line across multiple <span class="hljs-*">
    // tokens, so the exact text is asserted via textContent, not getByText.
    // The DOM (unlike the copy value) keeps the source's trailing newline.
    expect(document.querySelector("code")?.textContent).toBe("const answer = 42;\n");
  });

  it("keeps the code toolbar outside the scrolling <pre>", () => {
    // Regression guard: the toolbar used to render inside <pre>, where it
    // inherited the code's max-content width. On a block wider than the
    // column that pushed the right-aligned Copy button off-screen, reachable
    // only by scrolling the code all the way right. <pre> must scroll alone.
    render(<SafeMarkdown content={"```ts\nconst answer = 42;\n```"} />);

    const toolbar = document.querySelector(".code-block-toolbar");
    const pre = document.querySelector("pre");
    expect(toolbar).not.toBeNull();
    expect(pre).not.toBeNull();
    expect(pre?.contains(toolbar as Node)).toBe(false);
    expect(toolbar?.parentElement).toBe(pre?.parentElement);
    expect(toolbar?.parentElement).toHaveClass("code-block");
  });

  it("syntax-highlights a finalized (default) code block", () => {
    render(<SafeMarkdown content={"```ts\nconst answer = 42;\n```"} />);

    const code = document.querySelector("code");
    expect(code?.className).toContain("hljs");
    expect(code?.querySelector("[class*='hljs-']")).not.toBeNull();
  });

  it("skips highlighting while a message is still streaming (finalized=false)", () => {
    render(<SafeMarkdown content={"```ts\nconst answer = 42;\n```"} finalized={false} />);

    const code = document.querySelector("code");
    expect(code?.className ?? "").not.toContain("hljs");
    expect(code?.querySelector("[class*='hljs-']")).toBeNull();
    expect(code?.textContent).toBe("const answer = 42;\n");
  });

  it("copies the exact source text (via childrenToText) even once the code block is highlighted", async () => {
    if (!navigator.clipboard) {
      Object.defineProperty(navigator, "clipboard", { value: {}, configurable: true });
    }
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator.clipboard, "writeText", { value: writeText, configurable: true, writable: true });
    render(<SafeMarkdown content={"```ts\nconst answer = 42;\n```"} />);

    // Sanity check: the code element itself is a tree of highlight spans,
    // not a single text node — this is exactly the case childrenToText
    // exists to flatten correctly for the copy button.
    expect(document.querySelector("code")?.children.length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: "Copy ts code" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("const answer = 42;"));
  });

  it("leaves inline code unhighlighted", () => {
    render(<SafeMarkdown content={"Use `answer` here."} />);
    const code = document.querySelector("code");
    expect(code?.className ?? "").toBe("");
    expect(code?.textContent).toBe("answer");
  });
});
