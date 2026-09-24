import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { InstalledModel } from "../../../../contracts/cortex-api";
import { LocalModelMenu } from "./LocalModelMenu";

/** The option the keyboard is on: listbox focus stays put, activedescendant moves. */
function activeOption(owner: HTMLElement): HTMLElement | null {
  const id = owner.getAttribute("aria-activedescendant");
  return id ? document.getElementById(id) : null;
}

describe("LocalModelMenu", () => {
  it("lets the composer select the only discovered model and can rescan it", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn<(model: string) => boolean>().mockReturnValue(true);
    const onRescan = vi.fn<() => Promise<void>>().mockResolvedValue(undefined);

    const { container } = render(
      <LocalModelMenu
        models={["local-chat:7b"]}
        selectedModel={null}
        onSelect={onSelect}
        onRescan={onRescan}
      />,
    );

    expect(container.querySelector(".lucide-cpu")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Select a local model" }));
    await user.click(await screen.findByRole("option", { name: "local-chat:7b" }));
    expect(onSelect).toHaveBeenCalledWith("local-chat:7b");
    await waitFor(() => expect(screen.queryByRole("listbox")).not.toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Select a local model" }));
    await user.click(await screen.findByRole("button", { name: "Rescan local models" }));
    expect(onRescan).toHaveBeenCalledTimes(1);
    // Rescanning refreshes the list in place rather than dismissing it.
    expect(screen.getByRole("listbox", { name: "Discovered local models" })).toBeVisible();
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

    const listbox = await screen.findByRole("listbox", { name: "Discovered local models" });
    await waitFor(() => expect(listbox).toHaveFocus());
    expect(activeOption(listbox)).toBe(screen.getByRole("option", { name: "local-chat:13b" }));
    expect(screen.getByRole("option", { name: "local-chat:13b" })).toHaveAttribute("aria-selected", "true");

    await user.keyboard("{ArrowDown}");
    expect(activeOption(listbox)).toBe(screen.getByRole("option", { name: "local-code:7b" }));
    await user.keyboard("{ArrowDown}");
    expect(activeOption(listbox)).toBe(screen.getByRole("option", { name: "local-chat:7b" }));
    await user.keyboard("{End}");
    expect(activeOption(listbox)).toBe(screen.getByRole("option", { name: "local-code:7b" }));
    await user.keyboard("{Enter}");

    expect(onSelect).toHaveBeenCalledWith("local-code:7b");
    await waitFor(() => expect(screen.queryByRole("listbox")).not.toBeInTheDocument());
  });

  it("opens from a pointer click", async () => {
    const user = userEvent.setup();

    render(
      <LocalModelMenu
        models={["local-chat:7b", "local-chat:13b"]}
        selectedModel="local-chat:7b"
        onSelect={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Selected local model: local-chat:7b" }));
    expect(await screen.findByRole("listbox", { name: "Discovered local models" })).toBeVisible();
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

    const listbox = await screen.findByRole("listbox");
    await waitFor(() => expect(listbox).toHaveFocus());
    await user.keyboard("{Escape}");

    await waitFor(() => expect(trigger).toHaveFocus());
    await waitFor(() => expect(screen.queryByRole("listbox")).not.toBeInTheDocument());
  });

  it("does not open, select, or rescan while disabled", async () => {
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
    await user.click(trigger);

    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Rescan local models" })).not.toBeInTheDocument();
    expect(onSelect).not.toHaveBeenCalled();
    expect(onRescan).not.toHaveBeenCalled();
  });

  it("describes each model with its size, quantization, and vision support, grouped by source", async () => {
    const user = userEvent.setup();
    const details: InstalledModel[] = [
      { name: "local-chat:7b", parameter_size: "7B", quantization_level: "Q4_K_M", size: 4_100_000_000, supports_vision: true, source: "ollama" },
      { name: "gguf:coder.Q8_0.gguf", quantization_level: "Q8_0", size: 8_000_000_000, source: "gguf" },
    ];

    render(
      <LocalModelMenu
        models={["local-chat:7b", "gguf:coder.Q8_0.gguf"]}
        details={details}
        selectedModel="local-chat:7b"
        onSelect={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Selected local model: local-chat:7b" }));
    const chat = await screen.findByRole("option", { name: "local-chat:7b" });
    expect(chat).toHaveAccessibleDescription("7B · Q4_K_M · 3.8 GB");
    expect(within(chat).getByText("Vision")).toBeVisible();
    // The internal "gguf:" routing prefix never reaches the reader.
    expect(screen.getByRole("option", { name: "coder.Q8_0.gguf" })).toHaveAccessibleDescription("Q8_0 · 7.5 GB");
    expect(within(screen.getByRole("group", { name: "Ollama" })).getByRole("option", { name: "local-chat:7b" })).toBeVisible();
    expect(within(screen.getByRole("group", { name: "GGUF files" })).getByRole("option", { name: "coder.Q8_0.gguf" })).toBeVisible();
  });

  it("filters a large inventory from a search field and selects the first match with Enter", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn<(model: string) => boolean>().mockReturnValue(true);
    const models = ["alpha:1b", "bravo:2b", "charlie:3b", "delta:4b", "echo:5b", "foxtrot:6b", "qwen3-coder:30b", "zulu:7b"];

    render(<LocalModelMenu models={models} selectedModel="alpha:1b" onSelect={onSelect} />);

    await user.click(screen.getByRole("button", { name: "Selected local model: alpha:1b" }));
    const search = await screen.findByRole("combobox", { name: "Search local models" });
    await waitFor(() => expect(search).toHaveFocus());
    expect(screen.getAllByRole("option")).toHaveLength(models.length);

    await user.type(search, "zzz");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("No local model matches “zzz”.");

    await user.clear(search);
    await user.type(search, "coder");
    expect(screen.getAllByRole("option").map((option) => option.getAttribute("aria-label"))).toEqual(["qwen3-coder:30b"]);
    expect(activeOption(search)).toBe(screen.getByRole("option", { name: "qwen3-coder:30b" }));
    await user.keyboard("{Enter}");

    expect(onSelect).toHaveBeenCalledWith("qwen3-coder:30b");
  });

  it("keeps the highlight on the selected model while it still matches the search", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    const models = ["qwen3:4b", "qwen3:8b", "qwen3:14b", "gemma3:4b", "gemma3:12b", "llama3:8b", "phi4:14b"];

    render(<LocalModelMenu models={models} selectedModel="qwen3:8b" onSelect={onSelect} />);

    await user.click(screen.getByRole("button", { name: "Selected local model: qwen3:8b" }));
    const search = await screen.findByRole("combobox", { name: "Search local models" });
    await user.type(search, "qwen");
    expect(activeOption(search)).toBe(screen.getByRole("option", { name: "qwen3:8b" }));
    await user.keyboard("{Enter}");

    // Enter on the model already in use just closes the menu -- no re-save.
    expect(onSelect).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole("listbox")).not.toBeInTheDocument());
  });

  it("keeps the menu open when a selection could not be saved", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn<(model: string) => Promise<boolean>>().mockResolvedValue(false);

    render(
      <LocalModelMenu
        models={["local-chat:7b", "local-chat:13b"]}
        selectedModel="local-chat:7b"
        onSelect={onSelect}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Selected local model: local-chat:7b" }));
    await user.click(await screen.findByRole("option", { name: "local-chat:13b" }));

    expect(onSelect).toHaveBeenCalledWith("local-chat:13b");
    await waitFor(() => expect(screen.getByRole("option", { name: "local-chat:13b" })).not.toHaveAttribute("aria-disabled"));
    expect(screen.getByRole("listbox")).toBeVisible();
    expect(screen.getByRole("option", { name: "local-chat:7b" })).toHaveAttribute("aria-selected", "true");
  });

  it("explains an empty inventory and still offers a rescan", async () => {
    const user = userEvent.setup();
    const onRescan = vi.fn<() => Promise<void>>().mockResolvedValue(undefined);

    render(<LocalModelMenu models={[]} selectedModel={null} onSelect={vi.fn()} onRescan={onRescan} />);

    await user.click(screen.getByRole("button", { name: "No local models available" }));
    expect(await screen.findByRole("status")).toHaveTextContent("No local models found");
    await user.click(screen.getByRole("button", { name: "Rescan local models" }));
    expect(onRescan).toHaveBeenCalledTimes(1);
  });

  it("surfaces the selected model's runtime state on the trigger and in its row", async () => {
    const user = userEvent.setup();

    const { container } = render(
      <LocalModelMenu
        models={["gguf:demo.gguf", "local-chat:7b"]}
        selectedModel="gguf:demo.gguf"
        onSelect={vi.fn()}
        runtimeStatus={{ tone: "idle", label: "Not loaded yet", detail: "Loads when you send a message." }}
      />,
    );

    const trigger = screen.getByRole("button", { name: "Selected local model: demo.gguf" });
    expect(trigger).toHaveAttribute("title", "Not loaded yet — Loads when you send a message.");
    expect(container.querySelector(".model-picker-status-idle")).toBeInTheDocument();

    await user.click(trigger);
    expect(await screen.findByRole("option", { name: "demo.gguf" })).toHaveAccessibleDescription("Not loaded yet");
    expect(screen.getByRole("option", { name: "local-chat:7b" })).not.toHaveAccessibleDescription();
  });
});
