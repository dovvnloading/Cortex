import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { CortexSettings, ModelResponse } from "../../../contracts/cortex-api";
import { LocalSetup } from "./LocalSetup";

const settings: CortexSettings = {
  models: { chat: null, title: null, translation: "translategemma:4b" },
};

describe("LocalSetup", () => {
  it("requires an explicit choice from the locally scanned inventory", async () => {
    const user = userEvent.setup();
    const onSelectModel = vi.fn<(model: string) => Promise<boolean>>().mockResolvedValue(true);
    const models: ModelResponse = {
      connection: { success: true, status: "connected", message: "Connected to Ollama." },
      required_models: [],
      optional_models: [],
      installed_models: ["local-chat:7b"],
      models: [],
    };

    render(<LocalSetup models={models} settings={settings} busy={false} setupUrl="https://ollama.com/download" onRescan={vi.fn<() => Promise<void>>().mockResolvedValue(undefined)} onSelectModel={onSelectModel} />);

    expect(screen.getByRole("heading", { name: "Select a local model" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Use selected model" })).toBeDisabled();
    await user.click(screen.getByRole("radio", { name: /local-chat:7b/i }));
    await user.click(screen.getByRole("button", { name: "Use selected model" }));

    expect(onSelectModel).toHaveBeenCalledWith("local-chat:7b");
  });

  it("clears a model choice that disappears after a rescan", async () => {
    const user = userEvent.setup();
    const onSelectModel = vi.fn<(model: string) => Promise<boolean>>().mockResolvedValue(true);
    const initialModels: ModelResponse = {
      connection: { success: true, status: "connected", message: "Connected to Ollama." },
      required_models: [],
      optional_models: [],
      installed_models: ["local-chat:7b", "local-chat:13b"],
      models: [{ name: "local-chat:7b" }, { name: "local-chat:13b" }],
    };
    const props = {
      settings,
      busy: false,
      setupUrl: "https://ollama.com/download",
      onRescan: vi.fn<() => Promise<void>>().mockResolvedValue(undefined),
      onSelectModel,
    };
    const { rerender } = render(<LocalSetup {...props} models={initialModels} />);

    await user.click(screen.getByRole("radio", { name: /local-chat:7b/i }));
    expect(screen.getByRole("button", { name: "Use selected model" })).toBeEnabled();

    rerender(
      <LocalSetup
        {...props}
        models={{ ...initialModels, installed_models: ["local-chat:13b"], models: [{ name: "local-chat:13b" }] }}
      />,
    );

    expect(screen.queryByRole("radio", { name: /local-chat:7b/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Use selected model" })).toBeDisabled();
  });

  it("supports keyboard model selection", async () => {
    const user = userEvent.setup();
    const models: ModelResponse = {
      connection: { success: true, status: "connected", message: "Connected to Ollama." },
      required_models: [],
      optional_models: [],
      installed_models: ["local-chat:7b", "local-chat:13b"],
      models: [{ name: "local-chat:7b" }, { name: "local-chat:13b" }],
    };

    render(<LocalSetup models={models} settings={settings} busy={false} setupUrl="https://ollama.com/download" onRescan={vi.fn<() => Promise<void>>().mockResolvedValue(undefined)} onSelectModel={vi.fn<(model: string) => Promise<boolean>>().mockResolvedValue(true)} />);

    const first = screen.getByRole("radio", { name: /local-chat:7b/i });
    const second = screen.getByRole("radio", { name: /local-chat:13b/i });
    first.focus();
    await user.keyboard("{ArrowDown}");

    await waitFor(() => expect(second).toHaveFocus());
    expect(second).toHaveAttribute("aria-checked", "true");
  });
});
