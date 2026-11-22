import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { ChangesReview, type ChangesReviewProps, type FileChange } from "./ChangesReview";

const CHANGES: FileChange[] = [
  {
    path: "src/added.ts",
    kind: "add",
    diff: "--- /dev/null\n+++ b/src/added.ts\n@@ -0,0 +1 @@\n+export const added = true;\n",
  },
  {
    path: "src/modified.ts",
    kind: "modify",
    diff: "--- a/src/modified.ts\n+++ b/src/modified.ts\n@@ -1 +1 @@\n-export const value = 1;\n+export const value = 2;\n",
  },
  {
    path: "src/removed.ts",
    kind: "delete",
    diff: "--- a/src/removed.ts\n+++ /dev/null\n@@ -1 +0,0 @@\n-export const removed = true;\n",
  },
];

function renderReview(overrides: Partial<ChangesReviewProps> = {}) {
  const onAccept = vi.fn();
  const onDiscard = vi.fn();
  const onUndo = vi.fn();
  const props: ChangesReviewProps = {
    summary: "Added a helper, updated a value, and removed an old file.",
    changes: CHANGES,
    onAccept,
    onDiscard,
    onUndo,
    accepted: false,
    ...overrides,
  };
  return { ...render(<ChangesReview {...props} />), props, onAccept, onDiscard, onUndo };
}

function fileRow(path: string): HTMLLIElement {
  return screen.getByRole("checkbox", { name: "Select " + path }).closest("li") as HTMLLIElement;
}

