import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { CortexSettings, ModelResponse } from "../../../../contracts/cortex-api";
import { SettingsPanel } from "./SettingsPanel";

describe("SettingsPanel", () => {
  it("merges external settings changes before saving a mounted draft", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn<(settings: CortexSettings) => Promise<void>>().mockResolvedValue();
    const initialSettings: CortexSettings = {
      revision: 2,
      appearance: { theme: "dark" },
      models: { chat: null, title: null },
    };
    const latestSettings: CortexSettings = {
      revision: 3,
      appearance: { theme: "light" },
      models: { chat: "gguf:demo.Q4_K_M.gguf", title: null },
    };
    const models: ModelResponse = {
      required_models: [],
      optional_models: [],
      installed_models: ["gguf:demo.Q4_K_M.gguf"],
      models: [{ name: "gguf:demo.Q4_K_M.gguf" }],
      connection: { success: true, status: "connected", message: "Connected." },
    };
    const props = {
      memos: [],
      saving: false,
      memoryBusy: false,
      onSave,
      onAddMemory: vi.fn<(memo: string) => Promise<void>>().mockResolvedValue(),
      onReplaceMemory: vi.fn<(memos: string[]) => Promise<void>>().mockResolvedValue(),
      onClearMemory: vi.fn<() => Promise<void>>().mockResolvedValue(),
      models,
      modelBusy: false,
      modelProgress: null,
      setupUrl: "https://ollama.com/download",
      onCheckModels: vi.fn<() => Promise<void>>().mockResolvedValue(),
      onPullModel: vi.fn<(model: string) => Promise<void>>().mockResolvedValue(),
      llamacppStatus: { state: "idle" as const, binary_present: false, loaded_model: null, last_error: null, models_directory: "" },
      onDownloadGGUF: vi.fn().mockResolvedValue(undefined),
      onClose: vi.fn(),
    };

    const { rerender } = render(<SettingsPanel {...props} settings={initialSettings} />);
    // Keep a local theme edit, then simulate a model auto-selection and a
    // concurrent theme change arriving while this dialog remains mounted.
    await user.click(screen.getByRole("combobox", { name: "Theme" }));
    await user.click(screen.getByRole("option", { name: "System" }));
    rerender(<SettingsPanel {...props} settings={latestSettings} />);
    await user.click(screen.getByRole("button", { name: "Save settings" }));

    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({
      revision: 3,
      appearance: { theme: "system" },
      models: expect.objectContaining({ chat: "gguf:demo.Q4_K_M.gguf", title: null }),
    }));
  });

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
        llamacppStatus={{ state: "idle", binary_present: false, loaded_model: null, last_error: null, models_directory: "" }}
        onDownloadGGUF={vi.fn().mockResolvedValue(undefined)}
        onClose={vi.fn()}
      />,
    );

    expect(screen.queryByRole("checkbox", { name: /follow-up suggestions/i })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "AI Model" }));

    expect(screen.getByText(/Cortex lists models installed through Ollama and local \.gguf files/)).toBeVisible();
    expect(screen.queryByRole("textbox", { name: "Chat model tag" })).not.toBeInTheDocument();
    const picker = screen.getByRole("combobox", { name: "Chat model" });
    picker.focus();
    await user.keyboard("{ArrowDown}");
    await waitFor(() => expect(screen.getByRole("option", { name: "local-chat:13b" })).toHaveAttribute("data-highlighted"));
    await user.keyboard("{ArrowDown}");
    await waitFor(() => expect(screen.getByRole("option", { name: "local-chat:7b" })).toHaveAttribute("data-highlighted"));
    await user.keyboard("{Enter}");
    await waitFor(() => expect(screen.queryByRole("listbox")).not.toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Save settings" }));

    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({
      models: expect.objectContaining({ chat: "local-chat:7b", title: null }),
    }));
  });

  it("preserves the configured chat model when saving with an empty model inventory", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn<(settings: CortexSettings) => Promise<void>>().mockResolvedValue();
    const settings: CortexSettings = {
      appearance: { theme: "dark" },
      models: { chat: "gguf:mistral-7b.gguf", title: null, translation: "translategemma:4b" },
      generation: { temperature: 0.7, num_ctx: 4096, seed: -1, system_instructions: "" },
    };
    const models: ModelResponse = {
      required_models: [],
      optional_models: [],
      installed_models: [],
      models: [],
      connection: { success: false, status: "error", message: "Ollama is not running." },
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
        llamacppStatus={{ state: "idle", binary_present: false, loaded_model: null, last_error: null, models_directory: "" }}
        onDownloadGGUF={vi.fn().mockResolvedValue(undefined)}
        onClose={vi.fn()}
      />,
    );

    // An unrelated edit, e.g. toggling the theme, must not wipe the still-valid
    // configured chat model just because the inventory came back empty.
    await user.click(screen.getByRole("button", { name: "Save settings" }));

    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({
      models: expect.objectContaining({ chat: "gguf:mistral-7b.gguf", title: null }),
    }));
  });

  it("shows an active spinner and progress message while pulling the default translation model", async () => {
    const user = userEvent.setup();
    const settings: CortexSettings = {
      models: { chat: "local-chat:7b", title: null, translation: "translategemma:4b" },
      translation: { enabled: true, target_language: "Spanish" },
    };
    const models: ModelResponse = {
      required_models: [],
      optional_models: [],
      installed_models: ["local-chat:7b"],
      models: [{ name: "local-chat:7b" }],
      connection: { success: true, status: "connected", message: "Connected." },
    };

    const { container } = render(
      <SettingsPanel
        settings={settings}
        memos={[]}
        saving={false}
        memoryBusy={false}
        onSave={vi.fn<(next: CortexSettings) => Promise<void>>().mockResolvedValue()}
        onAddMemory={vi.fn<(memo: string) => Promise<void>>().mockResolvedValue()}
        onReplaceMemory={vi.fn<(memos: string[]) => Promise<void>>().mockResolvedValue()}
        onClearMemory={vi.fn<() => Promise<void>>().mockResolvedValue()}
        models={models}
        modelBusy
        modelProgress={{ model: "translategemma:4b", status: "downloading model layers", percent: 42 }}
        setupUrl="https://ollama.com/download"
        onCheckModels={vi.fn<() => Promise<void>>().mockResolvedValue()}
        onPullModel={vi.fn<(model: string) => Promise<void>>().mockResolvedValue()}
        llamacppStatus={{ state: "idle", binary_present: false, loaded_model: null, last_error: null, models_directory: "" }}
        onDownloadGGUF={vi.fn().mockResolvedValue(undefined)}
        onClose={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Translation" }));

    expect(screen.getByRole("button", { name: /Installing/ })).toBeDisabled();
    expect(screen.getByRole("status", { name: "Model installation status" })).toHaveTextContent("downloading model layers");
    expect(screen.getByRole("status", { name: "Model installation status" })).toHaveTextContent("42%");
    expect(container.querySelector(".translation-install-button .loading-spinner")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "System" }));
    expect(screen.getByRole("status", { name: "Model operation progress" })).toHaveAttribute("aria-busy", "true");
    expect(container.querySelector(".model-progress-spinner")).toBeInTheDocument();
  });

  it("lets a user turn automatic safe computation off", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn<(settings: CortexSettings) => Promise<void>>().mockResolvedValue();
    const settings: CortexSettings = {
      models: { chat: "local-chat:7b", title: null, translation: "translategemma:4b" },
      execution: { automatic_compute: true },
    };
    const models: ModelResponse = {
      required_models: [],
      optional_models: [],
      installed_models: ["local-chat:7b"],
      models: [{ name: "local-chat:7b" }],
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
        llamacppStatus={{ state: "idle", binary_present: false, loaded_model: null, last_error: null, models_directory: "" }}
        onDownloadGGUF={vi.fn().mockResolvedValue(undefined)}
        onClose={vi.fn()}
      />,
    );

    const toggle = screen.getByRole("checkbox", { name: "Use safe computation automatically" });
    expect(toggle).toBeChecked();
    await user.click(toggle);
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({
      execution: { automatic_compute: false },
    }));
  });

  it("lets a user bypass Cortex's default system prompt", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn<(settings: CortexSettings) => Promise<void>>().mockResolvedValue();
    const settings: CortexSettings = {
      models: { chat: "local-chat:7b", title: null, translation: "translategemma:4b" },
      generation: { temperature: 0.7, num_ctx: 4096, seed: -1, system_instructions: "", bypass_system_prompt: false },
    };
    const models: ModelResponse = {
      required_models: [],
      optional_models: [],
      installed_models: ["local-chat:7b"],
      models: [{ name: "local-chat:7b" }],
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
        llamacppStatus={{ state: "idle", binary_present: false, loaded_model: null, last_error: null, models_directory: "" }}
        onDownloadGGUF={vi.fn().mockResolvedValue(undefined)}
        onClose={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "AI Model" }));
    const toggle = screen.getByRole("checkbox", { name: "Bypass Cortex's default system prompt" });
    expect(toggle).not.toBeChecked();
    await user.click(toggle);
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({
      generation: expect.objectContaining({ bypass_system_prompt: true }),
    }));
  });
});
