import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import type { CortexSettings, LlamaCppRuntimeStatus, ModelResponse } from "../../../../contracts/cortex-api";
import { SettingsPanel } from "./SettingsPanel";

const models: ModelResponse = {
  required_models: [],
  optional_models: [],
  installed_models: [],
  models: [],
  connection: { success: true, status: "connected", message: "Connected." },
};

const readyStatus: LlamaCppRuntimeStatus = {
  state: "ready",
  binary_present: true,
  loaded_model: "gguf:synthetic.Q4_K_M.gguf",
  last_error: null,
  models_directory: "C:\\synthetic\\models",
  active_backend: "cpu",
};

const baseSettings: CortexSettings = {
  revision: 1,
  appearance: { theme: "dark" },
  models: { chat: null, title: null },
  llamacpp: { gpu_backend: "auto", idle_unload_minutes: 30 },
};

function panelProps(overrides: Partial<Parameters<typeof SettingsPanel>[0]> = {}) {
  return {
    settings: baseSettings,
    memos: [],
    saving: false,
    memoryBusy: false,
    onSave: vi.fn<(settings: CortexSettings) => Promise<CortexSettings | null | void>>().mockResolvedValue(),
    onAddMemory: vi.fn().mockResolvedValue(undefined),
    onReplaceMemory: vi.fn().mockResolvedValue(undefined),
    onClearMemory: vi.fn().mockResolvedValue(undefined),
    models,
    modelBusy: false,
    modelProgress: null,
    setupUrl: "https://ollama.com/download",
    onCheckModels: vi.fn().mockResolvedValue(undefined),
    onPullModel: vi.fn().mockResolvedValue(undefined),
    llamacppStatus: readyStatus,
    onDownloadGGUF: vi.fn().mockResolvedValue(undefined),
    onClose: vi.fn(),
    ...overrides,
  };
}

async function openSystem(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "System" }));
}

describe("SettingsPanel runtime memory settings", () => {
  it("saves a changed idle period inside the runtime settings, keeping the rest of them", async () => {
    const user = userEvent.setup();
    const props = panelProps();
    render(<SettingsPanel {...props} />);
    await openSystem(user);

    const field = screen.getByLabelText(/Unload an unused model after/);
    await user.clear(field);
    await user.type(field, "10");
    await user.click(screen.getByRole("button", { name: "Save settings" }));

    expect(props.onSave).toHaveBeenCalledWith(expect.objectContaining({
      llamacpp: { gpu_backend: "auto", idle_unload_minutes: 10 },
    }));
  });

  it("saves the advanced options as a list of words", async () => {
    const user = userEvent.setup();
    const props = panelProps();
    render(<SettingsPanel {...props} />);
    await openSystem(user);

    await user.type(screen.getByLabelText("Advanced runtime options"), "-ctk q8_0 -fa on");
    await user.click(screen.getByRole("button", { name: "Save settings" }));

    expect(props.onSave).toHaveBeenCalledWith(expect.objectContaining({
      llamacpp: expect.objectContaining({ extra_args: ["-ctk", "q8_0", "-fa", "on"] }),
    }));
  });

  it("is clean again once the saved list comes back, even as a new array", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn(async (next: CortexSettings) => {
      // The server answers with its own copy of what it stored.
      return { ...next, revision: (next.revision ?? 0) + 1, llamacpp: { ...next.llamacpp, extra_args: [...(next.llamacpp?.extra_args ?? [])] } };
    });

    function Harness() {
      const [settings, setSettings] = useState(baseSettings);
      return (
        <SettingsPanel
          {...panelProps({
            settings,
            onSave: async (next) => {
              const stored = await onSave(next);
              setSettings(stored);
              return stored;
            },
          })}
        />
      );
    }

    render(<Harness />);
    await openSystem(user);
    await user.type(screen.getByLabelText("Advanced runtime options"), "-t 8");
    expect(await screen.findByText("Unsaved changes")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Save settings" }));

    await waitFor(() => expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument());
    expect(screen.getByLabelText("Advanced runtime options")).toHaveValue("-t 8");
    expect(screen.getByRole("button", { name: "Save settings" })).toBeDisabled();
  });

  it("uses the backend defaults for a stored document that predates these settings", async () => {
    const user = userEvent.setup();
    render(<SettingsPanel {...panelProps({ settings: { revision: 1, models: { chat: null, title: null } } })} />);
    await openSystem(user);

    expect(screen.getByLabelText(/Unload an unused model after/)).toHaveValue(30);
  });

  it("starts the advanced options empty for a stored document that predates them", async () => {
    const user = userEvent.setup();
    render(<SettingsPanel {...panelProps({ settings: { revision: 1, models: { chat: null, title: null } } })} />);
    await openSystem(user);

    expect(screen.getByLabelText("Advanced runtime options")).toHaveValue("");
  });

  it("hands the unload request to the app and shows the button for a loaded model", async () => {
    const user = userEvent.setup();
    const onUnloadModel = vi.fn().mockResolvedValue(undefined);
    render(<SettingsPanel {...panelProps({ onUnloadModel })} />);
    await openSystem(user);

    await user.click(screen.getByRole("button", { name: "Unload model" }));

    expect(onUnloadModel).toHaveBeenCalledTimes(1);
  });

  it("offers no unload button when the app does not provide one", async () => {
    const user = userEvent.setup();
    render(<SettingsPanel {...panelProps()} />);
    await openSystem(user);

    expect(screen.queryByRole("button", { name: "Unload model" })).not.toBeInTheDocument();
  });
});
