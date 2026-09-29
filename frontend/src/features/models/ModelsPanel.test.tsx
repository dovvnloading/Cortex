import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { HuggingFaceFileListResponse, LlamaCppRuntimeStatus, ModelResponse } from "../../../../contracts/cortex-api";
import { ApiError } from "../../api/client";
import { ModelJobCancelledError } from "../../lib/modelJobs";
import type { ListGGUFFiles } from "./GGUFDownloadForm";
import { ModelsPanel } from "./ModelsPanel";

const GIB = 1024 ** 3;

const models: ModelResponse = {
  required_models: [],
  optional_models: [],
  installed_models: [],
  connection: { success: true, status: "connected", message: "Ready" },
};

const llamacppStatus: LlamaCppRuntimeStatus = {
  state: "idle",
  binary_present: false,
  models_directory: "C:\\synthetic\\models",
  models_directory_exists: true,
};

const listing: HuggingFaceFileListResponse = {
  repo_id: "owner/name",
  files: [],
  entries: [
    { path: "model.Q4_K_M.gguf", size: 4 * GIB },
    { path: "weights/model.Q8_0.gguf", size: 8 * GIB },
    { path: "big/model-Q6_K-00001-of-00003.gguf", size: 4 * GIB },
    { path: "big/model-Q6_K-00002-of-00003.gguf", size: 4 * GIB },
    { path: "big/model-Q6_K-00003-of-00003.gguf", size: 4 * GIB },
    { path: "broken/model-00001-of-00002.gguf", size: 1 },
  ],
};

type PanelOptions = {
  onListFiles?: ListGGUFFiles | null;
  onDownload?: (request: unknown) => Promise<void>;
  busy?: boolean;
  progress?: { model: string; status: string; percent: number | null; cancelling?: boolean } | null;
  onCancel?: (() => void) | null;
};

function renderPanel(options: PanelOptions = {}) {
  const onListFiles = options.onListFiles === null
    ? undefined
    : options.onListFiles ?? vi.fn<ListGGUFFiles>().mockResolvedValue(listing);
  const onDownload = vi.fn(options.onDownload ?? (() => Promise.resolve()));
  const onCancel = options.onCancel === null ? undefined : options.onCancel ?? vi.fn();
  const view = render(
    <ModelsPanel
      models={models}
      busy={options.busy ?? false}
      progress={options.progress ?? null}
      onCancel={onCancel}
      setupUrl="https://ollama.com/download"
      onCheck={vi.fn().mockResolvedValue(undefined)}
      llamacppStatus={llamacppStatus}
      gguf={{
        directory: "",
        directoryDirty: false,
        onDirectoryChange: vi.fn(),
        onDownload,
        busy: options.busy ?? false,
        onListFiles,
      }}
    />,
  );
  return { ...view, onListFiles, onDownload, onCancel, user: userEvent.setup() };
}

const fileNameInput = () => screen.getByLabelText("File name") as HTMLInputElement;

async function browse(user: ReturnType<typeof userEvent.setup>, repo = "owner/name") {
  await user.type(screen.getByLabelText(/Repo id/), repo);
  await user.click(screen.getByRole("button", { name: /Browse files/ }));
}

