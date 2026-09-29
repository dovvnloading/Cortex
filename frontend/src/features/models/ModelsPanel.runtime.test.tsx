import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { LlamaCppRuntimeStatus, ModelResponse } from "../../../../contracts/cortex-api";
import { ModelsPanel, type RuntimeControls } from "./ModelsPanel";

const models: ModelResponse = {
  required_models: [],
  optional_models: [],
  installed_models: [],
  models: [],
  connection: { success: true, status: "connected", message: "Connected." },
};

const idleStatus: LlamaCppRuntimeStatus = {
  state: "idle",
  binary_present: true,
  loaded_model: null,
  last_error: null,
  models_directory: "C:\\synthetic\\models",
};

const readyStatus: LlamaCppRuntimeStatus = {
  ...idleStatus,
  state: "ready",
  loaded_model: "gguf:synthetic.Q4_K_M.gguf",
  active_backend: "vulkan",
};

function runtimeControls(overrides: Partial<RuntimeControls> = {}): RuntimeControls {
  return {
    idleUnloadMinutes: 30,
    onIdleUnloadMinutesChange: vi.fn(),
    onUnload: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
}

function renderPanel(status: LlamaCppRuntimeStatus, runtime?: RuntimeControls) {
  const props = {
    models,
    busy: false,
    progress: null,
    setupUrl: "https://ollama.com/download",
    onCheck: vi.fn().mockResolvedValue(undefined),
    gguf: {
      directory: "",
      directoryDirty: false,
      onDirectoryChange: vi.fn(),
      onDownload: vi.fn().mockResolvedValue(undefined),
      busy: false,
    },
  };
  const view = render(<ModelsPanel {...props} llamacppStatus={status} runtime={runtime} />);
  return {
    ...view,
    rerenderWith: (nextStatus: LlamaCppRuntimeStatus, nextRuntime?: RuntimeControls) =>
      view.rerender(<ModelsPanel {...props} llamacppStatus={nextStatus} runtime={nextRuntime} />),
  };
}

describe("ModelsPanel unload control", () => {
  it("unloads the loaded model when asked", async () => {
    const user = userEvent.setup();
    const runtime = runtimeControls();
    renderPanel(readyStatus, runtime);

    await user.click(screen.getByRole("button", { name: "Unload model" }));

    expect(runtime.onUnload).toHaveBeenCalledTimes(1);
  });

  it("is disabled, and says so, when no model is loaded", () => {
    const runtime = runtimeControls();
    renderPanel(idleStatus, runtime);

    expect(screen.getByRole("button", { name: "Unload model" })).toBeDisabled();
    expect(screen.getByText("No local model is loaded right now.")).toBeVisible();
  });

  it("is disabled while a model is still loading", () => {
    renderPanel({ ...readyStatus, state: "starting", loaded_model: null }, runtimeControls());

    expect(screen.getByRole("button", { name: "Unload model" })).toBeDisabled();
  });

  it("shows progress and cannot be pressed twice while the unload is in flight", async () => {
    const user = userEvent.setup();
    let finish: () => void = () => {};
    const onUnload = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
    renderPanel(readyStatus, runtimeControls({ onUnload }));

    await user.click(screen.getByRole("button", { name: "Unload model" }));

    const busy = await screen.findByRole("button", { name: "Unloading…" });
    expect(busy).toBeDisabled();
    await user.click(busy);
    expect(onUnload).toHaveBeenCalledTimes(1);

    finish();
    await waitFor(() => expect(screen.getByRole("button", { name: "Unload model" })).toBeEnabled());
  });

  it("is left out where the build cannot unload a model", () => {
    renderPanel(readyStatus, runtimeControls({ onUnload: undefined }));

    expect(screen.queryByRole("button", { name: "Unload model" })).not.toBeInTheDocument();
    expect(screen.getByLabelText(/Unload an unused model after/)).toBeVisible();
  });

  it("shows no runtime controls when none are offered", () => {
    renderPanel(readyStatus);

    expect(screen.queryByRole("button", { name: "Unload model" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Unload an unused model after/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Advanced runtime options")).not.toBeInTheDocument();
  });
});

describe("ModelsPanel idle period", () => {
  it("shows the current period and reports a new one as it is typed", async () => {
    const user = userEvent.setup();
    const runtime = runtimeControls({ idleUnloadMinutes: 30 });
    renderPanel(idleStatus, runtime);
    const field = screen.getByLabelText(/Unload an unused model after/);
    expect(field).toHaveValue(30);

    await user.clear(field);
    await user.type(field, "45");

    expect(runtime.onIdleUnloadMinutesChange).toHaveBeenLastCalledWith(45);
  });

  it("accepts 0 as never", async () => {
    const user = userEvent.setup();
    const runtime = runtimeControls({ idleUnloadMinutes: 30 });
    renderPanel(idleStatus, runtime);
    const field = screen.getByLabelText(/Unload an unused model after/);

    await user.clear(field);
    await user.type(field, "0");

    expect(runtime.onIdleUnloadMinutesChange).toHaveBeenLastCalledWith(0);
    expect(screen.getByText(/0 keeps the model loaded until Cortex closes/)).toBeVisible();
  });

  it("does not report text that is not a period from 0 to a day", async () => {
    const user = userEvent.setup();
    const runtime = runtimeControls({ idleUnloadMinutes: 30 });
    renderPanel(idleStatus, runtime);
    const field = screen.getByLabelText(/Unload an unused model after/);

    await user.clear(field);
    await user.type(field, "1441");

    expect(runtime.onIdleUnloadMinutesChange).not.toHaveBeenCalledWith(1441);
    await user.clear(field);
    await user.type(field, "-5");
    expect(runtime.onIdleUnloadMinutesChange).not.toHaveBeenCalledWith(-5);
  });

  it("follows a period that changed elsewhere", () => {
    const view = renderPanel(idleStatus, runtimeControls({ idleUnloadMinutes: 30 }));

    view.rerenderWith(idleStatus, runtimeControls({ idleUnloadMinutes: 5 }));

    expect(screen.getByLabelText(/Unload an unused model after/)).toHaveValue(5);
  });
});
