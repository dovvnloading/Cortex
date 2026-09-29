import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CortexSettings, ModelResponse } from "../../../../contracts/cortex-api";
import { navigate } from "../../lib/navigation";
import { SettingsPanel } from "./SettingsPanel";

type Save = (settings: CortexSettings) => Promise<CortexSettings | null | void>;

const models: ModelResponse = {
  required_models: [],
  optional_models: [],
  installed_models: ["local-chat:7b"],
  models: [{ name: "local-chat:7b" }],
  connection: { success: true, status: "connected", message: "Connected." },
};

const initialSettings = (): CortexSettings => ({
  revision: 4,
  appearance: { theme: "dark" },
  models: { chat: "local-chat:7b", title: null, translation: "translategemma:4b" },
  generation: { temperature: 0.7, num_ctx: 4096, seed: -1, system_instructions: "", bypass_system_prompt: false },
  memory: { enabled: true },
  translation: { enabled: true, target_language: "Spanish" },
});

/**
 * The panel as the app hosts it: settings live above it, a save replaces them,
 * and Close is a navigation (which is what the unsaved-changes guard hooks).
 */
function Host({ onSave, close = () => navigate("/chat/new") }: { onSave: Save; close?: () => void }) {
  const [settings, setSettings] = useState(initialSettings);
  return (
    <SettingsPanel
      settings={settings}
      memos={[]}
      saving={false}
      memoryBusy={false}
      onSave={async (next) => {
        const saved = await onSave(next);
        if (saved) setSettings(saved);
        return saved;
      }}
      onAddMemory={vi.fn().mockResolvedValue(undefined)}
      onReplaceMemory={vi.fn().mockResolvedValue(undefined)}
      onClearMemory={vi.fn().mockResolvedValue(undefined)}
      models={models}
      modelBusy={false}
      modelProgress={null}
      setupUrl="https://ollama.com/download"
      onCheckModels={vi.fn().mockResolvedValue(undefined)}
      onPullModel={vi.fn().mockResolvedValue(undefined)}
      llamacppStatus={{ state: "idle", binary_present: false, loaded_model: null, last_error: null, models_directory: "" }}
      onDownloadGGUF={vi.fn().mockResolvedValue(undefined)}
      onClose={close}
    />
  );
}

const savedAs = (next: CortexSettings): CortexSettings => ({ ...next, revision: (next.revision ?? 0) + 1 });
const saveButton = () => screen.getByRole("button", { name: "Save settings" });

/** Change the theme to Light: the smallest real edit. */
async function editTheme(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("combobox", { name: "Theme" }));
  await user.click(await screen.findByRole("option", { name: "Light" }));
}

beforeEach(() => {
  window.history.replaceState({}, "", "/settings");
});
afterEach(() => {
  window.history.replaceState({}, "", "/");
});