describe("the progress row's Cancel control", () => {
  const progress = { model: "demo.Q4_K_M.gguf", status: "Downloading (1.0 GB of 4.0 GB)", percent: 25 };

  it("stops the operation that is running", async () => {
    const { user, onCancel } = renderPanel({ busy: true, progress });

    await user.click(screen.getByRole("button", { name: "Cancel demo.Q4_K_M.gguf" }));

    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("says it is cancelling and cannot be pressed twice", () => {
    renderPanel({ busy: true, progress: { ...progress, status: "Cancelling...", cancelling: true } });

    const button = screen.getByRole("button", { name: "Cancel demo.Q4_K_M.gguf" });
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent("Cancelling…");
  });

  it("is not offered when nothing is running, or when the panel has no way to cancel", () => {
    const idle = renderPanel({ busy: false, progress });
    expect(screen.queryByRole("button", { name: /^Cancel/ })).not.toBeInTheDocument();
    idle.unmount();

    renderPanel({ busy: true, progress, onCancel: null });
    expect(screen.queryByRole("button", { name: /^Cancel/ })).not.toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Model operation progress" })).toHaveTextContent("25%");
  });
});

describe("browsing a Hugging Face repository", () => {
  it("shows the .gguf files with their sizes, by folder, with a split model as one entry", async () => {
    const { user, onListFiles } = renderPanel();

    await browse(user);

    expect(onListFiles).toHaveBeenCalledWith("owner/name", expect.any(AbortSignal));
    const top = within(await screen.findByRole("group", { name: "Top level of the repository" }));
    expect(top.getByRole("radio", { name: /model\.Q4_K_M\.gguf.*4\.0 GB/ })).toBeInTheDocument();
    expect(within(screen.getByRole("group", { name: "weights" })).getByRole("radio", { name: /model\.Q8_0\.gguf.*8\.0 GB/ })).toBeInTheDocument();
    const split = within(screen.getByRole("group", { name: "big" })).getAllByRole("radio");
    expect(split).toHaveLength(1);
    expect(split[0]).toHaveAccessibleName(/model-Q6_K\.gguf.*3 parts.*12 GB/);
    expect(split[0]).toBeEnabled();
  });

  it("does not offer a split model that is missing a part", async () => {
    const { user } = renderPanel();
    await browse(user);

    const incomplete = await screen.findByRole("radio", { name: /model\.gguf.*missing a part/ });

    expect(incomplete).toBeDisabled();
  });

  it("fills in the file name when one is picked, and downloads exactly that file", async () => {
    const { user, onDownload } = renderPanel();
    await browse(user);

    await user.click(await screen.findByRole("radio", { name: /model\.Q8_0\.gguf/ }));

    expect(fileNameInput()).toHaveValue("weights/model.Q8_0.gguf");
    expect(screen.getByText(/Will download/)).toHaveTextContent("weights/model.Q8_0.gguf");
    await user.click(screen.getByRole("button", { name: "Download model" }));
    expect(onDownload).toHaveBeenCalledWith({
      source: "huggingface",
      repo_id: "owner/name",
      filename: "weights/model.Q8_0.gguf",
    });
  });

  it("asks for a split model by its first part and says every part will come", async () => {
    const { user, onDownload } = renderPanel();
    await browse(user);

    await user.click(await screen.findByRole("radio", { name: /model-Q6_K\.gguf/ }));

    expect(fileNameInput()).toHaveValue("big/model-Q6_K-00001-of-00003.gguf");
    expect(screen.getByText(/Will download all 3 parts/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Download model" }));
    expect(onDownload).toHaveBeenCalledWith(expect.objectContaining({ filename: "big/model-Q6_K-00001-of-00003.gguf" }));
  });

  it("waits for a repository id that looks like owner/name, and Enter browses too", async () => {
    const { user, onListFiles } = renderPanel();
    const button = screen.getByRole("button", { name: /Browse files/ });
    const repo = screen.getByLabelText(/Repo id/);

    expect(button).toBeDisabled();
    await user.type(repo, "owner");
    expect(button).toBeDisabled();
    await user.type(repo, "/name{Enter}");

    expect(onListFiles).toHaveBeenCalledTimes(1);
    expect(await screen.findByRole("radio", { name: /model\.Q4_K_M\.gguf/ })).toBeInTheDocument();
  });

  it("says so when a repository has no .gguf files", async () => {
    const { user } = renderPanel({ onListFiles: vi.fn<ListGGUFFiles>().mockResolvedValue({ repo_id: "owner/name", files: [], entries: [] }) });

    await browse(user);

    expect(await screen.findByRole("status")).toHaveTextContent("No .gguf files were found in owner/name");
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
  });

  it("reads a listing from an older backend that names files only", async () => {
    const { user } = renderPanel({
      onListFiles: vi.fn<ListGGUFFiles>().mockResolvedValue({ repo_id: "owner/name", files: ["old.Q4.gguf"] }),
    });

    await browse(user);

    expect(await screen.findByRole("radio", { name: "old.Q4.gguf" })).toBeInTheDocument();
  });

  it("drops a listing that was asked for and is no longer wanted", async () => {
    let signal: AbortSignal | undefined;
    let resolveListing!: (response: HuggingFaceFileListResponse) => void;
    const onListFiles = vi.fn<ListGGUFFiles>((_repo, requestSignal) => {
      signal = requestSignal;
      return new Promise((resolve) => { resolveListing = resolve; });
    });
    const { user } = renderPanel({ onListFiles });
    await browse(user, "owner/first");
    expect(await screen.findByText(/Looking up the .gguf files in owner\/first/)).toBeInTheDocument();

    await user.type(screen.getByLabelText(/Repo id/), "x");

    expect(signal?.aborted).toBe(true);
    expect(screen.queryByText(/Looking up/)).not.toBeInTheDocument();
    resolveListing(listing);
    await waitFor(() => expect(screen.queryByRole("radio")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: /Browse files/ })).toBeEnabled();
  });

  it("abandons a listing that is still running when the panel goes away", async () => {
    let signal: AbortSignal | undefined;
    const onListFiles = vi.fn<ListGGUFFiles>((_repo, requestSignal) => {
      signal = requestSignal;
      return new Promise(() => undefined);
    });
    const { user, unmount } = renderPanel({ onListFiles });
    await browse(user);
    await screen.findByText(/Looking up/);

    unmount();

    expect(signal?.aborted).toBe(true);
  });

  it("forgets a picked file when the repository changes, but keeps one that was typed", async () => {
    const { user } = renderPanel();
    await browse(user);
    await user.click(await screen.findByRole("radio", { name: /model\.Q4_K_M\.gguf/ }));
    expect(fileNameInput()).toHaveValue("model.Q4_K_M.gguf");

    await user.type(screen.getByLabelText(/Repo id/), "2");
    expect(fileNameInput()).toHaveValue("");
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();

    await user.type(fileNameInput(), "typed.gguf");
    await user.type(screen.getByLabelText(/Repo id/), "3");
    expect(fileNameInput()).toHaveValue("typed.gguf");
  });

  it("keeps the typed file name behind an advanced disclosure, closed until it is wanted", async () => {
    const { user } = renderPanel();

    expect(fileNameInput()).not.toBeVisible();
    await user.click(screen.getByText("Advanced: type a file name"));
    expect(fileNameInput()).toBeVisible();
  });

  it("is only a file name field when the panel cannot list files", () => {
    renderPanel({ onListFiles: null });

    expect(screen.queryByRole("button", { name: /Browse files/ })).not.toBeInTheDocument();
    expect(fileNameInput()).toBeVisible();
  });
});

describe("when Hugging Face will not list a repository", () => {
  const refusal = (code: string | null, message: string) => vi.fn<ListGGUFFiles>().mockRejectedValue(new ApiError(400, message, "http", code));

  it.each([
    ["gated", "This Hugging Face repository is gated or private. Set HF_TOKEN.", true],
    ["not_found", "Hugging Face has no such repository or file. Check the exact repository id.", true],
    ["rate_limited", "Hugging Face is rate-limiting requests right now. Wait a few minutes.", false],
    ["network", "Could not reach Hugging Face. The connection timed out.", false],
    ["unavailable", "Hugging Face had a server problem (HTTP 503).", false],
    [null, "A Hugging Face repo id must look like 'owner/name'.", false],
  ] as const)("names a %s failure and points to the next step", async (code, message, opensRepository) => {
    const { user } = renderPanel({ onListFiles: refusal(code, message) });

    await browse(user);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(message);
    expect(alert).toHaveTextContent("You can still type the file name yourself.");
    const link = within(alert).queryByRole("link", { name: /Open owner\/name on Hugging Face/ });
    if (opensRepository) expect(link).toHaveAttribute("href", "https://huggingface.co/owner/name");
    else expect(link).toBeNull();
    expect(screen.getByRole("button", { name: /Try again/ })).toBeEnabled();
  });

  it("does not stand between the person and a download typed by hand", async () => {
    const { user, onDownload } = renderPanel({ onListFiles: refusal("gated", "This repository is gated.") });
    await browse(user);
    await screen.findByRole("alert");

    // The advanced field opened by itself.
    expect(fileNameInput()).toBeVisible();
    await user.type(fileNameInput(), "typed.Q4_K_M.gguf");
    await user.click(screen.getByRole("button", { name: "Download model" }));

    expect(onDownload).toHaveBeenCalledWith({ source: "huggingface", repo_id: "owner/name", filename: "typed.Q4_K_M.gguf" });
  });

  it("can be tried again once the problem is gone", async () => {
    const onListFiles = vi.fn<ListGGUFFiles>()
      .mockRejectedValueOnce(new ApiError(400, "Hugging Face is rate-limiting requests.", "http", "rate_limited"))
      .mockResolvedValueOnce(listing);
    const { user } = renderPanel({ onListFiles });
    await browse(user);
    await screen.findByRole("alert");

    await user.click(screen.getByRole("button", { name: /Try again/ }));

    expect(await screen.findByRole("radio", { name: /model\.Q4_K_M\.gguf/ })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows nothing for a listing the person abandoned", async () => {
    const onListFiles = vi.fn<ListGGUFFiles>().mockRejectedValue(new ApiError(0, "The request was cancelled.", "aborted"));
    const { user } = renderPanel({ onListFiles });

    await browse(user);

    await waitFor(() => expect(onListFiles).toHaveBeenCalled());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("explains a failure that has no message of its own", async () => {
    const { user } = renderPanel({ onListFiles: vi.fn<ListGGUFFiles>().mockRejectedValue(new TypeError("boom")) });

    await browse(user);

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not list the files in this repository.");
  });
});

describe("how a download ends", () => {
  async function fillAndDownload(user: ReturnType<typeof userEvent.setup>) {
    await user.type(screen.getByLabelText(/Repo id/), "owner/name");
    await user.type(fileNameInput(), "model.gguf");
    await user.click(screen.getByRole("button", { name: "Download model" }));
  }

  it("clears the form once the model is downloaded", async () => {
    const { user, onDownload } = renderPanel();

    await fillAndDownload(user);

    await waitFor(() => expect(screen.getByLabelText(/Repo id/)).toHaveValue(""));
    expect(fileNameInput()).toHaveValue("");
    expect(onDownload).toHaveBeenCalledTimes(1);
  });

  it("says why a download failed, in the backend's words, and keeps what was typed", async () => {
    const reason = "Hugging Face has no such repository or file, or it is private. Check the exact repository id and file name.";
    const { user } = renderPanel({ onDownload: () => Promise.reject(new Error(reason)) });

    await fillAndDownload(user);

    expect(await screen.findByRole("alert")).toHaveTextContent(reason);
    expect(screen.getByLabelText(/Repo id/)).toHaveValue("owner/name");
    expect(fileNameInput()).toHaveValue("model.gguf");
  });

  it("does not call a cancelled download a failure", async () => {
    const { user } = renderPanel({ onDownload: () => Promise.reject(new ModelJobCancelledError()) });

    await fillAndDownload(user);

    expect(await screen.findByText("Download cancelled.")).toHaveAttribute("role", "status");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(fileNameInput()).toHaveValue("model.gguf");
  });

  it("still gives a sentence when the download rejected with nothing to say", async () => {
    const { user } = renderPanel({ onDownload: () => Promise.reject("nope") });

    await fillAndDownload(user);

    expect(await screen.findByRole("alert")).toHaveTextContent("The download did not complete.");
  });

  it("cannot start a second download while one is running", () => {
    renderPanel({ busy: true });

    expect(screen.getByRole("button", { name: "Downloading…" })).toBeDisabled();
  });
});
