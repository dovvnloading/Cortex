import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { useUiStore } from "../../stores/useUiStore";
import { ShortcutsHelpDialog } from "./ShortcutsHelpDialog";

describe("ShortcutsHelpDialog", () => {
  it("is closed by default and opens on '?'", async () => {
    render(<ShortcutsHelpDialog />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    const user = userEvent.setup();
    await user.keyboard("?");

    expect(await screen.findByRole("dialog", { name: "Keyboard shortcuts" })).toBeVisible();
    expect(screen.getByText("Open the command palette")).toBeInTheDocument();
  });

  it("closes via the Close button", async () => {
    render(<ShortcutsHelpDialog />);
    useUiStore.getState().setShortcutsDialogOpen(true);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "Close" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("closes on Escape", async () => {
    render(<ShortcutsHelpDialog />);
    useUiStore.getState().setShortcutsDialogOpen(true);
    await screen.findByRole("dialog");
    const user = userEvent.setup();

    await user.keyboard("{Escape}");

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
