import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useChatStore } from "../../stores/useChatStore";
import { MemoryProposals } from "./MemoryProposals";

/** Renders the component the way MessageCard does: its rows come from the store. */
function Harness({ onSave }: { onSave: (memo: string) => Promise<boolean> }) {
  const memos = useChatStore((state) => state.proposedMemoriesByMessage["assistant-1"]);
  if (!memos?.length) return <p>No suggestions left.</p>;
  return <MemoryProposals messageId="assistant-1" memos={memos} onSave={onSave} />;
}

function seed(memos: string[]) {
  useChatStore.setState({ proposedMemoriesByMessage: { "assistant-1": memos } });
}

describe("MemoryProposals", () => {
  afterEach(() => {
    useChatStore.setState({ proposedMemoriesByMessage: {} });
  });

  it("names each suggestion and offers Save and Dismiss for it", () => {
    seed(["User likes tea.", "User lives in Oslo."]);
    render(<Harness onSave={vi.fn()} />);

    expect(screen.getByRole("region", { name: "Cortex suggests remembering" })).toBeInTheDocument();
    const rows = screen.getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    const first = within(rows[0]);
    expect(first.getByText("User likes tea.")).toBeVisible();
    // The buttons are named by their action and described by the fact, so a
    // screen reader hears which suggestion each pair belongs to.
    expect(first.getByRole("button", { name: "Save" })).toHaveAccessibleDescription("User likes tea.");
    expect(first.getByRole("button", { name: "Dismiss" })).toHaveAccessibleDescription("User likes tea.");
  });

  it("saves only the suggestion whose Save was pressed, then removes it", async () => {
    const user = userEvent.setup();
    seed(["User likes tea.", "User lives in Oslo."]);
    const onSave = vi.fn().mockResolvedValue(true);
    render(<Harness onSave={onSave} />);

    await user.click(within(screen.getAllByRole("listitem")[1]).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(screen.queryByText("User lives in Oslo.")).not.toBeInTheDocument());
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave).toHaveBeenCalledWith("User lives in Oslo.");
    expect(screen.getByText("User likes tea.")).toBeVisible();
    expect(useChatStore.getState().proposedMemoriesByMessage).toEqual({ "assistant-1": ["User likes tea."] });
  });

  it("keeps a suggestion that could not be saved so the user can try again", async () => {
    const user = userEvent.setup();
    seed(["User likes tea."]);
    const onSave = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    render(<Harness onSave={onSave} />);

    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByRole("button", { name: "Save" })).toBeEnabled());
    expect(screen.getByText("User likes tea.")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.getByText("No suggestions left.")).toBeVisible());
    expect(onSave).toHaveBeenCalledTimes(2);
  });

  it("dismisses without saving anything", async () => {
    const user = userEvent.setup();
    seed(["User likes tea."]);
    const onSave = vi.fn().mockResolvedValue(true);
    render(<Harness onSave={onSave} />);

    await user.click(screen.getByRole("button", { name: "Dismiss" }));

    expect(screen.getByText("No suggestions left.")).toBeVisible();
    expect(onSave).not.toHaveBeenCalled();
  });

  it("does not save the same fact twice when Save is pressed again while it is being stored", async () => {
    seed(["User likes tea."]);
    let finish: (saved: boolean) => void = () => undefined;
    const onSave = vi.fn(() => new Promise<boolean>((resolve) => { finish = resolve; }));
    render(<Harness onSave={onSave} />);
    const save = screen.getByRole("button", { name: "Save" });

    // Two presses inside the same task, before React has re-rendered the button disabled.
    act(() => {
      save.click();
      save.click();
    });

    expect(onSave).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Dismiss" })).toBeDisabled();
    await act(async () => finish(true));
    await waitFor(() => expect(screen.getByText("No suggestions left.")).toBeVisible());
  });

  it("moves keyboard focus to the next suggestion when one is removed", async () => {
    const user = userEvent.setup();
    seed(["First fact.", "Second fact.", "Third fact."]);
    render(<Harness onSave={vi.fn().mockResolvedValue(true)} />);

    await user.click(within(screen.getAllByRole("listitem")[0]).getByRole("button", { name: "Dismiss" }));
    expect(within(screen.getAllByRole("listitem")[0]).getByRole("button", { name: "Save" })).toHaveFocus();
    expect(screen.queryByText("First fact.")).not.toBeInTheDocument();

    // Removing the last row lands on the one before it.
    await user.click(within(screen.getAllByRole("listitem")[1]).getByRole("button", { name: "Dismiss" }));
    expect(within(screen.getAllByRole("listitem")[0]).getByRole("button", { name: "Save" })).toHaveFocus();
    expect(screen.getByText("Second fact.")).toBeVisible();
  });

  it("renders a suggestion as plain text, never as markup", () => {
    seed(["<img src=x onerror=alert(1)> and **bold**"]);
    const { container } = render(<Harness onSave={vi.fn()} />);

    expect(screen.getByText("<img src=x onerror=alert(1)> and **bold**")).toBeVisible();
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("strong")).toBeNull();
  });
});
