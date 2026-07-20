import { useState } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
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
}: {
  initialValue?: string;
  phase?: ComposerPhase;
  onSubmit?: () => Promise<boolean>;
  onStop?: () => void | Promise<void>;
  error?: string | null;
}) {
  const [value, setValue] = useState(initialValue);
  return (
    <MessageComposer
      value={value}
      phase={phase}
      selectedModel="local-chat:7b"
      localModels={["local-chat:7b", "local-chat:13b"]}
      error={error}
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
});
