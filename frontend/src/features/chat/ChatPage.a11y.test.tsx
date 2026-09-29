import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ComponentProps } from "react";
import type { ChatResponse } from "../../../../contracts/cortex-api";
import { CortexApi } from "../../api/client";
import { useChatStore } from "../../stores/useChatStore";
import { ToastProvider } from "../../app/ToastProvider";
import { useUiStore } from "../../stores/useUiStore";
import { CommandPalette } from "../command-palette/CommandPalette";
import { ShortcutsHelpDialog } from "../command-palette/ShortcutsHelpDialog";
import { ChatPage } from "./ChatPage";
import { GenerationStreamHost } from "./GenerationStreamHost";

/**
 * Keyboard and screen-reader behaviour of the chat page while a response runs:
 * Escape from anywhere on the page, and the live region that says when a
 * response started, finished, stopped, or failed.
 */

function ChatWithHost(props: ComponentProps<typeof ChatPage>) {
  return (
    <>
      <GenerationStreamHost api={props.api} onSessionExpired={() => undefined} />
      <ChatPage {...props} />
    </>
  );
}

const emptyChat = (id: string): ChatResponse => ({
  id,
  title: "New Chat",
  timestamp: "2026-01-01T00:00:00Z",
  revision: 0,
  messages: [],
});

type Harness = {
  api: CortexApi;
  cancelGeneration: ReturnType<typeof vi.fn>;
  /** Deliver one stream event to the running job, then let the stream end. */
  finishWith: (event: string, data: Record<string, unknown>, savedChat?: ChatResponse) => Promise<void>;
};

/** A chat whose one job stays open until the test finishes it. */
function harness(): Harness {
  let emit: ((event: unknown) => void) | null = null;
  let resolveStream: (() => void) | null = null;
  let saved: ChatResponse = emptyChat("thread-a");
  const cancelGeneration = vi.fn(async () => ({ job_id: "job-1", kind: "generation", status: "cancelling", sequence: 2 }));
  const api = {
    chat: vi.fn(async () => saved),
    generate: vi.fn().mockResolvedValue({
      job_id: "job-1",
      kind: "generation",
      status: "queued",
      thread_id: "thread-a",
      user_message_id: "user-1",
    }),
    regenerate: vi.fn(),
    streamGeneration: vi.fn((_jobId, onEvent, options: { signal?: AbortSignal } = {}) => {
      emit = onEvent as (event: unknown) => void;
      return new Promise<void>((resolve, reject) => {
        options.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
        resolveStream = resolve;
      });
    }),
    generationStatus: vi.fn(),
    cancelGeneration,
    forkChat: vi.fn(),
    stageChatAttachment: vi.fn(),
  } as unknown as CortexApi;
  return {
    api,
    cancelGeneration,
    finishWith: async (event, data, savedChat) => {
      await waitFor(() => expect(emit).not.toBeNull());
      if (savedChat) saved = savedChat;
      await act(async () => {
        emit!({ event_id: 1, event, job_id: "job-1", thread_id: "thread-a", data });
        resolveStream?.();
      });
      await waitFor(() => expect(useChatStore.getState().generation.jobId).toBeNull());
    },
  };
}

function renderChat(api: CortexApi) {
  return render(
    <ChatWithHost
      api={api}
      threadId="thread-a"
      runtimeReady
      runtimeMessage={null}
      localModels={["local-chat:7b"]}
      selectedModel="local-chat:7b"
      modelBusy={false}
      onSelectModel={async () => true}
      onRescanModels={async () => undefined}
      onThreadCreated={vi.fn()}
      onForked={vi.fn()}
    />,
  );
}

const announcer = () => document.querySelector('.sr-only[role="status"]');

async function sendPrompt(user: ReturnType<typeof userEvent.setup>) {
  await user.type(await screen.findByLabelText("Message Cortex"), "Tell me something");
  await user.click(screen.getByRole("button", { name: "Send message" }));
  await screen.findByRole("button", { name: "Stop generating" });
}

/**
 * Move focus off the composer, as it is for someone reading the answer.
 * Sending hands focus back to the composer on the next frame, so wait for that
 * first; blurring earlier would just be undone.
 */
async function focusThePage() {
  const composer = screen.getByLabelText("Message Cortex");
  await waitFor(() => expect(document.activeElement).toBe(composer));
  composer.blur();
  expect(document.activeElement).toBe(document.body);
}

const savedAnswer = (stats: Record<string, unknown>): ChatResponse => ({
  ...emptyChat("thread-a"),
  revision: 2,
  messages: [
    { id: "user-1", role: "user", content: "Tell me something" },
    { id: "assistant-1", role: "assistant", content: "Something.", stats },
  ],
});