describe("ChangesReview", () => {
  it("shows the plain-language summary before every changed file, including deletions", () => {
    const { onAccept, onDiscard, onUndo } = renderReview();
    const dialog = screen.getByRole("alertdialog");
    const summary = screen.getByText("Added a helper, updated a value, and removed an old file.");
    expect(summary).toBeVisible();
    expect(dialog.textContent?.indexOf(summary.textContent ?? "")).toBeLessThan(
      dialog.textContent?.indexOf("src/added.ts") ?? -1,
    );
    expect(within(dialog).getByRole("list", { name: "Changed files" })).toBeInTheDocument();

    for (const change of CHANGES) {
      const row = fileRow(change.path);
      expect(within(row).getByText(change.path)).toBeVisible();
      expect(within(row).getByText(change.kind)).toHaveClass("status-pill");
      expect(within(row).getByRole("checkbox")).toBeChecked();
      expect(within(row).getByRole("button", { name: "Show changes" })).toHaveAttribute("aria-expanded", "false");
      expect(row.querySelector("pre code")).toBeNull();
    }
    expect(within(fileRow("src/added.ts")).getByText("add")).toHaveClass("status-success");
    expect(within(fileRow("src/removed.ts")).getByText("delete")).toHaveClass("status-danger");
    expect(onAccept).not.toHaveBeenCalled();
    expect(onDiscard).not.toHaveBeenCalled();
    expect(onUndo).not.toHaveBeenCalled();
  });

  it("folds diffs independently and colors changed content but not file headers", async () => {
    const user = userEvent.setup();
    renderReview();
    const modified = fileRow("src/modified.ts");
    const toggle = within(modified).getByRole("button", { name: "Show changes" });
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle).toHaveAttribute("aria-controls");

    const code = modified.querySelector("pre code");
    expect(code?.textContent).toBe(CHANGES[1].diff);
    expect(code?.querySelector('[data-diff-line="added"]')?.textContent).toBe("+export const value = 2;\n");
    expect(code?.querySelector('[data-diff-line="removed"]')?.textContent).toBe("-export const value = 1;\n");
    expect((code?.querySelector('[data-diff-line="added"]') as HTMLElement).style.color).toBe("var(--success)");
    expect((code?.querySelector('[data-diff-line="removed"]') as HTMLElement).style.color).toBe("var(--danger)");
    expect(code?.querySelector('[data-diff-line="context"]')?.textContent).toBe("--- a/src/modified.ts\n");
    expect((code?.querySelector('[data-diff-line="context"]') as HTMLElement).style.color).toBe("");
    expect(fileRow("src/added.ts").querySelector("pre code")).toBeNull();

    await user.click(within(modified).getByRole("button", { name: "Hide changes" }));
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(modified.querySelector("pre code")).toBeNull();
  });

  it("only accepts the checked paths after a confirmation that names the count", async () => {
    const user = userEvent.setup();
    const { onAccept, onDiscard } = renderReview();
    await user.click(screen.getByRole("checkbox", { name: "Select src/modified.ts" }));
    expect(screen.getByRole("checkbox", { name: "Select src/modified.ts" })).not.toBeChecked();
    // A deleted file must be eligible for acceptance rather than hidden.
    expect(screen.getByRole("checkbox", { name: "Select src/removed.ts" })).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Accept selected to project" }));

    const confirm = screen.getByRole("group", { name: "Accept confirmation" });
    expect(within(confirm).getByText("Apply changes to 2 files in your project?")).toBeVisible();
    expect(within(confirm).getByRole("button", { name: "Confirm accept" })).toHaveFocus();
    expect(onAccept).not.toHaveBeenCalled();
    expect(screen.getByRole("checkbox", { name: "Select src/removed.ts" })).toBeDisabled();

    await user.click(within(confirm).getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Accept selected to project" })).toHaveFocus();
    expect(onAccept).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Accept selected to project" }));
    await user.click(screen.getByRole("button", { name: "Confirm accept" }));
    expect(onAccept).toHaveBeenCalledExactlyOnceWith(["src/added.ts", "src/removed.ts"]);
    expect(onDiscard).not.toHaveBeenCalled();
  });

  it("disables acceptance for no selected files and for an empty changeset", async () => {
    const user = userEvent.setup();
    const { onAccept } = renderReview();
    for (const change of CHANGES) {
      await user.click(screen.getByRole("checkbox", { name: "Select " + change.path }));
    }
    expect(screen.getByRole("button", { name: "Accept selected to project" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Accept selected to project" }));
    expect(screen.queryByRole("group", { name: "Accept confirmation" })).not.toBeInTheDocument();
    expect(onAccept).not.toHaveBeenCalled();
  });

  it("shows an empty-state message and no way to accept an empty changeset", () => {
    const { onAccept } = renderReview({ changes: [] });
    expect(screen.getByText("No files to review.")).toBeVisible();
    expect(screen.queryByRole("list", { name: "Changed files" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Accept selected to project" })).toBeDisabled();
    expect(onAccept).not.toHaveBeenCalled();
  });

  it("requires a separate confirmation before discarding the entire workcopy", async () => {
    const user = userEvent.setup();
    const { onAccept, onDiscard, onUndo } = renderReview();
    await user.click(screen.getByRole("checkbox", { name: "Select src/added.ts" }));
    await user.click(screen.getByRole("button", { name: "Discard all" }));
    const confirmation = screen.getByRole("group", { name: "Discard confirmation" });
    expect(within(confirmation).getByText("Discard all 3 pending file changes? No project files will be updated.")).toBeVisible();
    expect(within(confirmation).getByRole("button", { name: "Confirm discard" })).toHaveFocus();
    expect(onDiscard).not.toHaveBeenCalled();
    await user.click(within(confirmation).getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Discard all" })).toHaveFocus();
    expect(onDiscard).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Discard all" }));
    await user.click(screen.getByRole("button", { name: "Confirm discard" }));
    expect(onDiscard).toHaveBeenCalledOnce();
    expect(onAccept).not.toHaveBeenCalled();
    expect(onUndo).not.toHaveBeenCalled();
  });

  it("uses singular file counts in both confirmations", async () => {
    const user = userEvent.setup();
    renderReview({ changes: [CHANGES[0]] });
    await user.click(screen.getByRole("button", { name: "Accept selected to project" }));
    expect(screen.getByText("Apply changes to 1 file in your project?")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.click(screen.getByRole("button", { name: "Discard all" }));
    expect(screen.getByText("Discard all 1 pending file change? No project files will be updated.")).toBeVisible();
  });

  it("offers Undo only after acceptance is reported by the parent", async () => {
    const user = userEvent.setup();
    const onAccept = vi.fn();
    const onDiscard = vi.fn();
    const onUndo = vi.fn();

    function Host() {
      const [accepted, setAccepted] = useState(false);
      return (
        <ChangesReview
          summary="Done."
          changes={CHANGES}
          accepted={accepted}
          onAccept={(paths) => { onAccept(paths); setAccepted(true); }}
          onDiscard={onDiscard}
          onUndo={() => { onUndo(); setAccepted(false); }}
        />
      );
    }

    render(<Host />);
    expect(screen.queryByRole("button", { name: "Undo" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Accept selected to project" }));
    await user.click(screen.getByRole("button", { name: "Confirm accept" }));
    expect(onAccept).toHaveBeenCalledExactlyOnceWith(CHANGES.map((change) => change.path));
    expect(screen.getByRole("button", { name: "Undo" })).toHaveFocus();
    expect(screen.getByRole("status")).toHaveTextContent("Changes accepted to the project.");
    expect(screen.queryByRole("button", { name: "Discard all" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Accept selected to project" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Undo" }));
    expect(onUndo).not.toHaveBeenCalled();
    const undoConfirmation = screen.getByRole("group", { name: "Undo confirmation" });
    expect(within(undoConfirmation).getByText(/Restore the original project files/)).toBeVisible();
    await user.click(within(undoConfirmation).getByRole("button", { name: "Cancel" }));
    expect(onUndo).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Undo" }));
    await user.click(screen.getByRole("button", { name: "Confirm undo" }));
    expect(onUndo).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "Accept selected to project" })).toBeEnabled();
    expect(screen.getByRole("checkbox", { name: "Select src/added.ts" })).toHaveFocus();
    expect(onDiscard).not.toHaveBeenCalled();
  });

  it("starts on Undo when already accepted and supports keyboard undo", async () => {
    const user = userEvent.setup();
    const { onUndo } = renderReview({ accepted: true });
    const undo = screen.getByRole("button", { name: "Undo" });
    await waitFor(() => expect(undo).toHaveFocus());
    expect(screen.queryByRole("button", { name: "Accept selected to project" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("checkbox").every((checkbox) => (checkbox as HTMLInputElement).disabled)).toBe(true);
    await user.keyboard("{Enter}");
    expect(screen.getByRole("button", { name: "Confirm undo" })).toHaveFocus();
    expect(onUndo).not.toHaveBeenCalled();
    await user.keyboard("{Enter}");
    expect(onUndo).toHaveBeenCalledOnce();
  });

  it("supports keyboard discard and its separate confirmation", async () => {
    const user = userEvent.setup();
    const { onDiscard, onAccept } = renderReview();
    screen.getByRole("button", { name: "Discard all" }).focus();
    await user.keyboard(" ");
    expect(onDiscard).not.toHaveBeenCalled();
    const confirm = screen.getByRole("button", { name: "Confirm discard" });
    expect(confirm).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(onDiscard).toHaveBeenCalledOnce();
    expect(onAccept).not.toHaveBeenCalled();
  });

  it("treats summary, paths, and HTML-looking unified diff content as plain text", async () => {
    const user = userEvent.setup();
    const path = "src/<img src=x onerror=alert(1)>.tsx";
    const diff = "--- a/old.tsx\n+++ b/new.tsx\n@@ -1 +1 @@\n-<script>bad()</script>\n+<img src=x onerror=alert(1)>\n";
    const summary = "Changed <script>bad()</script> to text.";
    renderReview({ summary, changes: [{ path, kind: "modify", diff }] });
    const dialog = screen.getByRole("alertdialog");
    expect(screen.getByText(summary)).toBeVisible();
    expect(screen.getByText(path)).toBeVisible();
    expect(dialog.querySelector("img, script, pre code")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Show changes" }));
    expect(dialog.querySelector("pre code")?.textContent).toBe(diff);
    expect(dialog.querySelector("img, script")).toBeNull();
  });

  it("keeps selections and folds across equivalent rerenders, but resets on a new changeset", async () => {
    const user = userEvent.setup();
    const { rerender, props, onAccept } = renderReview();
    await user.click(screen.getByRole("checkbox", { name: "Select src/added.ts" }));
    await user.click(within(fileRow("src/modified.ts")).getByRole("button", { name: "Show changes" }));
    rerender(<ChangesReview {...props} changes={CHANGES.map((change) => ({ ...change }))} />);
    expect(screen.getByRole("checkbox", { name: "Select src/added.ts" })).not.toBeChecked();
    expect(fileRow("src/modified.ts").querySelector("pre code")).not.toBeNull();

    const revised = CHANGES.map((change) => (
      change.path === "src/modified.ts" ? { ...change, diff: change.diff + "+another line\n" } : change
    ));
    rerender(<ChangesReview {...props} changes={revised} />);
    for (const change of revised) {
      expect(screen.getByRole("checkbox", { name: "Select " + change.path })).toBeChecked();
      expect(fileRow(change.path).querySelector("pre code")).toBeNull();
    }
    expect(onAccept).not.toHaveBeenCalled();
  });

  it("refuses stale accept consent when the diff changes during confirmation", async () => {
    const user = userEvent.setup();
    const { rerender, props, onAccept } = renderReview();
    await user.click(screen.getByRole("button", { name: "Accept selected to project" }));
    const changed = CHANGES.map((change) => (
      change.path === "src/added.ts" ? { ...change, diff: "+surprise\n" } : change
    ));
    rerender(<ChangesReview {...props} changes={changed} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Changes or selection changed.");
    expect(screen.getByRole("button", { name: "Confirm accept" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Confirm accept" }));
    expect(onAccept).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.click(screen.getByRole("button", { name: "Accept selected to project" }));
    await user.click(screen.getByRole("button", { name: "Confirm accept" }));
    expect(onAccept).toHaveBeenCalledExactlyOnceWith(CHANGES.map((change) => change.path));
  });

  it("refuses stale discard consent when new files appear", async () => {
    const user = userEvent.setup();
    const { rerender, props, onDiscard } = renderReview();
    await user.click(screen.getByRole("button", { name: "Discard all" }));
    rerender(<ChangesReview {...props} changes={[...CHANGES, { path: "new.ts", kind: "add", diff: "+new\n" }]} />);
    expect(screen.getByRole("button", { name: "Confirm discard" })).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("Cancel and review again.");
    await user.click(screen.getByRole("button", { name: "Confirm discard" }));
    expect(onDiscard).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.click(screen.getByRole("button", { name: "Discard all" }));
    await user.click(screen.getByRole("button", { name: "Confirm discard" }));
    expect(onDiscard).toHaveBeenCalledOnce();
  });

  it("allows toggle, checkbox, acceptance, and confirmation by keyboard", async () => {
    const user = userEvent.setup();
    const { onAccept } = renderReview();
    const toggle = within(fileRow("src/removed.ts")).getByRole("button", { name: "Show changes" });
    toggle.focus();
    await user.keyboard("{Enter}");
    expect(toggle).toHaveFocus();
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    await user.keyboard(" ");
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    const checkbox = screen.getByRole("checkbox", { name: "Select src/modified.ts" });
    checkbox.focus();
    await user.keyboard(" ");
    expect(checkbox).not.toBeChecked();
    const accept = screen.getByRole("button", { name: "Accept selected to project" });
    accept.focus();
    await user.keyboard("{Enter}");
    expect(onAccept).not.toHaveBeenCalled();
    const confirm = screen.getByRole("button", { name: "Confirm accept" });
    expect(confirm).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(onAccept).toHaveBeenCalledExactlyOnceWith(["src/added.ts", "src/removed.ts"]);
  });

  it("uses Escape to cancel only the confirmation and restores focus to its action", async () => {
    const user = userEvent.setup();
    const { onAccept, onDiscard } = renderReview();
    await user.click(screen.getByRole("button", { name: "Discard all" }));
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("group", { name: "Discard confirmation" })).not.toBeInTheDocument();
    expect(screen.getByRole("alertdialog")).toBeVisible();
    expect(screen.getByRole("button", { name: "Discard all" })).toHaveFocus();
    expect(onAccept).not.toHaveBeenCalled();
    expect(onDiscard).not.toHaveBeenCalled();
  });

  it("contains Tab within the review and restores focus on dismissal without calling actions", async () => {
    const user = userEvent.setup();
    const onAccept = vi.fn();
    const onDiscard = vi.fn();
    const onUndo = vi.fn();

    function Host() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>Open changes</button>
          {open && (
            <ChangesReview
              summary="Review pending changes."
              changes={CHANGES}
              accepted={false}
              onAccept={onAccept}
              onDiscard={onDiscard}
              onUndo={onUndo}
            />
          )}
        </>
      );
    }

    render(<Host />);
    const trigger = screen.getByRole("button", { name: "Open changes" });
    await user.click(trigger);
    const dialog = await screen.findByRole("alertdialog");
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
    const first = screen.getByRole("checkbox", { name: "Select src/added.ts" });
    const last = screen.getByRole("button", { name: "Accept selected to project" });
    last.focus();
    await user.tab();
    expect(first).toHaveFocus();
    await user.tab({ shift: true });
    expect(last).toHaveFocus();
    for (let index = 0; index < 12; index += 1) {
      await user.tab();
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
    await user.tab({ shift: true });
    expect(dialog.contains(document.activeElement)).toBe(true);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
    expect(onAccept).not.toHaveBeenCalled();
    expect(onDiscard).not.toHaveBeenCalled();
    expect(onUndo).not.toHaveBeenCalled();
  });
  it.each([false, true])("names discard as removing the work copy when nothing is left to review (recovery %s)", async (allowRecovery) => {
    const user = userEvent.setup();
    const { onDiscard } = renderReview({ changes: [], allowRecovery });
    expect(screen.getByText("No files to review.")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Discard all" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Discard work copy" }));
    const confirmation = screen.getByRole("group", { name: "Discard confirmation" });
    expect(within(confirmation).getByText(
      "Discard this run's work copy? It has no pending file changes. No project files will be updated.",
    )).toBeVisible();
    expect(screen.queryByText(/Discard all 0 pending/)).not.toBeInTheDocument();
    expect(onDiscard).not.toHaveBeenCalled();
    await user.click(within(confirmation).getByRole("button", { name: "Confirm discard" }));
    expect(onDiscard).toHaveBeenCalledTimes(1);
  });

  it("offers Undo alongside staged changes when the backend says a checkpoint exists", async () => {
    const user = userEvent.setup();
    const { onUndo, onAccept, onDiscard } = renderReview({ allowRecovery: true });
    expect(screen.getByRole("button", { name: "Accept selected to project" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Discard all" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Undo" }));
    expect(onUndo).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onUndo).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Undo" }));
    await user.click(screen.getByRole("button", { name: "Confirm undo" }));
    expect(onUndo).toHaveBeenCalledOnce();
    expect(onAccept).not.toHaveBeenCalled();
    expect(onDiscard).not.toHaveBeenCalled();
  });

  it("requires confirmation for journal recovery when an old run has no staged diff", async () => {
    const user = userEvent.setup();
    const { onUndo, onAccept } = renderReview({ changes: [], allowRecovery: true });
    expect(screen.getByText("No files to review.")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Undo previously accepted changes" }));
    expect(onUndo).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onUndo).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Undo previously accepted changes" }));
    await user.click(screen.getByRole("button", { name: "Confirm undo" }));
    expect(onUndo).toHaveBeenCalledOnce();
    expect(onAccept).not.toHaveBeenCalled();
  });

});
