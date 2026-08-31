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
});