afterEach(() => {
  document.body.querySelectorAll("[data-test-overlay]").forEach((node) => node.remove());
  window.sessionStorage.clear();
});

describe("ChatPage Escape", () => {
  it("stops the response when Escape is pressed with focus anywhere on the page", async () => {
    const user = userEvent.setup();
    const { api, cancelGeneration } = harness();
    renderChat(api);
    await sendPrompt(user);
    await focusThePage();

    await user.keyboard("{Escape}");

    await waitFor(() => expect(cancelGeneration).toHaveBeenCalledWith("job-1"));
    expect(cancelGeneration).toHaveBeenCalledTimes(1);
  });

  it("still stops from the composer, and only once", async () => {
    const user = userEvent.setup();
    const { api, cancelGeneration } = harness();
    renderChat(api);
    await sendPrompt(user);
    screen.getByLabelText("Message Cortex").focus();

    await user.keyboard("{Escape}");

    await waitFor(() => expect(cancelGeneration).toHaveBeenCalledWith("job-1"));
    // The composer handles it and prevents default; the page-level handler must not repeat it.
    expect(cancelGeneration).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["a dialog", "dialog"],
    ["an alert dialog", "alertdialog"],
    ["a menu", "menu"],
    ["a listbox", "listbox"],
  ])("leaves Escape to %s that is open", async (_name, role) => {
    const user = userEvent.setup();
    const { api, cancelGeneration } = harness();
    renderChat(api);
    await sendPrompt(user);
    const overlay = document.createElement("div");
    overlay.setAttribute("role", role);
    overlay.setAttribute("data-test-overlay", "");
    document.body.appendChild(overlay);
    await focusThePage();

    await user.keyboard("{Escape}");

    expect(cancelGeneration).not.toHaveBeenCalled();
  });

  it("leaves Escape to the real shortcuts dialog and the real command palette", async () => {
    // The synthetic roles above pin the rule; this pins that the app's own
    // Base UI dialog and its cmdk palette actually carry one of those roles.
    const user = userEvent.setup();
    const { api, cancelGeneration } = harness();
    render(
      <>
        <ChatWithHost
          api={api}
          threadId="thread-a"
          runtimeReady
          runtimeMessage={null}
          localModels={["local-chat:7b"]}
          selectedModel="local-chat:7b"
          modelBusy={false}
          onSelectModel={async () => true}
          onRescanModels={async () => undefined}
          onThreadCreated={vi.fn()}
          onForked={vi.fn()}
        />
        <ShortcutsHelpDialog />
        <CommandPalette
          chats={[]}
          localModels={[]}
          selectedModel={null}
          theme="dark"
          onNewChat={vi.fn()}
          onOpenSettings={vi.fn()}
          onToggleTheme={vi.fn()}
          onSelectModel={vi.fn()}
          onSelectChat={vi.fn()}
        />
      </>,
    );
    await sendPrompt(user);
    await focusThePage();

    act(() => useUiStore.getState().setShortcutsDialogOpen(true));
    expect(await screen.findByRole("dialog", { name: "Keyboard shortcuts" })).toBeVisible();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Keyboard shortcuts" })).not.toBeInTheDocument());
    expect(cancelGeneration).not.toHaveBeenCalled();

    act(() => useUiStore.getState().setCommandPaletteOpen(true));
    expect(await screen.findByRole("dialog", { name: "Command palette" })).toBeVisible();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Command palette" })).not.toBeInTheDocument());
    expect(cancelGeneration).not.toHaveBeenCalled();

    // With both closed, the same key now stops the response.
    (document.activeElement as HTMLElement | null)?.blur();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(cancelGeneration).toHaveBeenCalledTimes(1));
  });

  it("stops again once the overlay has closed", async () => {
    const user = userEvent.setup();
    const { api, cancelGeneration } = harness();
    renderChat(api);
    await sendPrompt(user);
    const overlay = document.createElement("div");
    overlay.setAttribute("role", "dialog");
    document.body.appendChild(overlay);
    await focusThePage();
    await user.keyboard("{Escape}");
    expect(cancelGeneration).not.toHaveBeenCalled();

    overlay.remove();
    await user.keyboard("{Escape}");

    await waitFor(() => expect(cancelGeneration).toHaveBeenCalledTimes(1));
  });

  it("leaves Escape to another text field, where it cancels the edit", async () => {
    const user = userEvent.setup();
    const { api, cancelGeneration } = harness();
    renderChat(api);
    await sendPrompt(user);
    // Sending hands focus back to the composer on the next frame; focusing the
    // field before that would just be undone (see focusThePage).
    await waitFor(() => expect(document.activeElement).toBe(screen.getByLabelText("Message Cortex")));
    const field = document.createElement("input");
    field.setAttribute("data-test-overlay", "");
    document.body.appendChild(field);
    field.focus();

    await user.keyboard("{Escape}");

    expect(cancelGeneration).not.toHaveBeenCalled();
  });

  it("does nothing while no response is running", async () => {
    const user = userEvent.setup();
    const { api, cancelGeneration } = harness();
    renderChat(api);
    await screen.findByLabelText("Message Cortex");
    (document.activeElement as HTMLElement | null)?.blur();

    await user.keyboard("{Escape}");

    expect(cancelGeneration).not.toHaveBeenCalled();
  });

  it("ignores Escape with a modifier held", async () => {
    const user = userEvent.setup();
    const { api, cancelGeneration } = harness();
    renderChat(api);
    await sendPrompt(user);
    await focusThePage();

    await user.keyboard("{Shift>}{Escape}{/Shift}");

    expect(cancelGeneration).not.toHaveBeenCalled();
  });
});

