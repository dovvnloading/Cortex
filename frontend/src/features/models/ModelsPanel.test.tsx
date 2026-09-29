import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ModelDownloadRequest, ModelResponse } from "../../../../contracts/cortex-api";
import { ModelsPanel } from "./ModelsPanel";

const models: ModelResponse = {
  required_models: [],
  optional_models: [],
  installed_models: [],
  models: [],
  connection: { success: true, status: "connected", message: "Ready" },
};

function renderPanel(onDownload: (request: ModelDownloadRequest) => Promise<void>) {
  return render(
    <ModelsPanel
      models={models}
      busy={false}
      progress={null}
      setupUrl="https://ollama.com/download"
      onCheck={vi.fn<() => Promise<void>>().mockResolvedValue()}
      llamacppStatus={{ state: "idle", binary_present: false, models_directory: "C:/models", models_directory_exists: true }}
      gguf={{ directory: "", directoryDirty: false, onDirectoryChange: vi.fn(), onDownload, busy: false }}
    />,
  );
}

async function submitHuggingFaceDownload(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText(/Repo id/), "vendor/demo-GGUF");
  await user.type(screen.getByLabelText(/File name/), "demo.Q4_K_M.gguf");
  await user.click(screen.getByRole("button", { name: /Download model/ }));
}

describe("ModelsPanel GGUF download form", () => {
  it("shows the reason a download failed next to the form, not only in a notification", async () => {
    // The reason used to reach the person only as a toast, and the form said
    // "See the notification" after that toast had already gone.
    const user = userEvent.setup();
    const onDownload = vi.fn<(request: ModelDownloadRequest) => Promise<void>>()
      .mockRejectedValue(new Error("The downloaded file did not match the published SHA-256 checksum"));
    renderPanel(onDownload);

    await submitHuggingFaceDownload(user);

    const alert = await screen.findByText(/did not match the published SHA-256 checksum/);
    expect(alert).toHaveTextContent("The downloaded file did not match the published SHA-256 checksum. Check the details above and try again.");
    expect(alert).toHaveAttribute("role", "alert");
    expect(alert).not.toHaveTextContent("notification");
    expect(onDownload).toHaveBeenCalledWith({ source: "huggingface", repo_id: "vendor/demo-GGUF", filename: "demo.Q4_K_M.gguf" });
    expect(screen.getByLabelText(/Repo id/)).toHaveValue("vendor/demo-GGUF");
  });

  it("falls back to a generic sentence when the rejection carries no message", async () => {
    const user = userEvent.setup();
    renderPanel(vi.fn<(request: ModelDownloadRequest) => Promise<void>>().mockRejectedValue(undefined));

    await submitHuggingFaceDownload(user);

    expect(await screen.findByText("The download did not complete. Check the details above and try again.")).toHaveAttribute("role", "alert");
  });

  it("clears the reason when the next download starts and empties the form when it succeeds", async () => {
    const user = userEvent.setup();
    const onDownload = vi.fn<(request: ModelDownloadRequest) => Promise<void>>()
      .mockRejectedValueOnce(new Error("Connection reset"))
      .mockResolvedValueOnce();
    renderPanel(onDownload);

    await submitHuggingFaceDownload(user);
    expect(await screen.findByText(/Connection reset\./)).toBeVisible();

    await user.click(screen.getByRole("button", { name: /Download model/ }));

    expect(screen.queryByText(/Connection reset/)).not.toBeInTheDocument();
    expect(screen.getByLabelText(/Repo id/)).toHaveValue("");
  });
});
