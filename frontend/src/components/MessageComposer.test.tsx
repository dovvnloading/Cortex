import { useState } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ChatAttachment, CodeExecutionRequest } from "../../../contracts/cortex-api";
import { MessageComposer, type ComposerPhase } from "./MessageComposer";

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function ComposerHarness({
  initialValue = "",
  phase = "ready",
  onSubmit = vi.fn<() => Promise<boolean>>().mockResolvedValue(true),
  onStop = vi.fn(),
  error = null,
  attachments = [],
  onAddAttachments,
  onRemoveAttachment,
  imageInputBlocked = null,
  codeExecutionAvailable = false,
  onRunCode,
}: {
  initialValue?: string;
  phase?: ComposerPhase;
  onSubmit?: () => Promise<boolean>;
  onStop?: () => void | Promise<void>;
  error?: string | null;
  attachments?: readonly ChatAttachment[];
  onAddAttachments?: (files: File[]) => Promise<void> | void;
  onRemoveAttachment?: (attachmentId: string) => void;
  imageInputBlocked?: string | null;
  codeExecutionAvailable?: boolean;
  onRunCode?: (payload: CodeExecutionRequest) => Promise<void>;
}) {
  const [value, setValue] = useState(initialValue);
  return (
    <MessageComposer
      value={value}
      phase={phase}
      selectedModel="local-chat:7b"
      localModels={["local-chat:7b", "local-chat:13b"]}
      error={error}
      attachments={attachments}
      onAddAttachments={onAddAttachments}
      onRemoveAttachment={onRemoveAttachment}
      imageInputBlocked={imageInputBlocked}
      codeExecutionAvailable={codeExecutionAvailable}
      onRunCode={onRunCode}
      onValueChange={setValue}
      onSubmit={async () => {
        const accepted = await onSubmit();
        if (accepted) setValue("");
        return accepted;
      }}
      onStop={onStop}
      onSelectModel={vi.fn().mockResolvedValue(true)}
    />
  );
}

