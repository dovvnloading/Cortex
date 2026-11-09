import { fireEvent, render, screen } from "@testing-library/react";
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

  it("does not stack shortcut help over an existing confirmation modal", async () => {
    const { unmount } = render(
      <>
        <ShortcutsHelpDialog />
        <div role="alertdialog" aria-label="Delete confirmation">
          <button type="button">Keep chat</button>
        </div>
      </>,
    );
    const confirmation = screen.getByRole("alertdialog", { name: "Delete confirmation" });
    const keep = screen.getByRole("button", { name: "Keep chat" });
    keep.focus();
    const guarded = new KeyboardEvent("keydown", { key: "?", bubbles: true, cancelable: true });
    fireEvent(keep, guarded);
    expect(guarded.defaultPrevented).toBe(false);
    expect(confirmation).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Keyboard shortcuts" })).not.toBeInTheDocument();
    expect(useUiStore.getState().shortcutsDialogOpen).toBe(false);
    unmount();

    render(<ShortcutsHelpDialog />);
    const outside = new KeyboardEvent("keydown", { key: "?", bubbles: true, cancelable: true });
    fireEvent(window, outside);
    expect(await screen.findByRole("dialog", { name: "Keyboard shortcuts" })).toBeVisible();
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
