import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { CortexSettings, ModelResponse } from "../../../contracts/cortex-api";
import { SettingsPanel } from "./SettingsPanel";

describe("SettingsPanel", () => {
  it("uses rounded local-model choices instead of editable model tag fields", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn<(settings: CortexSettings) => Promise<void>>().mockResolvedValue();
    const settings: CortexSettings = {
      appearance: { theme: "dark" },
      models: { chat: null, title: null, translation: "translategemma:4b" },
      generation: { temperature: 0.7, num_ctx: 4096, seed: -1, system_instructions: "" },
      memory: { enabled: true },
      translation: { enabled: false, target_language: "Spanish" },
      suggestions: { enabled: true, model: null },
    };
    const models: ModelResponse = {
      required_models: [],
      optional_models: [],
      installed_models: ["local-chat:7b", "local-chat:13b"],
      models: [{ name: "local-chat:7b" }, { name: "local-chat:13b" }],
      connection: { success: true, status: "connected", message: "Connected." },
    };

    render(
      <SettingsPanel
        settings={settings}
        memos={[]}
        saving={false}
        memoryBusy={false}
        onSave={onSave}
        onAddMemory={vi.fn<(memo: string) => Promise<void>>().mockResolvedValue()}
        onReplaceMemory={vi.fn<(memos: string[]) => Promise<void>>().mockResolvedValue()}
        onClearMemory={vi.fn<() => Promise<void>>().mockResolvedValue()}
        models={models}
        modelBusy={false}
        modelProgress={null}
        setupUrl="https://ollama.com/download"
        onCheckModels={vi.fn<() => Promise<void>>().mockResolvedValue()}
        onPullModel={vi.fn<(model: string) => Promise<void>>().mockResolvedValue()}
        onClose={vi.fn()}
      />,
    );

    expect(screen.queryByRole("checkbox", { name: /follow-up suggestions/i })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "AI Model" }));

    expect(screen.getByText(/Cortex scans the Ollama models installed on this PC/)).toBeVisible();
    expect(screen.queryByRole("textbox", { name: "Chat model tag" })).not.toBeInTheDocument();
    const picker = screen.getByRole("button", { name: "Chat model" });
    picker.focus();
    await user.keyboard("{ArrowDown}");
    await waitFor(() => expect(screen.getByRole("option", { name: "local-chat:13b" })).toHaveFocus());
    await user.keyboard("{ArrowDown}");
    await waitFor(() => expect(screen.getByRole("option", { name: "local-chat:7b" })).toHaveFocus());
    await user.keyboard("{Enter}");
    await user.click(screen.getByRole("button", { name: "Save settings" }));

    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({
      models: expect.objectContaining({ chat: "local-chat:7b", title: null }),
    }));
  });
});
