import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ChatGroup, ChatSummary } from "../../../../contracts/cortex-api";
import { ChatLibrary } from "./ChatLibrary";

const chat = (id: string, title: string, groupId: string | null = null): ChatSummary => ({
  id,
  title,
  timestamp: "2026-01-01T00:00:00Z",
  group_id: groupId,
});

const group = (id: string, name: string, collapsed = false): ChatGroup => ({
  id,
  name,
  position: 0,
  collapsed,
  timestamp: "2026-01-01T00:00:00Z",
});

function renderLibrary(overrides: Partial<React.ComponentProps<typeof ChatLibrary>> = {}) {
  const props: React.ComponentProps<typeof ChatLibrary> = {
    chats: [],
    groups: [],
    activeChatId: null,
    activeRowVisible: true,
    query: "",
    onSelectChat: vi.fn(),
    onRenameChat: vi.fn(),
    onDeleteChat: vi.fn(),
    onCreateGroup: vi.fn<(name: string) => Promise<void>>().mockResolvedValue(),
    onRenameGroup: vi.fn<(id: string, name: string) => Promise<void>>().mockResolvedValue(),
    onDeleteGroup: vi.fn<(id: string) => Promise<void>>().mockResolvedValue(),
    onToggleGroup: vi.fn(),
    onMoveChat: vi.fn(),
    ...overrides,
  };
  return { ...render(<ChatLibrary {...props} />), props };
}

