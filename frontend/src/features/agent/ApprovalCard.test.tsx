import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { ApprovalCard, type ApprovalRequest } from "./ApprovalCard";

const CLASSES = ["inspect", "build_test", "mutate_workcopy", "network", "dangerous", "ask"] as const;
const LEVELS = ["appcontainer", "unsandboxed"] as const;

function makeRequest(overrides: Partial<ApprovalRequest> = {}): ApprovalRequest {
  return {
    command: "git status --short",
    cls: "inspect",
    plain_text: "Reads project information without changing files.",
    sandbox_level: "appcontainer",
    network: false,
    ...overrides,
  };
}

function renderApproval(request = makeRequest()) {
  const onAllowOnce = vi.fn();
  const onAlwaysAllow = vi.fn();
  const onDeny = vi.fn();
  const view = render(
    <ApprovalCard
      request={request}
      onAllowOnce={onAllowOnce}
      onAlwaysAllow={onAlwaysAllow}
      onDeny={onDeny}
    />,
  );
  return { ...view, onAllowOnce, onAlwaysAllow, onDeny };
}

describe("ApprovalCard", () => {
  const combinations = CLASSES.flatMap((cls) => LEVELS.map((sandbox_level) => ({ cls, sandbox_level })));

  it.each(combinations)("shows safe actions for $cls / $sandbox_level", ({ cls, sandbox_level }) => {
    const { onAllowOnce, onAlwaysAllow, onDeny } = renderApproval(makeRequest({ cls, sandbox_level }));
    const dialog = screen.getByRole("alertdialog");
    expect(within(dialog).getByText("Reads project information without changing files.")).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "Show command" })).toHaveAttribute("aria-expanded", "false");
    expect(dialog.querySelector("pre code")).toBeNull();
    expect(within(dialog).getByRole("button", { name: "Deny" })).toBeEnabled();

    const allowOnce = within(dialog).getByRole("button", { name: "Allow once" });
    expect(allowOnce).toBeInTheDocument();
    if (cls === "dangerous") {
      expect(allowOnce).toBeDisabled();
      expect(within(dialog).getByRole("button", { name: "Review dangerous command" })).toBeEnabled();
    } else {
      expect(allowOnce).toBeEnabled();
      expect(within(dialog).queryByRole("button", { name: "Review dangerous command" })).not.toBeInTheDocument();
    }

    const always = within(dialog).queryByRole("button", { name: "Always allow in this project" });
    if (sandbox_level === "appcontainer" && (cls === "inspect" || cls === "build_test")) {
      expect(always).toBeEnabled();
    } else {
      expect(always).not.toBeInTheDocument();
    }

    if (sandbox_level === "appcontainer") {
      expect(within(dialog).getByText("Sandboxed")).toHaveClass("status-success");
      expect(within(dialog).queryByText("Runs as you")).not.toBeInTheDocument();
    } else {
      expect(within(dialog).getByText("Runs as you")).toHaveClass("status-danger");
      expect(within(dialog).queryByText("Sandboxed")).not.toBeInTheDocument();
    }
    expect(within(dialog).queryByText("Uses the internet")).not.toBeInTheDocument();
    expect(onAllowOnce).not.toHaveBeenCalled();
    expect(onAlwaysAllow).not.toHaveBeenCalled();
    expect(onDeny).not.toHaveBeenCalled();
  });

  it.each(CLASSES)("labels internet use and withholds Always allow for %s even if sandboxed", (cls) => {
    const { onAlwaysAllow } = renderApproval(makeRequest({ cls, network: true }));
    expect(screen.getByText("Uses the internet")).toHaveClass("status-danger");
    expect(screen.getByText("Sandboxed")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Always allow in this project" })).not.toBeInTheDocument();
    expect(onAlwaysAllow).not.toHaveBeenCalled();
  });

  it("shows both warnings for an unsandboxed network request", () => {
    renderApproval(makeRequest({ cls: "network", sandbox_level: "unsandboxed", network: true }));
    expect(screen.getByText("Runs as you")).toHaveClass("status-danger");
    expect(screen.getByText("Uses the internet")).toBeInTheDocument();
    expect(screen.queryByText("Sandboxed")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Always allow in this project" })).not.toBeInTheDocument();
  });

  it("folds the exact command and renders both command and explanation as text, not HTML", async () => {
    const user = userEvent.setup();
    const command = "echo '<img src=x onerror=alert(1)>' && echo '<script>bad()</script>'";
    const plain_text = "Cortex wants to read <img src=x onerror=alert(1)>.";
    const { onAllowOnce, onAlwaysAllow, onDeny } = renderApproval(makeRequest({ command, plain_text }));
    const dialog = screen.getByRole("alertdialog");
    const toggle = within(dialog).getByRole("button", { name: "Show command" });

    expect(within(dialog).getByText(plain_text)).toBeVisible();
    expect(dialog.querySelector("img, script, pre code")).toBeNull();
    await user.click(toggle);

    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle).toHaveAttribute("aria-controls");
    expect(dialog.querySelector("pre code")?.textContent).toBe(command);
    expect(dialog.querySelector("img, script")).toBeNull();

    await user.click(within(dialog).getByRole("button", { name: "Hide command" }));
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(dialog.querySelector("pre code")).toBeNull();
    expect(onAllowOnce).not.toHaveBeenCalled();
    expect(onAlwaysAllow).not.toHaveBeenCalled();
    expect(onDeny).not.toHaveBeenCalled();
  });

  it.each(["inspect", "build_test", "mutate_workcopy", "network", "ask"] as const)(
    "allows %s once only when the button is clicked",
    async (cls) => {
      const user = userEvent.setup();
      const { onAllowOnce, onAlwaysAllow, onDeny } = renderApproval(makeRequest({ cls }));
      await user.click(screen.getByRole("button", { name: "Allow once" }));
      expect(onAllowOnce).toHaveBeenCalledOnce();
      expect(onAlwaysAllow).not.toHaveBeenCalled();
      expect(onDeny).not.toHaveBeenCalled();
    },
  );

  it.each(["inspect", "build_test"] as const)("only invokes Always allow on a click for %s", async (cls) => {
    const user = userEvent.setup();
    const { onAllowOnce, onAlwaysAllow, onDeny } = renderApproval(makeRequest({ cls }));
    await user.click(screen.getByRole("button", { name: "Always allow in this project" }));
    expect(onAlwaysAllow).toHaveBeenCalledOnce();
    expect(onAllowOnce).not.toHaveBeenCalled();
    expect(onDeny).not.toHaveBeenCalled();
  });

  it("denies only after clicking Deny", async () => {
    const user = userEvent.setup();
    const { onAllowOnce, onAlwaysAllow, onDeny } = renderApproval();
    await user.click(screen.getByRole("button", { name: "Deny" }));
    expect(onDeny).toHaveBeenCalledOnce();
    expect(onAllowOnce).not.toHaveBeenCalled();
    expect(onAlwaysAllow).not.toHaveBeenCalled();
  });

  it("requires a second, exact-command acknowledgment for dangerous commands", async () => {
    const user = userEvent.setup();
    const command = "reg add HKCU\\Software\\Example /v Mode /d 1";
    const { onAllowOnce, onAlwaysAllow, onDeny } = renderApproval(makeRequest({ cls: "dangerous", command }));
    const allowOnce = screen.getByRole("button", { name: "Allow once" });
    expect(allowOnce).toBeDisabled();
    await user.click(allowOnce);
    expect(onAllowOnce).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Review dangerous command" }));
    const second = screen.getByRole("group", { name: "Dangerous command second confirmation" });
    expect(within(second).getByText(/Review the exact command/)).toBeVisible();
    expect(second.querySelector("pre code")?.textContent).toBe(command);
    expect(screen.getByRole("button", { name: "Show command" })).toHaveAttribute("aria-expanded", "false");

    const acknowledgment = within(second).getByRole("checkbox", { name: /I have reviewed this exact command/ });
    expect(acknowledgment).toHaveFocus();
    expect(allowOnce).toBeDisabled();
    await user.click(acknowledgment);
    expect(allowOnce).toBeEnabled();
    await user.click(allowOnce);

    expect(onAllowOnce).toHaveBeenCalledOnce();
    expect(onAlwaysAllow).not.toHaveBeenCalled();
    expect(onDeny).not.toHaveBeenCalled();
    expect(allowOnce).toBeDisabled();
  });

  it("invalidates a dangerous confirmation when the command changes", async () => {
    const user = userEvent.setup();
    const first = makeRequest({ cls: "dangerous", command: "reg query HKCU" });
    const second = makeRequest({ cls: "dangerous", command: "reg delete HKCU\\Software\\Example" });
    const { rerender, onAllowOnce, onAlwaysAllow, onDeny } = renderApproval(first);
    await user.click(screen.getByRole("button", { name: "Review dangerous command" }));
    await user.click(screen.getByRole("checkbox", { name: /I have reviewed this exact command/ }));
    expect(screen.getByRole("button", { name: "Allow once" })).toBeEnabled();

    rerender(
      <ApprovalCard request={second} onAllowOnce={onAllowOnce} onAlwaysAllow={onAlwaysAllow} onDeny={onDeny} />,
    );

    expect(screen.getByRole("button", { name: "Allow once" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Review dangerous command" })).toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(onAllowOnce).not.toHaveBeenCalled();
  });

  it("removes an existing Always allow choice when the network or sandbox changes", () => {
    const first = makeRequest();
    const { rerender, onAllowOnce, onAlwaysAllow, onDeny } = renderApproval(first);
    expect(screen.getByRole("button", { name: "Always allow in this project" })).toBeInTheDocument();

    rerender(
      <ApprovalCard request={{ ...first, network: true }} onAllowOnce={onAllowOnce} onAlwaysAllow={onAlwaysAllow} onDeny={onDeny} />,
    );
    expect(screen.queryByRole("button", { name: "Always allow in this project" })).not.toBeInTheDocument();

    rerender(
      <ApprovalCard request={{ ...first, sandbox_level: "unsandboxed" }} onAllowOnce={onAllowOnce} onAlwaysAllow={onAlwaysAllow} onDeny={onDeny} />,
    );
    expect(screen.queryByRole("button", { name: "Always allow in this project" })).not.toBeInTheDocument();
  });

  it("supports keyboard activation of the command toggle and the decision buttons", async () => {
    const user = userEvent.setup();
    const { onAllowOnce, onAlwaysAllow, onDeny } = renderApproval();
    const toggle = screen.getByRole("button", { name: "Show command" });
    toggle.focus();
    await user.keyboard("{Enter}");
    expect(toggle).toHaveFocus();
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("alertdialog").querySelector("pre code")?.textContent).toBe("git status --short");

    await user.keyboard(" ");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByRole("alertdialog").querySelector("pre code")).toBeNull();

    const always = screen.getByRole("button", { name: "Always allow in this project" });
    always.focus();
    await user.keyboard("{Enter}");
    expect(onAlwaysAllow).toHaveBeenCalledOnce();
    expect(onAllowOnce).not.toHaveBeenCalled();
    expect(onDeny).not.toHaveBeenCalled();
  });

  it("lets the dangerous acknowledgment and Allow once be completed with the keyboard", async () => {
    const user = userEvent.setup();
    const { onAllowOnce } = renderApproval(makeRequest({ cls: "dangerous" }));
    const review = screen.getByRole("button", { name: "Review dangerous command" });
    review.focus();
    await user.keyboard("{Enter}");
    const checkbox = screen.getByRole("checkbox", { name: /I have reviewed this exact command/ });
    expect(checkbox).toHaveFocus();
    await user.keyboard(" ");
    expect(checkbox).toBeChecked();
    const allow = screen.getByRole("button", { name: "Allow once" });
    allow.focus();
    await user.keyboard("{Enter}");
    expect(onAllowOnce).toHaveBeenCalledOnce();
  });

  it("traps Tab in the dialog, restores focus on Escape, and never treats dismissal as consent", async () => {
    const user = userEvent.setup();
    const onAllowOnce = vi.fn();
    const onAlwaysAllow = vi.fn();
    const onDeny = vi.fn();

    function Host() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>Open approval</button>
          {open && (
            <ApprovalCard request={makeRequest()} onAllowOnce={onAllowOnce} onAlwaysAllow={onAlwaysAllow} onDeny={onDeny} />
          )}
        </>
      );
    }

    render(<Host />);
    const trigger = screen.getByRole("button", { name: "Open approval" });
    await user.click(trigger);
    const dialog = await screen.findByRole("alertdialog");
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));

    for (let index = 0; index < 8; index += 1) {
      await user.tab();
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
    expect(onAllowOnce).not.toHaveBeenCalled();
    expect(onAlwaysAllow).not.toHaveBeenCalled();
    expect(onDeny).not.toHaveBeenCalled();
  });

  it("supports keyboard denial without invoking either approval callback", async () => {
    const user = userEvent.setup();
    const { onAllowOnce, onAlwaysAllow, onDeny } = renderApproval();
    screen.getByRole("button", { name: "Deny" }).focus();
    await user.keyboard("{Enter}");
    expect(onDeny).toHaveBeenCalledOnce();
    expect(onAllowOnce).not.toHaveBeenCalled();
    expect(onAlwaysAllow).not.toHaveBeenCalled();
  });
});