describe("ChatPage response announcements", () => {
  it("is a polite status region, hidden from sight, and empty until something happens", async () => {
    const { api } = harness();
    renderChat(api);
    await screen.findByLabelText("Message Cortex");

    const region = announcer();
    expect(region).not.toBeNull();
    expect(region).toHaveAttribute("aria-live", "polite");
    expect(region).toHaveClass("sr-only");
    expect(region).toBeEmptyDOMElement();
  });

  it("says a response started, then how long the finished one was", async () => {
    const user = userEvent.setup();
    const { api, finishWith } = harness();
    renderChat(api);
    await sendPrompt(user);
    await waitFor(() => expect(announcer()).toHaveTextContent("Response started"));

    await finishWith("generation.completed", { assistant_message_id: "assistant-1" }, savedAnswer({ eval_count: 1234 }));

    await waitFor(() => expect(announcer()).toHaveTextContent("Response complete, 1,234 tokens"));
  });

  it("says a response is complete without a count when none is known", async () => {
    const user = userEvent.setup();
    const { api, finishWith } = harness();
    renderChat(api);
    await sendPrompt(user);

    await finishWith("generation.completed", { assistant_message_id: "assistant-1" }, savedAnswer({}));

    await waitFor(() => expect(announcer()).toHaveTextContent(/^Response complete$/));
  });

  it("says a response stopped after the person stopped it and the answer was kept", async () => {
    const user = userEvent.setup();
    const { api, finishWith } = harness();
    renderChat(api);
    await sendPrompt(user);
    await user.click(screen.getByRole("button", { name: "Stop generating" }));
    await screen.findByRole("button", { name: "Stopping response" });

    await finishWith(
      "generation.cancelled",
      { message: "Job cancelled.", assistant_message_id: "assistant-1" },
      savedAnswer({ stopped: true, eval_count: 9 }),
    );

    await waitFor(() => expect(announcer()).toHaveTextContent(/^Response stopped$/));
  });

  it("says a response stopped when there was nothing to keep, not that it failed", async () => {
    const user = userEvent.setup();
    const { api, finishWith } = harness();
    renderChat(api);
    await sendPrompt(user);
    await user.click(screen.getByRole("button", { name: "Stop generating" }));
    await screen.findByRole("button", { name: "Stopping response" });

    await finishWith("generation.cancelled", { message: "Job cancelled." });

    await waitFor(() => expect(announcer()).toHaveTextContent(/^Response stopped$/));
  });

  it("says a response stopped after Escape from the page", async () => {
    const user = userEvent.setup();
    const { api, cancelGeneration, finishWith } = harness();
    renderChat(api);
    await sendPrompt(user);
    await focusThePage();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(cancelGeneration).toHaveBeenCalledTimes(1));

    await finishWith(
      "generation.cancelled",
      { message: "Job cancelled.", assistant_message_id: "assistant-1" },
      savedAnswer({ stopped: true }),
    );

    await waitFor(() => expect(announcer()).toHaveTextContent(/^Response stopped$/));
  });

  it("says a response failed, briefly, leaving the reason to the composer's alert", async () => {
    const user = userEvent.setup();
    const { api, finishWith } = harness();
    renderChat(api);
    await sendPrompt(user);

    await finishWith("generation.failed", { message: "The model failed." });

    await waitFor(() => expect(announcer()).toHaveTextContent(/^Response failed$/));
    expect(await screen.findByRole("alert")).toHaveTextContent("The model failed.");
  });

  it("does not announce every streamed token", async () => {
    const user = userEvent.setup();
    const { api } = harness();
    renderChat(api);
    await sendPrompt(user);
    await waitFor(() => expect(announcer()).toHaveTextContent("Response started"));
    const seen: string[] = [];
    const observer = new MutationObserver(() => seen.push(announcer()?.textContent ?? ""));
    observer.observe(announcer()!, { childList: true, characterData: true, subtree: true });

    act(() => {
      for (let token = 0; token < 30; token += 1) useChatStore.getState().appendContentToken("job-1", "word ");
    });
    await act(async () => { await Promise.resolve(); });
    observer.disconnect();

    expect(seen).toEqual([]);
    expect(announcer()).toHaveTextContent(/^Response started$/);
  });

  it("does not read out an outcome that was already waiting when the page opened", async () => {
    useChatStore.getState().recordFailure({ threadId: "thread-a", message: "Earlier failure." });
    const { api } = harness();
    renderChat(api);
    await screen.findByLabelText("Message Cortex");

    expect(announcer()).toBeEmptyDOMElement();
  });

  it("does not announce another thread's outcome", async () => {
    const { api } = harness();
    renderChat(api);
    await screen.findByLabelText("Message Cortex");

    act(() => useChatStore.getState().recordFailure({ threadId: "thread-b", message: "Elsewhere." }));

    expect(announcer()).toBeEmptyDOMElement();
  });
});

