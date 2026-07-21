import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { LocalModelMenu } from "./LocalModelMenu";

describe("LocalModelMenu", () => {
  it("renders a single discovered model as a quiet label and can rescan it", async () => {
    const user = userEvent.setup();
    const onRescan = vi.fn<() => Promise<void>>().mockResolvedValue(undefined);

    const { container } = render(
      <LocalModelMenu
        models={["local-chat:7b"]}
        selectedModel="local-chat:7b"
        onSelect={vi.fn()}
        onRescan={onRescan}
      />,
    );

    expect(screen.getByLabelText("Local model: local-chat:7b")).toBeVisible();
    expect(screen.queryByRole("button", { name: /selected local model/i })).not.toBeInTheDocument();
    expect(container.querySelector(".lucide-cpu")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Rescan local models" }));
    expect(onRescan).toHaveBeenCalledTimes(1);
  });

  it("selects only a supplied discovered model with keyboard navigation", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn<(model: string) => boolean>().mockReturnValue(true);

    const { container } = render(
      <LocalModelMenu
        models={["local-chat:7b", "local-chat:13b", "local-code:7b"]}
        selectedModel="local-chat:13b"
        onSelect={onSelect}
      />,
    );

    const trigger = screen.getByRole("button", { name: "Selected local model: local-chat:13b" });
    expect(container.querySelector(".lucide-cpu")).not.toBeInTheDocument();
    trigger.focus();
    await user.keyboard("{ArrowDown}");

    const selectedOption = screen.getByRole("option", { name: "local-chat:13b" });
    await waitFor(() => expect(selectedOption).toHaveFocus());
    await user.keyboard("{ArrowDown}");

    const nextOption = screen.getByRole("option", { name: "local-code:7b" });
    await waitFor(() => expect(nextOption).toHaveFocus());
    await user.keyboard("{Enter}");

    expect(onSelect).toHaveBeenCalledWith("local-code:7b");
    await waitFor(() => expect(screen.queryByRole("listbox")).not.toBeInTheDocument());
  });

  it("returns focus to the trigger when the menu closes with Escape", async () => {
    const user = userEvent.setup();

    render(
      <LocalModelMenu
        models={["local-chat:7b", "local-chat:13b"]}
        selectedModel="local-chat:7b"
        onSelect={vi.fn()}
      />,
    );

    const trigger = screen.getByRole("button", { name: "Selected local model: local-chat:7b" });
    trigger.focus();
    await user.keyboard("{ArrowDown}");
    await user.keyboard("{Escape}");

    await waitFor(() => expect(trigger).toHaveFocus());
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("does not invoke selection or rescanning while disabled", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    const onRescan = vi.fn();

    render(
      <LocalModelMenu
        models={["local-chat:7b", "local-chat:13b"]}
        selectedModel="local-chat:7b"
        onSelect={onSelect}
        onRescan={onRescan}
        disabled
      />,
    );

    const trigger = screen.getByRole("button", { name: "Selected local model: local-chat:7b" });
    expect(trigger).toBeDisabled();
    expect(screen.getByRole("button", { name: "Rescan local models" })).toBeDisabled();

    await user.click(trigger);
    await user.click(screen.getByRole("button", { name: "Rescan local models" }));

    expect(onSelect).not.toHaveBeenCalled();
    expect(onRescan).not.toHaveBeenCalled();
  });
});
