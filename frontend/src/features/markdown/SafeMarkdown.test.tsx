import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { declarationsOf, parseRules, selectorsOf, tokensCss } from "../../test/css";
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

  it("does not leak react-markdown's `node` prop onto the rendered link or table elements", () => {
    // Regression guard: Link and Table used to be typed as plain
    // ComponentProps<"a">/ComponentProps<"table">, so react-markdown's extra
    // `node` prop (the hast AST element every custom renderer receives) fell
    // into their `...props` rest spread and landed on the real DOM element
    // as a stray `node="[object Object]"` attribute. Note this does *not*
    // surface as a console warning here: React only warns about unknown DOM
    // props that look like a mis-cased custom attribute (e.g. `someProp`),
    // and silently passes through already-lowercase unknown props like
    // `node` -- so the attribute itself, not console.error, is the
    // observable symptom to assert on.
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});

    render(
      <SafeMarkdown
        content={"[good](https://example.com)\n\n| A | B |\n| --- | --- |\n| 1 | 2 |"}
      />,
    );

    const link = screen.getByRole("link", { name: "good" });
    const table = document.querySelector("table");
    expect(link).not.toHaveAttribute("node");
    expect(table).not.toBeNull();
    expect(table).not.toHaveAttribute("node");
    expect(consoleError).not.toHaveBeenCalled();
  });

  describe("code wrapping", () => {
    const WRAP_KEY = "cortex.codeWrap";
    const TWO_BLOCKS = "```ts\nconst a = 1;\n```\n\n```py\nb = 2\n```";

    afterEach(() => {
      window.localStorage.clear();
    });

    it("offers a Wrap toggle beside Copy that folds long lines and remembers the choice", () => {
      render(<SafeMarkdown content={"```ts\nconst answer = 42;\n```"} />);

      const toggle = screen.getByRole("button", { name: "Wrap ts code lines" });
      const block = document.querySelector(".code-block");
      // Off by default: code keeps scrolling sideways, as before.
      expect(toggle).toHaveAttribute("aria-pressed", "false");
      expect(block).not.toHaveClass("code-block-wrap");
      // Both controls live in the toolbar, outside the <pre>.
      expect(toggle.closest(".code-block-toolbar")).not.toBeNull();
      expect(screen.getByRole("button", { name: "Copy ts code" }).closest(".code-block-toolbar")).not.toBeNull();

      fireEvent.click(toggle);

      expect(toggle).toHaveAttribute("aria-pressed", "true");
      expect(block).toHaveClass("code-block-wrap");
      expect(window.localStorage.getItem(WRAP_KEY)).toBe("1");

      fireEvent.click(toggle);
      expect(block).not.toHaveClass("code-block-wrap");
      expect(window.localStorage.getItem(WRAP_KEY)).toBe("0");
    });

    it("applies one choice to every block on the page", () => {
      render(<SafeMarkdown content={TWO_BLOCKS} />);

      fireEvent.click(screen.getByRole("button", { name: "Wrap ts code lines" }));

      const blocks = document.querySelectorAll(".code-block");
      expect(blocks).toHaveLength(2);
      for (const block of blocks) expect(block).toHaveClass("code-block-wrap");
      expect(screen.getByRole("button", { name: "Wrap py code lines" })).toHaveAttribute("aria-pressed", "true");
    });

    it("starts wrapped when the saved choice says so", () => {
      window.localStorage.setItem(WRAP_KEY, "1");
      render(<SafeMarkdown content={"```ts\nconst answer = 42;\n```"} />);

      expect(document.querySelector(".code-block")).toHaveClass("code-block-wrap");
      expect(screen.getByRole("button", { name: "Wrap ts code lines" })).toHaveAttribute("aria-pressed", "true");
    });

    it("still toggles for the session when storage refuses the write", () => {
      const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
        throw new DOMException("blocked", "SecurityError");
      });
      render(<SafeMarkdown content={"```ts\nconst answer = 42;\n```"} />);
      const toggle = screen.getByRole("button", { name: "Wrap ts code lines" });

      fireEvent.click(toggle);
      expect(toggle).toHaveAttribute("aria-pressed", "true");
      expect(document.querySelector(".code-block")).toHaveClass("code-block-wrap");

      // With storage working again the next click is stored, and the
      // session-only override is gone.
      setItem.mockRestore();
      fireEvent.click(toggle);
      expect(toggle).toHaveAttribute("aria-pressed", "false");
      expect(window.localStorage.getItem(WRAP_KEY)).toBe("0");
    });

    it("keeps working when storage cannot even be read", () => {
      vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
        throw new DOMException("blocked", "SecurityError");
      });
      render(<SafeMarkdown content={"```ts\nconst answer = 42;\n```"} />);

      expect(screen.getByRole("button", { name: "Wrap ts code lines" })).toHaveAttribute("aria-pressed", "false");
      vi.restoreAllMocks();
    });

    it("does not change what Copy puts on the clipboard", async () => {
      if (!navigator.clipboard) Object.defineProperty(navigator, "clipboard", { value: {}, configurable: true });
      const writeText = vi.fn().mockResolvedValue(undefined);
      Object.defineProperty(navigator.clipboard, "writeText", { value: writeText, configurable: true, writable: true });
      window.localStorage.setItem(WRAP_KEY, "1");
      render(<SafeMarkdown content={"```ts\nconst answer = 42;\n```"} />);

      fireEvent.click(screen.getByRole("button", { name: "Copy ts code" }));
      await waitFor(() => expect(writeText).toHaveBeenCalledWith("const answer = 42;"));
    });
  });

  describe("task lists", () => {
    it("renders each task as a disabled checkbox inside a list the stylesheet can unmark", () => {
      render(<SafeMarkdown content={"- [x] done\n- [ ] todo\n- plain"} />);

      const list = document.querySelector("ul");
      // The classes the stylesheet keys on to drop the bullet, so a task shows
      // its checkbox alone instead of a bullet and a checkbox.
      expect(list).toHaveClass("contains-task-list");
      const items = document.querySelectorAll("li.task-list-item");
      expect(items).toHaveLength(2);
      const boxes = screen.getAllByRole("checkbox");
      expect(boxes).toHaveLength(2);
      for (const box of boxes) expect(box).toBeDisabled();
      expect(boxes[0]).toBeChecked();
      expect(boxes[1]).not.toBeChecked();
    });

    it("strips list markers and styles the checkbox in the stylesheet", () => {
      const rules = parseRules(tokensCss);
      const declarationsFor = (selector: string) => {
        const rule = rules.find((candidate) => selectorsOf(candidate).includes(selector));
        if (!rule) throw new Error(`tokens.css has no rule for ${selector}`);
        return declarationsOf(rule);
      };

      expect(declarationsFor(".markdown-body .contains-task-list").get("list-style")).toBe("none");
      expect(declarationsFor('.markdown-body .task-list-item input[type="checkbox"]').get("accent-color")).toBe("var(--accent)");
      // Code that wraps must actually fold, in the same place the base rule pins it to `pre`.
      expect(declarationsFor(".markdown-body .code-block-wrap pre > code").get("white-space")).toBe("pre-wrap");
    });
  });

  describe("images", () => {
    it("names a stripped image in place of a picture, with its source on hover", () => {
      render(<SafeMarkdown content={"Look: ![Quarterly chart](https://example.com/chart.png) end."} />);

      expect(document.querySelector("img")).toBeNull();
      const note = screen.getByText("Image: Quarterly chart");
      expect(note).toHaveClass("markdown-image-placeholder");
      expect(note).toHaveAttribute("title", "Image not loaded: https://example.com/chart.png");
      // Still inline with the sentence it sat in.
      expect(note.parentElement).toHaveTextContent("Look: Image: Quarterly chart end.");
    });

    it("says only that there was an image when it had no alt text", () => {
      render(<SafeMarkdown content={"![](https://example.com/chart.png)"} />);

      const note = document.querySelector(".markdown-image-placeholder");
      expect(note).toHaveTextContent(/^Image$/);
      expect(note).toHaveAttribute("title", "Image not loaded: https://example.com/chart.png");
    });

    it("never loads the image or leaks react-markdown's node prop", () => {
      render(<SafeMarkdown content={"![x](https://example.com/a.png)"} />);

      const note = document.querySelector(".markdown-image-placeholder");
      expect(note).not.toHaveAttribute("node");
      expect(note).not.toHaveAttribute("src");
      expect(document.querySelector("img")).toBeNull();
    });

    it("keeps a source the sanitizer removed out of the note", () => {
      // javascript: and data: sources do not survive sanitizing, so there is
      // no address to show -- and none must be invented.
      render(<SafeMarkdown content={"![sneaky](javascript:alert(1))"} />);

      const note = document.querySelector(".markdown-image-placeholder");
      expect(note).toHaveTextContent("Image: sneaky");
      expect(note).toHaveAttribute("title", "Image not loaded");
    });
  });
});
