import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
    vi.spyOn(window, "confirm").mockReturnValue(true);
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

    expect(onClear).toHaveBeenCalledOnce();
    expect(screen.getByRole("textbox", { name: "Memory 1" })).toHaveValue("Keep this");
  });

  it("clears the draft after clear succeeds", async () => {
    const user = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(true);
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

    expect(screen.queryByRole("textbox", { name: "Memory 1" })).not.toBeInTheDocument();
    expect(screen.getByText("No permanent memories stored.")).toBeInTheDocument();
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
});