describe("ChatLibrary", () => {
  it("files chats under their group and leaves the rest ungrouped", () => {
    renderLibrary({
      groups: [group("g1", "Research")],
      chats: [chat("c1", "Vector stores", "g1"), chat("c2", "Loose thread")],
    });

    const section = screen.getByRole("button", { name: "Collapse Research" }).closest("section")!;
    expect(within(section).getByRole("button", { name: "Vector stores" })).toBeVisible();
    expect(within(section).queryByRole("button", { name: "Loose thread" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Loose thread" })).toBeVisible();
  });

  it("hides a collapsed group's chats and reports the toggle", async () => {
    const user = userEvent.setup();
    const { props } = renderLibrary({
      groups: [group("g1", "Research", true)],
      chats: [chat("c1", "Vector stores", "g1")],
    });

    expect(screen.queryByRole("button", { name: "Vector stores" })).not.toBeInTheDocument();
    const toggle = screen.getByRole("button", { name: "Expand Research" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    await user.click(toggle);
    expect(props.onToggleGroup).toHaveBeenCalledWith("g1", false);
  });

  it("keeps the count visible so a collapsed group still shows its size", () => {
    renderLibrary({
      groups: [group("g1", "Research", true)],
      chats: [chat("c1", "One", "g1"), chat("c2", "Two", "g1")],
    });

    expect(within(screen.getByRole("button", { name: "Expand Research" })).getByText("2")).toBeVisible();
  });

  it("force-expands a collapsed group while searching so matches stay reachable", () => {
    renderLibrary({
      groups: [group("g1", "Research", true)],
      chats: [chat("c1", "Vector stores", "g1")],
      query: "vector",
    });

    // Collapsing must never hide the very result the user searched for.
    expect(screen.getByRole("button", { name: "Vector stores" })).toBeVisible();
  });

  it("drops groups with no match from the search results", () => {
    renderLibrary({
      groups: [group("g1", "Research"), group("g2", "Empty")],
      chats: [chat("c1", "Vector stores", "g1")],
      query: "vector",
    });

    expect(screen.getByRole("button", { name: "Collapse Research" })).toBeVisible();
    expect(screen.queryByRole("button", { name: /Collapse Empty|Expand Empty/ })).not.toBeInTheDocument();
  });

  it("reports no matches without claiming the library is empty", () => {
    renderLibrary({ chats: [chat("c1", "Vector stores")], query: "zzz" });
    expect(screen.getByText("No chats match your search.")).toBeVisible();
  });

  it("reports no matches when existing groups contain no matching chats", () => {
    renderLibrary({
      groups: [group("g1", "Research")],
      chats: [chat("c1", "Vector stores", "g1")],
      query: "zzz",
    });

    expect(screen.getByText("No chats match your search.")).toBeVisible();
  });

  it("moves a chat into a group and back out again", async () => {
    const user = userEvent.setup();
    const { props } = renderLibrary({
      groups: [group("g1", "Research")],
      chats: [chat("c1", "Loose thread")],
    });

    await user.click(screen.getByRole("button", { name: "Move Loose thread to a group" }));
    await user.click(screen.getByRole("menuitem", { name: "Research" }));
    expect(props.onMoveChat).toHaveBeenCalledWith("c1", "g1");
  });

  it("provides composite-menu keyboard navigation and restores focus on escape", async () => {
    const user = userEvent.setup();
    renderLibrary({
      groups: [group("g1", "Research"), group("g2", "Work")],
      chats: [chat("c1", "Loose thread")],
    });

    const trigger = screen.getByRole("button", { name: "Move Loose thread to a group" });
    await user.click(trigger);
    const research = screen.getByRole("menuitem", { name: "Research" });
    const work = screen.getByRole("menuitem", { name: "Work" });
    expect(research).toHaveFocus();

    await user.keyboard("{ArrowDown}");
    expect(work).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(research).toHaveFocus();
    await user.keyboard("{ArrowUp}");
    expect(work).toHaveFocus();
    await user.keyboard("{Home}");
    expect(research).toHaveFocus();
    await user.keyboard("{End}");
    expect(work).toHaveFocus();

    await user.keyboard("r");
    expect(research).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("opens at the correct edge when using arrow keys on the trigger", async () => {
    const user = userEvent.setup();
    renderLibrary({
      groups: [group("g1", "Research"), group("g2", "Work")],
      chats: [chat("c1", "Loose thread")],
    });

    const trigger = screen.getByRole("button", { name: "Move Loose thread to a group" });
    trigger.focus();
    await user.keyboard("{ArrowUp}");
    expect(screen.getByRole("menuitem", { name: "Work" })).toHaveFocus();
  });

  it("skips the current disabled group and exits the menu with Tab", async () => {
    const user = userEvent.setup();
    renderLibrary({
      groups: [group("g1", "Research"), group("g2", "Work"), group("g3", "Writing")],
      chats: [chat("c1", "Vector stores", "g1")],
    });

    const trigger = screen.getByRole("button", { name: "Move Vector stores to a group" });
    await user.click(trigger);
    const research = screen.getByRole("menuitem", { name: "Research" });
    const work = screen.getByRole("menuitem", { name: "Work" });
    const writing = screen.getByRole("menuitem", { name: "Writing" });
    const remove = screen.getByRole("menuitem", { name: "Remove from group" });
    expect(research).toBeDisabled();
    expect(work).toHaveFocus();

    await user.keyboard("{ArrowDown}");
    expect(writing).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(remove).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(work).toHaveFocus();
    await user.keyboard("{ArrowUp}");
    expect(remove).toHaveFocus();

    await user.keyboard("{Tab}");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Rename Vector stores" })).toHaveFocus();

    await user.click(trigger);
    const reopenedWork = screen.getByRole("menuitem", { name: "Work" });
    expect(reopenedWork).toHaveFocus();
    await user.keyboard("{Shift>}{Tab}{/Shift}");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("offers removal only for a chat that is actually in a group", async () => {
    const user = userEvent.setup();
    const { props } = renderLibrary({
      groups: [group("g1", "Research")],
      chats: [chat("c1", "Vector stores", "g1")],
    });

    await user.click(screen.getByRole("button", { name: "Move Vector stores to a group" }));
    // Its current group is not a move target.
    expect(screen.getByRole("menuitem", { name: "Research" })).toBeDisabled();

    await user.click(screen.getByRole("menuitem", { name: "Remove from group" }));
    expect(props.onMoveChat).toHaveBeenCalledWith("c1", null);
  });

  it("does not offer a move control when no group exists yet", () => {
    renderLibrary({ chats: [chat("c1", "Loose thread")] });
    expect(screen.queryByRole("button", { name: /Move .* to a group/ })).not.toBeInTheDocument();
  });

  it("creates a group from the library header", async () => {
    const user = userEvent.setup();
    const { props } = renderLibrary();

    await user.click(screen.getByRole("button", { name: "New group" }));
    await user.type(screen.getByLabelText("Group name"), "Research");
    await user.click(screen.getByRole("button", { name: "Create group" }));

    expect(props.onCreateGroup).toHaveBeenCalledWith("Research");
  });

  it("keeps the create-group dialog and input when creation fails", async () => {
    const user = userEvent.setup();
    const onCreateGroup = vi.fn<(name: string) => Promise<boolean>>().mockResolvedValue(false);
    renderLibrary({ onCreateGroup });

    await user.click(screen.getByRole("button", { name: "New group" }));
    const field = screen.getByLabelText("Group name");
    await user.type(field, "Research");
    await user.click(screen.getByRole("button", { name: "Create group" }));

    expect(onCreateGroup).toHaveBeenCalledWith("Research");
    expect(screen.getByRole("dialog")).toBeVisible();
    expect(field).toHaveValue("Research");
  });

  it("explains that deleting a group keeps its chats, and confirms without a gauntlet", async () => {
    const user = userEvent.setup();
    const { props } = renderLibrary({
      groups: [group("g1", "Research")],
      chats: [chat("c1", "Vector stores", "g1"), chat("c2", "Embeddings", "g1")],
    });

    await user.click(screen.getByRole("button", { name: "Delete group Research" }));
    // The copy has to state the stakes plainly: this is filing, not data loss.
    expect(screen.getByText(/2 chats in “Research” will move back/)).toBeVisible();
    expect(screen.getByText(/Nothing is deleted/)).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Delete group" }));
    expect(props.onDeleteGroup).toHaveBeenCalledWith("g1");
  });

  it("renames a group from its row", async () => {
    const user = userEvent.setup();
    const { props } = renderLibrary({ groups: [group("g1", "Research")] });

    await user.click(screen.getByRole("button", { name: "Rename group Research" }));
    const field = screen.getByLabelText("Group name");
    await user.clear(field);
    await user.type(field, "Deep Research");
    await user.click(screen.getByRole("button", { name: "Save name" }));

    expect(props.onRenameGroup).toHaveBeenCalledWith("g1", "Deep Research");
  });

  it("keeps the group rename dialog and input when renaming fails", async () => {
    const user = userEvent.setup();
    const onRenameGroup = vi.fn<(id: string, name: string) => Promise<boolean>>().mockResolvedValue(false);
    renderLibrary({ groups: [group("g1", "Research")], onRenameGroup });

    await user.click(screen.getByRole("button", { name: "Rename group Research" }));
    const field = screen.getByLabelText("Group name");
    await user.clear(field);
    await user.type(field, "Deep Research");
    await user.click(screen.getByRole("button", { name: "Save name" }));

    expect(onRenameGroup).toHaveBeenCalledWith("g1", "Deep Research");
    expect(screen.getByRole("dialog")).toBeVisible();
    expect(field).toHaveValue("Deep Research");
  });

  it("keeps the group delete dialog open when deletion fails", async () => {
    const user = userEvent.setup();
    const onDeleteGroup = vi.fn<(id: string) => Promise<boolean>>().mockResolvedValue(false);
    renderLibrary({ groups: [group("g1", "Research")], onDeleteGroup });

    await user.click(screen.getByRole("button", { name: "Delete group Research" }));
    await user.click(screen.getByRole("button", { name: "Delete group" }));

    expect(onDeleteGroup).toHaveBeenCalledWith("g1");
    expect(screen.getByRole("dialog")).toBeVisible();
  });

  it("marks only the active chat, and not while another route is showing", () => {
    const { unmount } = renderLibrary({
      chats: [chat("c1", "Vector stores")],
      activeChatId: "c1",
      activeRowVisible: true,
    });
    expect(screen.getByRole("button", { name: "Vector stores" })).toHaveAttribute("aria-current", "page");
    unmount();

    renderLibrary({ chats: [chat("c1", "Vector stores")], activeChatId: "c1", activeRowVisible: false });
    expect(screen.getByRole("button", { name: "Vector stores" })).not.toHaveAttribute("aria-current");
  });

  it("shows an empty group as empty rather than omitting it", () => {
    renderLibrary({ groups: [group("g1", "Research")] });
    expect(screen.getByRole("button", { name: "Collapse Research" })).toBeVisible();
    expect(screen.getByText("Empty — move a chat here.")).toBeVisible();
  });
});