describe("ChatPage beside the app's toasts", () => {
  afterEach(() => {
    act(() => {
      for (const toast of useUiStore.getState().toasts) useUiStore.getState().dismissToast(toast.id);
    });
  });

  function renderChatWithToasts(api: CortexApi) {
    return render(
      <ToastProvider>
        <ChatWithHost
          api={api}
          threadId="thread-a"
          runtimeReady
          runtimeMessage={null}
          localModels={["local-chat:7b"]}
          selectedModel="local-chat:7b"
          modelBusy={false}
          onSelectModel={async () => true}
          onRescanModels={async () => undefined}
          onThreadCreated={vi.fn()}
          onForked={vi.fn()}
        />
      </ToastProvider>,
    );
  }

  it("leaves Escape to the Undo button of a toast that has focus, and stops the response from anywhere else", async () => {
    const user = userEvent.setup();
    const { api, cancelGeneration } = harness();
    renderChatWithToasts(api);
    await sendPrompt(user);
    await focusThePage();
    const undo = vi.fn();
    act(() => { useUiStore.getState().notify("Chat deleted.", "success", { action: { label: "Undo", onAction: undo }, durationMs: 60_000 }); });
    const undoButton = await screen.findByRole("button", { name: "Undo" });
    undoButton.focus();
    expect(document.activeElement).toBe(undoButton);

    await user.keyboard("{Escape}");

    // The response keeps running, and the toast (and its way back) is untouched.
    expect(cancelGeneration).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Undo" })).toBeInTheDocument();
    expect(undo).not.toHaveBeenCalled();

    undoButton.blur();
    await user.keyboard("{Escape}");

    await waitFor(() => expect(cancelGeneration).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("button", { name: "Undo" })).toBeInTheDocument();
  });

  it("keeps the response announcer and an error toast to one reading each", async () => {
    const user = userEvent.setup();
    const { api, finishWith } = harness();
    renderChatWithToasts(api);
    await sendPrompt(user);
    await waitFor(() => expect(announcer()).toHaveTextContent(/^Response started$/));

    act(() => { useUiStore.getState().notify("Could not save memory.", "error"); });

    // The toast is its own alert; the announcer neither repeats it nor changes.
    const toastAlert = await screen.findByRole("alert");
    expect(toastAlert).toHaveTextContent("Could not save memory.");
    expect(announcer()).toHaveTextContent(/^Response started$/);

    await finishWith("generation.failed", { message: "The model failed." });

    // The failure is said briefly by the announcer and in full by the composer's alert;
    // the toast is not read again, and the announcer is a polite status, never an alert.
    await waitFor(() => expect(announcer()).toHaveTextContent(/^Response failed$/));
    const alerts = await screen.findAllByRole("alert");
    expect(alerts).toHaveLength(2);
    expect(alerts.filter((alert) => alert.textContent?.includes("Could not save memory."))).toHaveLength(1);
    expect(alerts.filter((alert) => alert.textContent?.includes("The model failed."))).toHaveLength(1);
    expect(alerts).not.toContain(announcer());
    expect(screen.getAllByText("Could not save memory.")).toHaveLength(1);
    expect(screen.getAllByText(/Response failed/)).toHaveLength(1);
  });

  it("does not read a status toast into the announcer either", async () => {
    const user = userEvent.setup();
    const { api, finishWith } = harness();
    renderChatWithToasts(api);
    await sendPrompt(user);

    await finishWith("generation.completed", { assistant_message_id: "assistant-1" }, savedAnswer({ eval_count: 5 }));
    act(() => { useUiStore.getState().notify("Memory saved.", "success"); });

    await waitFor(() => expect(announcer()).toHaveTextContent(/^Response complete, 5 tokens$/));
    expect(screen.getAllByText("Memory saved.")).toHaveLength(1);
  });
});
