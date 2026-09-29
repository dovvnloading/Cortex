import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { MemoryPanel } from "./MemoryPanel";

describe("MemoryPanel", () => {
  it("keeps focus and caret continuity while editing a memory", async () => {
    const user = userEvent.setup();
    render(
      <MemoryPanel
        memos={["Original fact"]}
        busy={false}
        onAdd={vi.fn<(memo: string) => Promise<void>>().mockResolvedValue()}
        onReplace={vi.fn<(memos: string[]) => Promise<void>>().mockResolvedValue()}
        onClear={vi.fn<() => Promise<void>>().mockResolvedValue()}
      />,
    );

    const input = screen.getByRole("textbox", { name: "Memory 1" });
    await user.click(input);
    await user.keyboard(" updated");

    expect(input).toHaveValue("Original fact updated");
    expect(input).toHaveFocus();
  });

  it("keeps unsaved edits to other rows when a memory is added", async () => {
    // Adding a memory changes the server list, which re-seeded the whole
    // draft. Every other row the user had edited but not yet saved silently
    // reverted to the server's copy.
    const user = userEvent.setup();
    const onReplace = vi.fn<(memos: string[]) => Promise<void>>().mockResolvedValue();

    function Host() {
      const [memos, setMemos] = useState(["First", "Second"]);
      return (
        <MemoryPanel
          memos={memos}
          busy={false}
          onAdd={async (memo: string) => { setMemos((current) => [...current, memo]); }}
          onReplace={onReplace}
          onClear={vi.fn<() => Promise<void>>().mockResolvedValue()}
        />
      );
    }
    render(<Host />);

    await user.clear(screen.getByRole("textbox", { name: "Memory 2" }));
    await user.type(screen.getByRole("textbox", { name: "Memory 2" }), "Edited");

    await user.type(screen.getByRole("textbox", { name: "New memory" }), "Third");
    await user.click(screen.getByRole("button", { name: "Add memory" }));
    await screen.findByRole("textbox", { name: "Memory 3" });

    expect(screen.getByRole("textbox", { name: "Memory 2" })).toHaveValue("Edited");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(onReplace).toHaveBeenCalledWith(["First", "Edited", "Third"]);
  });

  it("preserves edited rows when removing a neighboring row", async () => {
    const user = userEvent.setup();
    const onReplace = vi.fn<(memos: string[]) => Promise<void>>().mockResolvedValue();
    render(
      <MemoryPanel
        memos={["First", "Second"]}
        busy={false}
        onAdd={vi.fn<(memo: string) => Promise<void>>().mockResolvedValue()}
        onReplace={onReplace}
        onClear={vi.fn<() => Promise<void>>().mockResolvedValue()}
      />,
    );

    await user.clear(screen.getByRole("textbox", { name: "Memory 2" }));
    await user.type(screen.getByRole("textbox", { name: "Memory 2" }), "Edited");
    await user.click(screen.getByRole("button", { name: "Remove memory 1" }));
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(onReplace).toHaveBeenCalledWith(["Edited"]);
  });

  it("only appends a memory after the add succeeds", async () => {
    const user = userEvent.setup();
    const onAdd = vi.fn<(memo: string) => Promise<void>>().mockRejectedValue(new Error("save failed"));
    render(
      <MemoryPanel
        memos={[]}
        busy={false}
        onAdd={onAdd}
        onReplace={vi.fn<(memos: string[]) => Promise<void>>().mockResolvedValue()}
        onClear={vi.fn<() => Promise<void>>().mockResolvedValue()}
      />,
    );

    await user.type(screen.getByRole("textbox", { name: "New memory" }), "Failed memory");
    await user.click(screen.getByRole("button", { name: "Add memory" }));

    expect(await screen.findByRole("textbox", { name: "New memory" })).toHaveValue("Failed memory");
    expect(screen.queryByRole("textbox", { name: "Memory 1" })).not.toBeInTheDocument();
  });

  it("only clears the draft after clear succeeds", async () => {
    const user = userEvent.setup();
    const onClear = vi.fn<() => Promise<void>>().mockRejectedValue(new Error("clear failed"));
    render(
      <MemoryPanel
        memos={["Keep this"]}
        busy={false}
        onAdd={vi.fn<(memo: string) => Promise<void>>().mockResolvedValue()}
        onReplace={vi.fn<(memos: string[]) => Promise<void>>().mockResolvedValue()}
        onClear={onClear}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Clear all" }));
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Clear all memories" }));

    expect(onClear).toHaveBeenCalledOnce();
    expect(screen.getByRole("textbox", { name: "Memory 1" })).toHaveValue("Keep this");
    // The failure is the workspace's to report; the dialog does not linger.
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
  });

  it("clears the draft after clear succeeds", async () => {
    const user = userEvent.setup();
    render(
      <MemoryPanel
        memos={["Remove this"]}
        busy={false}
        onAdd={vi.fn<(memo: string) => Promise<void>>().mockResolvedValue()}
        onReplace={vi.fn<(memos: string[]) => Promise<void>>().mockResolvedValue()}
        onClear={vi.fn<() => Promise<void>>().mockResolvedValue()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Clear all" }));
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Clear all memories" }));

    expect(screen.queryByRole("textbox", { name: "Memory 1" })).not.toBeInTheDocument();
    expect(screen.getByText("No permanent memories stored.")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
  });

  it("asks in the app's own dialog, never window.confirm, and clears nothing on Keep", async () => {
    // window.confirm blocks the JavaScript thread until the native dialog is
    // dismissed; ChatPage avoids it for the same reason.
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const onClear = vi.fn<() => Promise<void>>().mockResolvedValue();
    render(
      <MemoryPanel
        memos={["Remember this"]}
        busy={false}
        onAdd={vi.fn<(memo: string) => Promise<void>>().mockResolvedValue()}
        onReplace={vi.fn<(memos: string[]) => Promise<void>>().mockResolvedValue()}
        onClear={onClear}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Clear all" }));

    const dialog = screen.getByRole("alertdialog");
    expect(dialog).toHaveTextContent("Clear all memories?");
    expect(dialog).toHaveTextContent("It cannot be undone.");
    await user.click(within(dialog).getByRole("button", { name: "Keep memories" }));

    expect(confirm).not.toHaveBeenCalled();
    expect(onClear).not.toHaveBeenCalled();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Memory 1" })).toHaveValue("Remember this");
    confirm.mockRestore();
  });

  it("keeps edited rows available when saving changes fails", async () => {
    const user = userEvent.setup();
    const onReplace = vi.fn<(memos: string[]) => Promise<void>>().mockRejectedValue(new Error("save failed"));
    render(
      <MemoryPanel
        memos={["Original"]}
        busy={false}
        onAdd={vi.fn<(memo: string) => Promise<void>>().mockResolvedValue()}
        onReplace={onReplace}
        onClear={vi.fn<() => Promise<void>>().mockResolvedValue()}
      />,
    );

    const input = screen.getByRole("textbox", { name: "Memory 1" });
    await user.clear(input);
    await user.type(input, "Edited");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(onReplace).toHaveBeenCalledWith(["Edited"]);
    expect(screen.getByRole("textbox", { name: "Memory 1" })).toHaveValue("Edited");
  });
});

describe("MemoryPanel server reconciliation", () => {
  it("drops rows the server normalized away", async () => {
    // The draft was seeded from `memos` once and never re-derived, so an
    // entry the server rejected -- trimmed to nothing, or a case-insensitive
    // duplicate -- stayed on screen looking saved.
    const { useState } = await import("react");
    const { waitFor } = await import("@testing-library/react");

    function Harness() {
      const [memos, setMemos] = useState<string[]>(["kept", "duplicate"]);
      return (
        <>
          <button onClick={() => setMemos(["kept"])}>server responded</button>
          <MemoryPanel
            memos={memos}
            busy={false}
            onAdd={vi.fn().mockResolvedValue(undefined)}
            onReplace={vi.fn().mockResolvedValue(undefined)}
            onClear={vi.fn().mockResolvedValue(undefined)}
          />
        </>
      );
    }
    render(<Harness />);

    expect(screen.getByDisplayValue("duplicate")).toBeInTheDocument();

    screen.getByRole("button", { name: "server responded" }).click();

    await waitFor(() => {
      expect(screen.queryByDisplayValue("duplicate")).not.toBeInTheDocument();
    });
    expect(screen.getByDisplayValue("kept")).toBeInTheDocument();
  });

  it("drops a normalized-away row without discarding an edit to a surviving one", async () => {
    // Both halves at once: reconciliation must not be satisfied by simply
    // preferring the draft (which would keep the rejected row on screen) or
    // by simply preferring the server (the original defect).
    const user = userEvent.setup();
    const { waitFor } = await import("@testing-library/react");

    function Harness() {
      const [memos, setMemos] = useState<string[]>(["kept", "duplicate"]);
      return (
        <>
          <button onClick={() => setMemos(["kept"])}>server responded</button>
          <MemoryPanel
            memos={memos}
            busy={false}
            onAdd={vi.fn<(memo: string) => Promise<void>>().mockResolvedValue()}
            onReplace={vi.fn<(memos: string[]) => Promise<void>>().mockResolvedValue()}
            onClear={vi.fn<() => Promise<void>>().mockResolvedValue()}
          />
        </>
      );
    }
    render(<Harness />);

    await user.clear(screen.getByRole("textbox", { name: "Memory 1" }));
    await user.type(screen.getByRole("textbox", { name: "Memory 1" }), "kept and edited");
    await user.click(screen.getByRole("button", { name: "server responded" }));

    await waitFor(() => {
      expect(screen.queryByDisplayValue("duplicate")).not.toBeInTheDocument();
    });
    expect(screen.getByRole("textbox", { name: "Memory 1" })).toHaveValue("kept and edited");
  });

  describe("when the list has not loaded", () => {
    const noop = {
      busy: false,
      onAdd: vi.fn<(memo: string) => Promise<void>>().mockResolvedValue(),
      onReplace: vi.fn<(memos: string[]) => Promise<void>>().mockResolvedValue(),
      onClear: vi.fn<() => Promise<void>>().mockResolvedValue(),
    };

    it("says it is loading instead of claiming there are no memories", () => {
      render(<MemoryPanel memos={[]} load={{ status: "loading" }} {...noop} />);

      expect(screen.getByRole("status")).toHaveTextContent("Loading saved memories...");
      expect(screen.queryByText("No permanent memories stored.")).not.toBeInTheDocument();
      expect(screen.queryByRole("textbox", { name: "New memory" })).not.toBeInTheDocument();
    });

    it("reports the failure with a retry and offers no editor over an unknown list", async () => {
      const user = userEvent.setup();
      const onRetry = vi.fn();
      render(<MemoryPanel memos={[]} load={{ status: "error", message: "Cortex could not load memories." }} onRetry={onRetry} {...noop} />);

      expect(screen.getByRole("alert")).toHaveTextContent("Cortex could not load memories.");
      expect(screen.queryByText("No permanent memories stored.")).not.toBeInTheDocument();
      expect(screen.queryByRole("textbox", { name: "New memory" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /Clear all|Save changes/ })).not.toBeInTheDocument();

      await user.click(screen.getByRole("button", { name: "Retry" }));
      expect(onRetry).toHaveBeenCalledOnce();
    });

    it("shows the list once it has loaded, seeded from what arrived", () => {
      const { rerender } = render(<MemoryPanel memos={[]} load={{ status: "loading" }} {...noop} />);

      rerender(<MemoryPanel memos={["Likes tea"]} load={{ status: "ready" }} {...noop} />);

      expect(screen.getByRole("textbox", { name: "Memory 1" })).toHaveValue("Likes tea");
    });
  });
});

describe("MemoryPanel unsaved changes", () => {
  const renderPanel = (memos: string[], onReplace = vi.fn<(memos: string[]) => Promise<void>>().mockResolvedValue()) => {
    render(
      <MemoryPanel
        memos={memos}
        busy={false}
        onAdd={vi.fn<(memo: string) => Promise<void>>().mockResolvedValue()}
        onReplace={onReplace}
        onClear={vi.fn<() => Promise<void>>().mockResolvedValue()}
      />,
    );
    return onReplace;
  };

  it("keeps Save disabled and quiet until something changes", async () => {
    const user = userEvent.setup();
    renderPanel(["First", "Second"]);

    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    expect(screen.getByRole("status")).toBeEmptyDOMElement();

    await user.type(screen.getByRole("textbox", { name: "Memory 1" }), "!");

    expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled();
    expect(screen.getByRole("status")).toHaveTextContent("Unsaved changes");
  });

  it("does not count whitespace the server would trim as a change", async () => {
    // The server stores memories trimmed, so "First " saves as "First" and the
    // list would otherwise keep reporting unsaved changes after a save.
    const user = userEvent.setup();
    renderPanel(["First"]);

    await user.type(screen.getByRole("textbox", { name: "Memory 1" }), "  ");

    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });

  it("is clean again when an edit is typed back to what was saved", async () => {
    const user = userEvent.setup();
    renderPanel(["First"]);

    const input = screen.getByRole("textbox", { name: "Memory 1" });
    await user.type(input, "x");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled();
    await user.type(input, "{Backspace}");

    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });

  it("strikes a removed row through, offers Undo, and keeps it out of the save", async () => {
    const user = userEvent.setup();
    const onReplace = renderPanel(["First", "Second"]);

    await user.click(screen.getByRole("button", { name: "Remove memory 1" }));

    const removed = screen.getByRole("textbox", { name: "Memory 1 (removed)" });
    expect(removed).toHaveValue("First");
    expect(removed).toHaveAttribute("readonly");
    expect(removed.closest("li")).toHaveClass("memory-list-item-removed");
    expect(screen.getByRole("status")).toHaveTextContent("Unsaved changes");
    // Nothing has been sent: removal is a draft until Save.
    expect(onReplace).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(onReplace).toHaveBeenCalledExactlyOnceWith(["Second"]);
  });

  it("puts a removed row back with Undo and is clean again", async () => {
    const user = userEvent.setup();
    const onReplace = renderPanel(["First", "Second"]);

    await user.click(screen.getByRole("button", { name: "Remove memory 2" }));
    await user.click(screen.getByRole("button", { name: "Undo removing memory 2" }));

    const restored = screen.getByRole("textbox", { name: "Memory 2" });
    expect(restored).toHaveValue("Second");
    expect(restored).not.toHaveAttribute("readonly");
    expect(restored.closest("li")).not.toHaveClass("memory-list-item-removed");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    expect(onReplace).not.toHaveBeenCalled();
  });

  it("keeps a row's edit when its removal is undone", async () => {
    const user = userEvent.setup();
    const onReplace = renderPanel(["First"]);

    await user.type(screen.getByRole("textbox", { name: "Memory 1" }), " edited");
    await user.click(screen.getByRole("button", { name: "Remove memory 1" }));
    await user.click(screen.getByRole("button", { name: "Undo removing memory 1" }));
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(onReplace).toHaveBeenCalledExactlyOnceWith(["First edited"]);
  });

  it("leaves the marked rows in place when the save fails, and settles them once the server answers", async () => {
    const user = userEvent.setup();
    const onReplace = vi.fn<(memos: string[]) => Promise<void>>()
      .mockRejectedValueOnce(new Error("save failed"))
      .mockResolvedValueOnce();

    function Host() {
      const [memos, setMemos] = useState(["First", "Second"]);
      return (
        <MemoryPanel
          memos={memos}
          busy={false}
          onAdd={vi.fn<(memo: string) => Promise<void>>().mockResolvedValue()}
          onReplace={async (next) => { await onReplace(next); setMemos(next); }}
          onClear={vi.fn<() => Promise<void>>().mockResolvedValue()}
        />
      );
    }
    render(<Host />);

    await user.click(screen.getByRole("button", { name: "Remove memory 1" }));
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    // The failed save changed nothing: still struck through, still unsaved.
    expect(await screen.findByRole("textbox", { name: "Memory 1 (removed)" })).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("Unsaved changes");

    await user.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(screen.queryByRole("textbox", { name: "Memory 1 (removed)" })).not.toBeInTheDocument());
    expect(screen.getByRole("textbox", { name: "Memory 1" })).toHaveValue("Second");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
    expect(onReplace).toHaveBeenNthCalledWith(2, ["Second"]);
  });

  it("does not lose a pending removal when a memory is added meanwhile", async () => {
    const user = userEvent.setup();
    const onReplace = vi.fn<(memos: string[]) => Promise<void>>().mockResolvedValue();

    function Host() {
      const [memos, setMemos] = useState(["First", "Second"]);
      return (
        <MemoryPanel
          memos={memos}
          busy={false}
          onAdd={async (memo: string) => { setMemos((current) => [...current, memo]); }}
          onReplace={onReplace}
          onClear={vi.fn<() => Promise<void>>().mockResolvedValue()}
        />
      );
    }
    render(<Host />);

    await user.click(screen.getByRole("button", { name: "Remove memory 1" }));
    await user.type(screen.getByRole("textbox", { name: "New memory" }), "Third");
    await user.click(screen.getByRole("button", { name: "Add memory" }));
    await screen.findByRole("textbox", { name: "Memory 3" });

    expect(screen.getByRole("textbox", { name: "Memory 1 (removed)" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(onReplace).toHaveBeenCalledExactlyOnceWith(["Second", "Third"]);
  });
});
