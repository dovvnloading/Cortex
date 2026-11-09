import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { CortexSettings, ModelResponse } from "../../../../contracts/cortex-api";
import { navigate } from "../../lib/navigation";
import { SettingsPanel, type SettingsPanelProps } from "./SettingsPanel";

const settings: CortexSettings = {
  revision: 2,
  appearance: { theme: "dark" },
  models: { chat: null, title: null },
};
const models: ModelResponse = {
  required_models: [],
  optional_models: [],
  installed_models: [],
  models: [],
  connection: { success: true, status: "connected", message: "Ready" },
};

function renderMemorySettings(memos: string[]) {
  window.history.replaceState({}, "", "/settings");
  const onReplaceMemory = vi.fn<(next: string[]) => Promise<void>>().mockResolvedValue(undefined);
  const onSave = vi.fn<(next: CortexSettings) => Promise<CortexSettings | null>>()
    .mockResolvedValue({ ...settings, revision: settings.revision + 1 });
  const props: SettingsPanelProps = {
    settings,
    memos,
    saving: false,
    memoryBusy: false,
    onSave,
    onAddMemory: vi.fn<(memo: string) => Promise<void>>().mockResolvedValue(undefined),
    onReplaceMemory,
    onClearMemory: vi.fn<(expected: string[]) => Promise<void>>().mockResolvedValue(undefined),
    models,
    modelBusy: false,
    modelProgress: null,
    setupUrl: "https://ollama.com/download",
    onCheckModels: vi.fn<() => Promise<void>>().mockResolvedValue(undefined),
    onPullModel: vi.fn<(model: string) => Promise<void>>().mockResolvedValue(undefined),
    llamacppStatus: { state: "idle", binary_present: false, models_directory: "" },
    onDownloadGGUF: vi.fn().mockResolvedValue(undefined),
    onClose: () => navigate("/chat/new"),
  };
  render(<SettingsPanel {...props} />);
  return { onReplaceMemory, onSave };
}

afterEach(() => {
  window.history.replaceState({}, "", "/chat/new");
});

describe("Settings navigation with unsaved permanent memory", () => {
  it("guards edited rows across tab switches, page reload and Close; Keep editing retains them", async () => {
    const user = userEvent.setup();
    const { onReplaceMemory } = renderMemorySettings(["Original"]);
    await user.click(screen.getByRole("button", { name: "Memory" }));
    const input = screen.getByRole("textbox", { name: "Memory 1" });
    await user.clear(input);
    await user.type(input, "Edited in memory");

    const reload = new Event("beforeunload", { cancelable: true });
    act(() => { window.dispatchEvent(reload); });
    expect(reload.defaultPrevented).toBe(true);

    await user.click(screen.getByRole("button", { name: "General" }));
    await user.click(screen.getByRole("button", { name: "Close settings" }));
    const prompt = await screen.findByRole("alertdialog");
    expect(prompt).toHaveTextContent("permanent memory changes");
    expect(window.location.pathname).toBe("/settings");

    await user.click(within(prompt).getByRole("button", { name: "Keep editing" }));
    expect(window.location.pathname).toBe("/settings");
    await user.click(screen.getByRole("button", { name: "Memory" }));
    expect(screen.getByRole("textbox", { name: "Memory 1" })).toHaveValue("Edited in memory");
    expect(onReplaceMemory).not.toHaveBeenCalled();

    // This same route guard protects chat/sidebar navigation and native
    // Back/Forward. Discard is the only intentionally destructive path.
    act(() => navigate("/chat/another"));
    const again = await screen.findByRole("alertdialog");
    await user.click(within(again).getByRole("button", { name: "Discard" }));
    await waitFor(() => expect(window.location.pathname).toBe("/chat/another"));
    expect(onReplaceMemory).not.toHaveBeenCalled();
  });

  it("Save and close persists pending removals without writing unchanged Settings", async () => {
    const user = userEvent.setup();
    const { onReplaceMemory, onSave } = renderMemorySettings(["First", "Second"]);
    await user.click(screen.getByRole("button", { name: "Memory" }));
    await user.click(screen.getByRole("button", { name: "Remove memory 1" }));
    await user.click(screen.getByRole("button", { name: "Close settings" }));

    const prompt = await screen.findByRole("alertdialog");
    await user.click(within(prompt).getByRole("button", { name: "Save and close" }));
    await waitFor(() => expect(onReplaceMemory).toHaveBeenCalledWith(["Second"]));
    await waitFor(() => expect(window.location.pathname).toBe("/chat/new"));
    expect(onSave).not.toHaveBeenCalled();
  });

  it("does not navigate or discard edits after a conflicting memory Save and close", async () => {
    const user = userEvent.setup();
    const { onReplaceMemory, onSave } = renderMemorySettings(["Original"]);
    onReplaceMemory.mockRejectedValue(new Error("409 stale memories"));
    await user.click(screen.getByRole("button", { name: "Memory" }));
    const input = screen.getByRole("textbox", { name: "Memory 1" });
    await user.clear(input);
    await user.type(input, "My unsaved revision");
    await user.click(screen.getByRole("button", { name: "Close settings" }));

    const prompt = await screen.findByRole("alertdialog");
    await user.click(within(prompt).getByRole("button", { name: "Save and close" }));
    await waitFor(() => expect(onReplaceMemory).toHaveBeenCalledWith(["My unsaved revision"]));
    expect(window.location.pathname).toBe("/settings");
    expect(screen.getByRole("textbox", { name: "Memory 1" })).toHaveValue("My unsaved revision");
    expect(onSave).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Close settings" }));
    expect(await screen.findByRole("alertdialog")).toHaveTextContent("permanent memory changes");
  });
});
