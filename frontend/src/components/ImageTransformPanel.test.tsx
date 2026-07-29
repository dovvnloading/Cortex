import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CortexApi } from "../api/client";
import { ImageTransformPanel } from "./ImageTransformPanel";

describe("ImageTransformPanel", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows active work and returns a downloadable fixed-recipe result", async () => {
    const user = userEvent.setup();
    const createObjectURL = vi.fn().mockReturnValueOnce("blob:source").mockReturnValueOnce("blob:result");
    vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL: vi.fn() });
    let finishStage: ((value: { artifact_id: string }) => void) | undefined;
    const api = {
      stageAttachment: vi.fn(() => new Promise<{ artifact_id: string }>((resolve) => { finishStage = resolve; })),
      startRecipeImageTransform: vi.fn().mockResolvedValue({ job_id: "image-job" }),
      executionStatus: vi.fn().mockResolvedValue({
        job_id: "image-job",
        status: "succeeded",
        result: { artifact_id: "result-artifact", mime_type: "image/png" },
      }),
      downloadExecutionArtifact: vi.fn().mockResolvedValue(new Response(new Blob(["image"]), { status: 200 })),
    } as unknown as CortexApi;

    render(<ImageTransformPanel api={api} available />);
    await user.click(screen.getByRole("button", { name: "Transform image" }));
    const image = new File([new Uint8Array([137, 80, 78, 71])], "photo.png", { type: "image/png" });
    await user.upload(screen.getByLabelText("Image file"), image);
    await user.click(screen.getAllByRole("button", { name: "Transform image" })[1]);

    expect(await screen.findByRole("status")).toHaveTextContent("Preparing image");
    finishStage?.({ artifact_id: "source-artifact" });
    await waitFor(() => expect(screen.getByRole("link", { name: "Download result" })).toBeVisible());
    expect(api.startRecipeImageTransform).toHaveBeenCalledWith(expect.objectContaining({
      source_artifact_id: "source-artifact",
      plan: expect.objectContaining({ steps: [{ op: "grayscale" }] }),
    }));
    expect(createObjectURL).toHaveBeenCalledTimes(2);
  });
});