describe("SettingsPanel unsaved changes", () => {
  it("keeps Save off and the pill away until something changes", async () => {
    const user = userEvent.setup();
    render(<Host onSave={vi.fn<Save>()} />);

    expect(saveButton()).toBeDisabled();
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();

    await editTheme(user);

    expect(saveButton()).toBeEnabled();
    expect(screen.getByText("Unsaved changes")).toBeVisible();
  });

  it("is clean again when an edit is put back", async () => {
    const user = userEvent.setup();
    render(<Host onSave={vi.fn<Save>()} />);

    await editTheme(user);
    await user.click(screen.getByRole("combobox", { name: "Theme" }));
    await user.click(await screen.findByRole("option", { name: "Dark" }));

    expect(saveButton()).toBeDisabled();
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();
  });

  it("is clean after a successful save and shows what the server stored", async () => {
    const user = userEvent.setup();
    // The server trims the target language, so what it returns differs from
    // what was typed. The draft must not stay "dirty" over that difference.
    const onSave = vi.fn<Save>(async (next) => savedAs({
      ...next,
      translation: { ...next.translation, target_language: (next.translation?.target_language ?? "").trim() },
    }));
    render(<Host onSave={onSave} />);

    await user.click(screen.getByRole("button", { name: "Translation" }));
    const language = screen.getByLabelText("Target language");
    await user.clear(language);
    await user.type(language, "French ");
    expect(screen.getByText("Unsaved changes")).toBeVisible();

    await user.click(saveButton());

    await waitFor(() => expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument());
    expect(saveButton()).toBeDisabled();
    expect(language).toHaveValue("French");
  });

  it("stays dirty when the save did not go through", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn<Save>().mockResolvedValue(null);
    render(<Host onSave={onSave} />);

    await editTheme(user);
    await user.click(saveButton());

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(screen.getByText("Unsaved changes")).toBeVisible();
    expect(saveButton()).toBeEnabled();
  });

  it("keeps an edit made while a save was in flight as unsaved", async () => {
    const user = userEvent.setup();
    let finish!: (saved: CortexSettings) => void;
    const onSave = vi.fn<Save>(() => new Promise((resolve) => { finish = resolve; }));
    render(<Host onSave={onSave} />);

    await editTheme(user);
    await user.click(saveButton());
    await user.click(screen.getByRole("button", { name: "AI Model" }));
    await user.click(screen.getByLabelText(/Bypass Cortex's default system prompt/));
    await act(async () => { finish(savedAs({ ...onSave.mock.calls[0][0] })); });

    // The theme was saved; the bypass toggle was not.
    expect(screen.getByText("Unsaved changes")).toBeVisible();
    expect(saveButton()).toBeEnabled();
    expect(screen.getByLabelText(/Bypass Cortex's default system prompt/)).toBeChecked();
  });

  it("closes straight away when there is nothing to lose", async () => {
    const user = userEvent.setup();
    render(<Host onSave={vi.fn<Save>()} />);

    await user.click(screen.getByRole("button", { name: "Close settings" }));

    expect(window.location.pathname).toBe("/chat/new");
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("asks before Close throws edits away, and Keep editing stays put with the edit intact", async () => {
    const user = userEvent.setup();
    render(<Host onSave={vi.fn<Save>()} />);
    await editTheme(user);

    await user.click(screen.getByRole("button", { name: "Close settings" }));

    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText("Save your changes?")).toBeVisible();
    expect(window.location.pathname).toBe("/settings");

    await user.click(within(dialog).getByRole("button", { name: "Keep editing" }));

    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(window.location.pathname).toBe("/settings");
    expect(screen.getByText("Unsaved changes")).toBeVisible();
    expect(screen.getByRole("combobox", { name: "Theme" })).toHaveTextContent("Light");
  });

  it("treats Escape on the prompt as Keep editing", async () => {
    const user = userEvent.setup();
    render(<Host onSave={vi.fn<Save>()} />);
    await editTheme(user);
    await user.click(screen.getByRole("button", { name: "Close settings" }));
    await screen.findByRole("alertdialog");

    await user.keyboard("{Escape}");

    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(window.location.pathname).toBe("/settings");
  });

  it("Discard leaves without saving", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn<Save>();
    render(<Host onSave={onSave} />);
    await editTheme(user);

    await user.click(screen.getByRole("button", { name: "Close settings" }));
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Discard" }));

    await waitFor(() => expect(window.location.pathname).toBe("/chat/new"));
    expect(onSave).not.toHaveBeenCalled();
  });

  it("Save and close saves the edits, then leaves", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn<Save>(async (next) => savedAs(next));
    render(<Host onSave={onSave} />);
    await editTheme(user);

    await user.click(screen.getByRole("button", { name: "Close settings" }));
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Save and close" }));

    await waitFor(() => expect(window.location.pathname).toBe("/chat/new"));
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ appearance: { theme: "light" } }));
  });

  it("Save and close does not leave when the save failed", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn<Save>().mockResolvedValue(null);
    render(<Host onSave={onSave} />);
    await editTheme(user);

    await user.click(screen.getByRole("button", { name: "Close settings" }));
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Save and close" }));

    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(window.location.pathname).toBe("/settings");
    expect(screen.getByText("Unsaved changes")).toBeVisible();
    expect(onSave).toHaveBeenCalledTimes(1);
  });

  it("Save and close does not leave when saving throws", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn<Save>().mockRejectedValue(new Error("network down"));
    render(<Host onSave={onSave} />);
    await editTheme(user);

    await user.click(screen.getByRole("button", { name: "Close settings" }));
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Save and close" }));

    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(window.location.pathname).toBe("/settings");
  });

  it("guards every way of navigating, not just Close", async () => {
    const user = userEvent.setup();
    render(<Host onSave={vi.fn<Save>()} />);
    await editTheme(user);

    // The sidebar, the palette's New chat and links all call navigate().
    act(() => navigate("/chat/thread-1"));

    await screen.findByRole("alertdialog");
    expect(window.location.pathname).toBe("/settings");
  });

  it("shows one prompt when navigations pile up, and the last one wins", async () => {
    const user = userEvent.setup();
    render(<Host onSave={vi.fn<Save>()} />);
    await editTheme(user);

    act(() => navigate("/chat/thread-1"));
    await screen.findByRole("alertdialog");
    act(() => navigate("/chat/thread-2"));

    expect(screen.getAllByRole("alertdialog")).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Discard" }));
    await waitFor(() => expect(window.location.pathname).toBe("/chat/thread-2"));
  });

  it("stops guarding once the panel is gone", async () => {
    const user = userEvent.setup();
    const { unmount } = render(<Host onSave={vi.fn<Save>()} />);
    await editTheme(user);
    unmount();

    navigate("/chat/new");

    expect(window.location.pathname).toBe("/chat/new");
  });

  it("asks the window before it closes or reloads with unsaved edits", async () => {
    const user = userEvent.setup();
    const { unmount } = render(<Host onSave={vi.fn<Save>()} />);
    const unload = () => {
      const event = new Event("beforeunload", { cancelable: true });
      window.dispatchEvent(event);
      return event.defaultPrevented;
    };

    expect(unload()).toBe(false);
    await editTheme(user);
    expect(unload()).toBe(true);

    unmount();
    expect(unload()).toBe(false);
  });
});