describe("MessageComposer", () => {
  it("keeps the utility row quiet and model-focused", () => {
    render(<ComposerHarness />);

    expect(screen.queryByText("LOCAL ENGINE")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Selected local model: local-chat:7b" })).toBeVisible();
  });

  it("submits once with Enter and clears only after acceptance", async () => {
    const user = userEvent.setup();
    const request = deferred<boolean>();
    const onSubmit = vi.fn(() => request.promise);
    render(<ComposerHarness onSubmit={onSubmit} />);

    const composer = screen.getByLabelText("Message Cortex");
    await user.type(composer, "Keep this draft safe");
    await user.keyboard("{Enter}");

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(composer).toHaveValue("Keep this draft safe");

    request.resolve(true);
    await waitFor(() => expect(composer).toHaveValue(""));
  });

  it("does not submit on Shift+Enter or during IME composition", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn<() => Promise<boolean>>().mockResolvedValue(true);
    render(<ComposerHarness initialValue="Message" onSubmit={onSubmit} />);

    const composer = screen.getByLabelText("Message Cortex");
    composer.focus();
    await user.keyboard("{Shift>}{Enter}{/Shift}");
    expect(onSubmit).not.toHaveBeenCalled();

    fireEvent.compositionStart(composer);
    fireEvent.keyDown(composer, { key: "Enter" });
    expect(onSubmit).not.toHaveBeenCalled();

    fireEvent.compositionEnd(composer);
    fireEvent.keyDown(composer, { key: "Enter" });
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
  });

  it("synchronously blocks duplicate Enter and click submissions", async () => {
    const user = userEvent.setup();
    const request = deferred<boolean>();
    const onSubmit = vi.fn(() => request.promise);
    render(<ComposerHarness initialValue="One request" onSubmit={onSubmit} />);

    const composer = screen.getByLabelText("Message Cortex");
    fireEvent.keyDown(composer, { key: "Enter" });
    fireEvent.keyDown(composer, { key: "Enter" });
    await user.click(screen.getByRole("button", { name: "Send message" }));

    expect(onSubmit).toHaveBeenCalledTimes(1);
    request.resolve(true);
  });

  it("keeps the next draft editable while generating and exposes an idempotent stop action", async () => {
    const user = userEvent.setup();
    const stop = deferred<void>();
    const onStop = vi.fn(() => stop.promise);
    const onSubmit = vi.fn<() => Promise<boolean>>().mockResolvedValue(true);
    render(<ComposerHarness phase="generating" onStop={onStop} onSubmit={onSubmit} />);

    const composer = screen.getByLabelText("Message Cortex");
    expect(composer).toBeEnabled();
    await user.type(composer, "A follow-up draft");
    await user.keyboard("{Enter}");
    expect(onSubmit).not.toHaveBeenCalled();

    const stopButton = screen.getByRole("button", { name: "Stop generating" });
    await user.click(stopButton);
    await user.click(stopButton);
    expect(onStop).toHaveBeenCalledTimes(1);
    expect(composer).toHaveValue("A follow-up draft\n");
    stop.resolve();
  });

  it("keeps drafting available while the local runtime is unavailable", async () => {
    const user = userEvent.setup();
    render(<ComposerHarness phase="unavailable" />);

    const composer = screen.getByLabelText("Message Cortex");
    await user.type(composer, "Write while reconnecting");
    expect(composer).toHaveValue("Write while reconnecting");
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
  });

  it("keeps a request error beside the composer", () => {
    render(<ComposerHarness initialValue="Preserved" error="The response could not be started. Your message is still here." />);

    expect(screen.getByRole("alert")).toHaveTextContent("Your message is still here.");
    expect(screen.getByLabelText("Message Cortex")).toHaveValue("Preserved");
  });

  it("keeps the file picker inside the composer and forwards the selected file", async () => {
    const user = userEvent.setup();
    const onAddAttachments = vi.fn();
    render(<ComposerHarness onAddAttachments={onAddAttachments} />);

    const file = new File(["# Notes"], "notes.md", { type: "text/markdown" });
    await user.upload(screen.getByLabelText("Attach images or documents"), file);

    expect(onAddAttachments).toHaveBeenCalledWith([file]);
  });

  it("shows attachment chips and blocks send when the selected model cannot accept images", () => {
    const attachment: ChatAttachment = {
      attachment_id: "image-1",
      filename: "photo.png",
      mime_type: "image/png",
      size: 4,
      sha256: "a".repeat(64),
      kind: "image",
      expires_at: "2099-01-01T00:00:00Z",
    };
    render(
      <ComposerHarness
        attachments={[attachment]}
        imageInputBlocked="Selected model cannot accept images."
      />,
    );

    expect(screen.getByText("photo.png")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("cannot accept images");
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
  });

  it("opens an editor-first code workspace and submits an explicit approval request", async () => {
    const user = userEvent.setup();
    const onRunCode = vi.fn<(payload: CodeExecutionRequest) => Promise<void>>().mockResolvedValue(undefined);
    render(<ComposerHarness codeExecutionAvailable onRunCode={onRunCode} />);

    await user.click(screen.getByRole("button", { name: "Open code workspace" }));
    expect(screen.getByRole("region", { name: "Code workspace" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Review & request" })).toBeDisabled();
    expect(screen.getByText("Sandboxed by default")).toBeVisible();
    expect(screen.getByRole("checkbox", { name: "Files capability" })).not.toBeVisible();

    await user.type(screen.getByRole("textbox", { name: "Python source" }), "print('hello')");
    await user.type(screen.getByRole("textbox", { name: "Run intent" }), "Print a greeting");

    await user.click(screen.getByText("Permissions"));
    expect(screen.getByRole("checkbox", { name: "Files capability" })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Processes capability" })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Network capability" })).not.toBeChecked();

    await user.click(screen.getByRole("checkbox", { name: "Files capability" }));
    expect(screen.getByRole("note")).toHaveTextContent("access your machine");
    await user.click(screen.getByRole("button", { name: "Review & request" }));

    await waitFor(() => expect(onRunCode).toHaveBeenCalledTimes(1));
    expect(onRunCode.mock.calls[0][0]).toMatchObject({
      language: "python",
      source: "print('hello')",
      intent_summary: "Print a greeting",
      capabilities: { filesystem: true, process: false, network: false },
    });
    expect(screen.queryByRole("region", { name: "Code workspace" })).not.toBeInTheDocument();
  });

  it("keeps editor navigation inside the source field", async () => {
    render(<ComposerHarness codeExecutionAvailable onRunCode={vi.fn().mockResolvedValue(undefined)} />);

    await userEvent.setup().click(screen.getByRole("button", { name: "Open code workspace" }));
    const source = screen.getByRole("textbox", { name: "Python source" });
    await userEvent.setup().type(source, "if ready:");
    fireEvent.keyDown(source, { key: "Tab" });

    await waitFor(() => expect(source).toHaveValue("if ready:    "));
    expect(source).toHaveFocus();
  });
});
