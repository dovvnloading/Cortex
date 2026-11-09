import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { CortexSettings, ModelResponse } from "../../../../contracts/cortex-api";
import type { ActiveModelJob, ModelJobKind } from "../../stores/useModelStore";
import { SettingsPanel } from "./SettingsPanel";

const settings: CortexSettings = {
  revision: 4,
  appearance: { theme: "dark" },
  models: { chat: "local-chat:7b", title: null, translation: "translategemma:4b" },
  generation: { temperature: 0.7, num_ctx: 4096, seed: -1 },
  memory: { enabled: true },
  translation: { enabled: true, target_language: "Spanish" },
};
const inventory: ModelResponse = {
  required_models: [],
  optional_models: [],
  installed_models: ["local-chat:7b"],
  models: [{ name: "local-chat:7b" }],
  connection: { success: true, status: "connected", message: "Connected." },
};
const secondary = (kind: ModelJobKind): ActiveModelJob => ({
  jobId: `secondary-${kind}`, kind, model: kind === "models" ? "ollama-scan" : "model.gguf",
  lastEventId: 0,
});

function showSettings({
  primaryKind, secondaries = {},
}: {
  primaryKind?: ModelJobKind | null;
  secondaries?: Record<string, ActiveModelJob>;
}) {
  const onCheck = vi.fn().mockResolvedValue(undefined);
  const onCancel = vi.fn();
  const view = render(
    <SettingsPanel
      settings={settings}
      memos={[]}
      saving={false}
      memoryBusy={false}
      onSave={vi.fn().mockResolvedValue(settings)}
      onAddMemory={vi.fn().mockResolvedValue(undefined)}
      onReplaceMemory={vi.fn().mockResolvedValue(undefined)}
      onClearMemory={vi.fn().mockResolvedValue(undefined)}
      models={inventory}
      modelBusy
      activeModelJobKind={primaryKind}
      modelProgress={primaryKind ? {
        model: primaryKind === "models" ? "Ollama scan" : "Running GGUF download",
        percent: 12, status: "Running",
      } : null}
      secondaryModelJobs={secondaries}
      onCancelModelJob={onCancel}
      setupUrl="https://ollama.com/download"
      onCheckModels={onCheck}
      onPullModel={vi.fn().mockResolvedValue(undefined)}
      llamacppStatus={{
        state: "idle", binary_present: false, loaded_model: null,
        last_error: null, models_directory: "",
      }}
      onDownloadGGUF={vi.fn().mockResolvedValue(undefined)}
      onClose={vi.fn()}
    />,
  );
  return { ...view, user: userEvent.setup(), onCheck, onCancel };
}

async function readyForm(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "System" }));
  await user.type(screen.getByLabelText(/Repo id/), "owner/name");
  await user.type(screen.getByLabelText("File name"), "model.gguf");
  return {
    rescan: screen.getByRole("button", { name: /Rescan local models/ }),
    download: screen.getByRole("button", { name: /^(Download model|Downloading…)$/ }),
  };
}

describe("independent model-job types in Settings", () => {
  it("lets Ollama rescan run during a GGUF download without permitting a second GGUF", async () => {
    const { user, onCheck } = showSettings({ primaryKind: "gguf_download" });
    const { rescan, download } = await readyForm(user);
    expect(rescan).toBeEnabled();
    expect(download).toBeDisabled();
    await user.click(rescan);
    expect(onCheck).toHaveBeenCalledTimes(1);
  });

  it("allows a GGUF download while an Ollama scan is busy, but blocks another scan", async () => {
    const { user } = showSettings({ primaryKind: "models" });
    const { rescan, download } = await readyForm(user);
    expect(rescan).toBeDisabled();
    expect(download).toBeEnabled();
  });

  it("blocks both kinds when each has an active backend job and retains independent Cancel", async () => {
    const second = secondary("models");
    const { user, onCancel } = showSettings({
      primaryKind: "gguf_download", secondaries: { [second.jobId]: second },
    });
    const { rescan, download } = await readyForm(user);
    expect(rescan).toBeDisabled();
    expect(download).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Cancel ollama-scan" }));
    expect(onCancel).toHaveBeenCalledWith(second.jobId);
    expect(within(screen.getByRole("status", { name: "Model operation progress" }))
      .getByRole("button", { name: "Cancel Running GGUF download" })).toBeEnabled();
  });

  it("fails closed when a busy operation has not yet revealed its job kind", async () => {
    const { user } = showSettings({ primaryKind: null });
    const { rescan, download } = await readyForm(user);
    expect(rescan).toBeDisabled();
    expect(download).toBeDisabled();
  });
});
