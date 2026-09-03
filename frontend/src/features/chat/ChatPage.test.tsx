import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useState } from "react";
import type { ChatAttachment, ChatResponse } from "../../../../contracts/cortex-api";
import { ApiError, CortexApi } from "../../api/client";
import { humanizeGenerationStatus } from "../../lib/generationStatus";
import { NEW_THREAD_OPTIONS_KEY, useChatStore } from "../../stores/useChatStore";
import { ChatPage } from "./ChatPage";

describe("humanizeGenerationStatus", () => {
  it("never exposes an internal all-caps control marker", () => {
    expect(humanizeGenerationStatus("START_FINAL_ANIMATION")).toBe("Generating a response...");
  });

  it("keeps useful human-facing progress text", () => {
    expect(humanizeGenerationStatus("Analyzing the request...")).toBe("Analyzing the request...");
  });
});

const emptyChat = (id: string): ChatResponse => ({
  id,
  title: "New Chat",
  timestamp: "2026-01-01T00:00:00Z",
  revision: 0,
  messages: [],
});

function chatApi(overrides: Partial<CortexApi> = {}): CortexApi {
  return {
    chat: vi.fn(async (id: string) => emptyChat(id)),
    generate: vi.fn(),
    regenerate: vi.fn(),
    streamGeneration: vi.fn((_jobId, _onEvent, options: { signal?: AbortSignal } = {}) => new Promise<void>((_resolve, reject) => {
      options.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
    })),
    generationStatus: vi.fn(),
    cancelGeneration: vi.fn(async () => ({ job_id: "job-1", kind: "generation", status: "cancelling", sequence: 2 })),
    forkChat: vi.fn(),
    stageChatAttachment: vi.fn(),
    ...overrides,
  } as unknown as CortexApi;
}

function renderChat(api: CortexApi, threadId = "thread-a", selectedModelSupportsVision: boolean | null = null, onClearMemory?: () => Promise<void>) {
  return render(
    <ChatPage
      api={api}
      threadId={threadId}
      runtimeReady
      runtimeMessage={null}
      localModels={["local-chat:7b"]}
      selectedModel="local-chat:7b"
      selectedModelSupportsVision={selectedModelSupportsVision}
      modelBusy={false}
      onSelectModel={async () => true}
      onRescanModels={async () => undefined}
      onThreadCreated={vi.fn()}
      onChatChanged={vi.fn()}
      onForked={vi.fn()}
      onClearMemory={onClearMemory}
      onSessionExpired={vi.fn()}
    />,
  );
}

describe("ChatPage composer integration", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.sessionStorage.clear();
    useChatStore.setState({ generationOptionsByThread: {} });
  });

  it("keeps a blank conversation focused on the composer", async () => {
    renderChat(chatApi());

    await screen.findByLabelText("Message Cortex");

    expect(screen.queryByRole("heading", { name: "New thread" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Message Cortex")).toHaveValue("");
  });

  it("asks before clearing memories requested by a completed generation", async () => {
    const user = userEvent.setup();
    const clearMemory = vi.fn<() => Promise<void>>().mockResolvedValue();
    // Regression guard for the blocking-dialog bug: window.confirm() must
    // never be invoked -- the confirmation now runs through the app's own
    // async dialog instead of the synchronous, main-thread-freezing native one.
    const confirm = vi.spyOn(window, "confirm");
    let emit: ((event: unknown) => void) | null = null;
    let resolveStream: (() => void) | null = null;
    const api = chatApi({
      generate: vi.fn().mockResolvedValue({
        job_id: "job-clear-ui",
        kind: "generation",
        status: "queued",
        thread_id: "thread-a",
        user_message_id: "message-clear-ui",
      }),
      streamGeneration: vi.fn((_jobId, onEvent, options: { signal?: AbortSignal } = {}) => {
        emit = onEvent as (event: unknown) => void;
        return new Promise<void>((resolve, reject) => {
          options.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
          resolveStream = resolve;
        });
      }),
    });
    renderChat(api, "thread-a", null, clearMemory);

    await user.type(await screen.findByLabelText("Message Cortex"), "Please clear memories");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(emit).not.toBeNull());
    await act(async () => {
      emit!({
        event_id: 1,
        event: "generation.completed",
        job_id: "job-clear-ui",
        thread_id: "thread-a",
        data: { clear_requested: true },
      });
      resolveStream?.();
    });

    expect(await screen.findByRole("heading", { name: "Clear permanent memories?" })).toBeVisible();
    expect(screen.getByText("Cortex requested clearing all permanent memories. Clear them now? This cannot be undone.")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Clear memories" }));

    await waitFor(() => expect(clearMemory).toHaveBeenCalledTimes(1));
    expect(confirm).not.toHaveBeenCalled();
  });

  it("does not clear memories when the user declines a generation proposal", async () => {
    const user = userEvent.setup();
    const clearMemory = vi.fn<() => Promise<void>>().mockResolvedValue();
    // Same regression guard as the confirm-path test above.
    const confirm = vi.spyOn(window, "confirm");
    let emit: ((event: unknown) => void) | null = null;
    let resolveStream: (() => void) | null = null;
    const api = chatApi({
      generate: vi.fn().mockResolvedValue({
        job_id: "job-clear-decline",
        kind: "generation",
        status: "queued",
        thread_id: "thread-a",
        user_message_id: "message-clear-decline",
      }),
      streamGeneration: vi.fn((_jobId, onEvent, options: { signal?: AbortSignal } = {}) => {
        emit = onEvent as (event: unknown) => void;
        return new Promise<void>((resolve, reject) => {
          options.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
          resolveStream = resolve;
        });
      }),
    });
    renderChat(api, "thread-a", null, clearMemory);

    await user.type(await screen.findByLabelText("Message Cortex"), "Please clear memories");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(emit).not.toBeNull());
    await act(async () => {
      emit!({
        event_id: 1,
        event: "generation.completed",
        job_id: "job-clear-decline",
        thread_id: "thread-a",
        data: { clear_requested: true },
      });
      resolveStream?.();
    });

    await waitFor(() => expect(useChatStore.getState().generation.jobId).toBeNull());
    await user.click(await screen.findByRole("button", { name: "Cancel" }));

    expect(clearMemory).not.toHaveBeenCalled();
    expect(confirm).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole("heading", { name: "Clear permanent memories?" })).not.toBeInTheDocument());
  });

  it("renders role-aware bubbles with markdown, reasoning, and sources", async () => {
    const transcript: ChatResponse = {
      id: "thread-a",
      title: "Workbench",
      timestamp: "2026-01-01T00:00:00Z",
      revision: 2,
      messages: [
        { id: "m-1", role: "user", content: "Show me the result.", thoughts: "This must never render in the user bubble." },
        {
          id: "m-2",
          role: "assistant",
          content: "# Result\n\n```ts\nconst answer = 42;\n```",
          thoughts: "I checked the input first.",
          sources: ["[Reference](https://example.com/reference)"],
          attachments: [{ attachment_id: "doc-1", filename: "result.md", mime_type: "text/markdown", size: 12, sha256: "a".repeat(64), kind: "document", expires_at: "2099-01-01T00:00:00Z" }],
          timestamp: "2026-01-01T00:05:00Z",
        },
      ],
    };
    const api = chatApi({
      chat: vi.fn(async () => transcript),
    });
    renderChat(api);

    // The speaker is carried by form and by each article's accessible name,
    // not by a repeated visible nameplate above every turn.
    expect(await screen.findByLabelText("Cortex message")).toBeInTheDocument();
    expect(screen.getByLabelText("Your message")).toBeInTheDocument();
    expect(screen.queryByText("Cortex")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Result", level: 1 })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy ts code" })).toBeInTheDocument();
    expect(screen.getByText("Reasoning")).toBeInTheDocument();
    expect(screen.getByText("Reasoning").closest("details")?.parentElement).toHaveClass("message-card");
    expect(screen.queryByText("This must never render in the user bubble.")).not.toBeInTheDocument();
    expect(screen.getByText("Sources")).toBeInTheDocument();
    expect(screen.getByText("result.md")).toBeInTheDocument();
  });

  it("explains an empty answer next to its reasoning instead of showing a blank bubble", async () => {
    const transcript: ChatResponse = {
      id: "thread-a",
      title: "Workbench",
      timestamp: "2026-01-01T00:00:00Z",
      revision: 1,
      messages: [
        { id: "m-1", role: "user", content: "Explain the halting problem." },
        { id: "m-2", role: "assistant", content: "", thoughts: "Still working through the reduction..." },
      ],
    };
    const api = chatApi({ chat: vi.fn(async () => transcript) });
    renderChat(api);

    expect(await screen.findByText("Reasoning")).toBeInTheDocument();
    expect(screen.getByText("Cortex ran out of room to finish this answer -- see its reasoning below.")).toBeInTheDocument();
  });

  it("never renders the previous transcript when the current route fails to load", async () => {
    const alpha: ChatResponse = {
      id: "thread-a",
      title: "Alpha",
      timestamp: "2026-01-01T00:00:00Z",
      revision: 2,
      messages: [
        { id: "m-1", role: "user", content: "Alpha question" },
        { id: "m-2", role: "assistant", content: "Alpha answer" },
      ],
    };
    const api = chatApi({
      chat: vi.fn(async (id: string) => {
        if (id === "thread-a") return alpha;
        throw new ApiError(404, "Chat not found.");
      }),
    });
    const view = renderChat(api, "thread-a");
    expect(await screen.findByText("Alpha answer")).toBeInTheDocument();

    view.rerender(
      <ChatPage
        api={api}
        threadId="thread-b"
        runtimeReady
        runtimeMessage={null}
        localModels={["local-chat:7b"]}
        selectedModel="local-chat:7b"
        modelBusy={false}
        onSelectModel={async () => true}
        onRescanModels={async () => undefined}
        onThreadCreated={vi.fn()}
        onChatChanged={vi.fn()}
        onForked={vi.fn()}
        onSessionExpired={vi.fn()}
      />,
    );

    expect(
      await screen.findByRole("heading", { name: "Conversation unavailable" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Chat not found.")).toBeInTheDocument();
    expect(screen.queryByText("Alpha answer")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Message Cortex")).not.toBeInTheDocument();
  });

  it("ignores an older request when the same route is loaded again", async () => {
    const pending: Record<string, Array<(value: ChatResponse) => void>> = {};
    const api = chatApi({
      chat: vi.fn((id: string) => new Promise<ChatResponse>((resolve) => {
        (pending[id] ??= []).push(resolve);
      })),
    });
    const view = renderChat(api, "thread-a");
    await waitFor(() => expect(api.chat).toHaveBeenCalledWith("thread-a"));

    view.rerender(
      <ChatPage
        api={api}
        threadId="thread-b"
        runtimeReady
        runtimeMessage={null}
        localModels={["local-chat:7b"]}
        selectedModel="local-chat:7b"
        modelBusy={false}
        onSelectModel={async () => true}
        onRescanModels={async () => undefined}
        onThreadCreated={vi.fn()}
        onChatChanged={vi.fn()}
        onForked={vi.fn()}
        onSessionExpired={vi.fn()}
      />,
    );
    await waitFor(() => expect(api.chat).toHaveBeenCalledWith("thread-b"));
    view.rerender(
      <ChatPage
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
        onChatChanged={vi.fn()}
        onForked={vi.fn()}
        onSessionExpired={vi.fn()}
      />,
    );
    await waitFor(() => expect(api.chat).toHaveBeenCalledTimes(3));

    pending["thread-a"][1]({ ...emptyChat("thread-a"), title: "Newest Alpha", messages: [{ id: "new", role: "assistant", content: "Newest Alpha response" }] });
    expect(await screen.findByText("Newest Alpha response")).toBeInTheDocument();
    pending["thread-a"][0]({ ...emptyChat("thread-a"), title: "Old Alpha", messages: [{ id: "old", role: "assistant", content: "Old Alpha response" }] });

    await waitFor(() => expect(screen.getByText("Newest Alpha response")).toBeInTheDocument());
    expect(screen.queryByText("Old Alpha response")).not.toBeInTheDocument();
  });

  it("keeps the accepted new-chat turn visible while its refresh is pending", async () => {
    const user = userEvent.setup();
    const accepted = { job_id: "job-new", kind: "generation" as const, status: "queued" as const, thread_id: "thread-new", user_message_id: "message-new" };
    const api = chatApi({
      chat: vi.fn(() => new Promise<ChatResponse>(() => undefined)),
      generate: vi.fn().mockResolvedValue(accepted),
    });
    function RoutedChat() {
      const [threadId, setThreadId] = useState<string | null>(null);
      return (
        <ChatPage
          api={api}
          threadId={threadId}
          runtimeReady
          runtimeMessage={null}
          localModels={["local-chat:7b"]}
          selectedModel="local-chat:7b"
          modelBusy={false}
          onSelectModel={async () => true}
          onRescanModels={async () => undefined}
          onThreadCreated={setThreadId}
          onChatChanged={vi.fn()}
          onForked={vi.fn()}
          onSessionExpired={vi.fn()}
        />
      );
    }
    render(<RoutedChat />);
    const composer = await screen.findByLabelText("Message Cortex");
    await user.type(composer, "Accepted immediately");
    await user.click(screen.getByRole("button", { name: "Send message" }));

    expect(await screen.findByText("Accepted immediately")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop generating" })).toBeInTheDocument();
  });

  it("moves drafts created during new-chat acceptance into the accepted thread", async () => {
    const user = userEvent.setup();
    const lateAttachment: ChatAttachment = {
      attachment_id: "late-doc",
      filename: "next-turn.md",
      mime_type: "text/markdown",
      size: 9,
      sha256: "1".repeat(64),
      kind: "document",
      expires_at: "2099-01-01T00:00:00Z",
    };
    let accept!: (value: { job_id: string; kind: "generation"; status: "queued"; thread_id: string; user_message_id: string }) => void;
    const accepted = new Promise<{ job_id: string; kind: "generation"; status: "queued"; thread_id: string; user_message_id: string }>((resolve) => { accept = resolve; });
    const api = chatApi({
      chat: vi.fn(async (id: string) => emptyChat(id)),
      generate: vi.fn(() => accepted),
      stageChatAttachment: vi.fn().mockResolvedValue(lateAttachment),
    });
    function RoutedChat() {
      const [threadId, setThreadId] = useState<string | null>(null);
      return (
        <ChatPage
          api={api}
          threadId={threadId}
          runtimeReady
          runtimeMessage={null}
          localModels={["local-chat:7b"]}
          selectedModel="local-chat:7b"
          modelBusy={false}
          onSelectModel={async () => true}
          onRescanModels={async () => undefined}
          onThreadCreated={setThreadId}
          onChatChanged={vi.fn()}
          onForked={vi.fn()}
          onSessionExpired={vi.fn()}
        />
      );
    }
    render(<RoutedChat />);

    const composer = await screen.findByLabelText("Message Cortex");
    const attachmentInput = screen.getByLabelText("Attach images or documents");
    await user.type(composer, "First turn");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(api.generate).toHaveBeenCalledTimes(1));

    await user.clear(composer);
    await user.type(composer, "Draft for the next turn");
    await user.upload(attachmentInput, new File(["next turn"], "next-turn.md", { type: "text/markdown" }));
    expect(await screen.findByRole("button", { name: "Remove next-turn.md" })).toBeInTheDocument();

    accept({
      job_id: "job-new",
      kind: "generation",
      status: "queued",
      thread_id: "thread-new",
      user_message_id: "message-new",
    });

    await waitFor(() => expect(screen.getByLabelText("Message Cortex")).toHaveValue("Draft for the next turn"));
    expect(screen.getByRole("button", { name: "Remove next-turn.md" })).toBeInTheDocument();
    expect(window.sessionStorage.getItem("cortex.composer.draft.new")).toBeNull();
    expect(window.sessionStorage.getItem("cortex.composer.draft.thread-new")).toBe("Draft for the next turn");
    expect(window.sessionStorage.getItem("cortex.composer.attachments.new")).toBeNull();
    expect(JSON.parse(window.sessionStorage.getItem("cortex.composer.attachments.thread-new") ?? "[]")).toEqual([lateAttachment]);
  });

  it("migrates generation overrides set before the first message to the new chat's thread id", async () => {
    const user = userEvent.setup();
    const api = chatApi({
      chat: vi.fn(async (id: string) => emptyChat(id)),
      generate: vi.fn(async () => ({
        job_id: "job-new",
        kind: "generation" as const,
        status: "queued" as const,
        thread_id: "thread-new",
        user_message_id: "message-new",
      })),
    });
    function RoutedChat() {
      const [threadId, setThreadId] = useState<string | null>(null);
      return (
        <ChatPage
          api={api}
          threadId={threadId}
          runtimeReady
          runtimeMessage={null}
          localModels={["local-chat:7b"]}
          selectedModel="local-chat:7b"
          modelBusy={false}
          onSelectModel={async () => true}
          onRescanModels={async () => undefined}
          onThreadCreated={setThreadId}
          onChatChanged={vi.fn()}
          onForked={vi.fn()}
          onSessionExpired={vi.fn()}
        />
      );
    }
    render(<RoutedChat />);

    // Tune sampling before the thread exists -- it is stored under the
    // "new chat" placeholder key until a real thread id is assigned.
    useChatStore.getState().setThreadOptions(NEW_THREAD_OPTIONS_KEY, { temperature: 0.2 });

    const composer = await screen.findByLabelText("Message Cortex");
    await user.type(composer, "First turn");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(api.generate).toHaveBeenCalledTimes(1));

    await waitFor(() => expect(useChatStore.getState().generationOptionsByThread["thread-new"]).toEqual({ temperature: 0.2 }));
    expect(useChatStore.getState().generationOptionsByThread[NEW_THREAD_OPTIONS_KEY]).toBeUndefined();
  });

  it("retargets an in-flight new-chat attachment when acceptance wins the race", async () => {
    const user = userEvent.setup();
    const stagedAttachment: ChatAttachment = {
      attachment_id: "inverse-order-doc",
      filename: "after-acceptance.md",
      mime_type: "text/markdown",
      size: 16,
      sha256: "2".repeat(64),
      kind: "document",
      expires_at: "2099-01-01T00:00:00Z",
    };
    let accept!: (value: { job_id: string; kind: "generation"; status: "queued"; thread_id: string; user_message_id: string }) => void;
    let finishStaging!: (value: ChatAttachment) => void;
    const accepted = new Promise<{ job_id: string; kind: "generation"; status: "queued"; thread_id: string; user_message_id: string }>((resolve) => { accept = resolve; });
    const staging = new Promise<ChatAttachment>((resolve) => { finishStaging = resolve; });
    const api = chatApi({
      chat: vi.fn(async (id: string) => emptyChat(id)),
      generate: vi.fn(() => accepted),
      stageChatAttachment: vi.fn(() => staging),
    });
    function RoutedChat() {
      const [threadId, setThreadId] = useState<string | null>(null);
      return (
        <ChatPage
          api={api}
          threadId={threadId}
          runtimeReady
          runtimeMessage={null}
          localModels={["local-chat:7b"]}
          selectedModel="local-chat:7b"
          modelBusy={false}
          onSelectModel={async () => true}
          onRescanModels={async () => undefined}
          onThreadCreated={setThreadId}
          onChatChanged={vi.fn()}
          onForked={vi.fn()}
          onSessionExpired={vi.fn()}
        />
      );
    }
    render(<RoutedChat />);

    const composer = await screen.findByLabelText("Message Cortex");
    await user.type(composer, "First turn");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(api.generate).toHaveBeenCalledTimes(1));
    await user.upload(
      screen.getByLabelText("Attach images or documents"),
      new File(["after acceptance"], "after-acceptance.md", { type: "text/markdown" }),
    );
    await waitFor(() => expect(api.stageChatAttachment).toHaveBeenCalledTimes(1));

    accept({
      job_id: "job-inverse",
      kind: "generation",
      status: "queued",
      thread_id: "thread-inverse",
      user_message_id: "message-inverse",
    });
    await waitFor(() => expect(api.chat).toHaveBeenCalledWith("thread-inverse"));
    expect(window.sessionStorage.getItem("cortex.composer.attachments.new")).toBeNull();

    act(() => finishStaging(stagedAttachment));

    expect(await screen.findByRole("button", { name: "Remove after-acceptance.md" })).toBeInTheDocument();
    expect(window.sessionStorage.getItem("cortex.composer.attachments.new")).toBeNull();
    expect(JSON.parse(window.sessionStorage.getItem("cortex.composer.attachments.thread-inverse") ?? "[]")).toEqual([stagedAttachment]);
  });

  it("replays from the beginning on a cold start, then resumes without duplicating after a route remount", async () => {
    // Two different situations that both reach this mount effect:
    //
    //   Cold start (page reload): sessionStorage remembers the job but the
    //   module-level store was wiped, so the transcript must be rebuilt by
    //   replaying every event from 0.
    //
    //   Route remount (Settings and back): the page unmounts but the store
    //   survives with its accumulated text intact. Replaying from 0 here
    //   appends the whole answer onto itself -- the regression this pins.
    window.sessionStorage.setItem("cortex.active.generation", JSON.stringify({ jobId: "job-replay", threadId: "thread-a", lastEventId: 7 }));
    const streamCalls: Array<{ afterEventId?: number }> = [];
    const api = chatApi({
      streamGeneration: vi.fn((_jobId, onEvent, options: { signal?: AbortSignal; afterEventId?: number } = {}) => {
        streamCalls.push({ afterEventId: options.afterEventId });
        onEvent({ event_id: 8, event: "generation.content_delta", job_id: "job-replay", thread_id: "thread-a", data: { delta: "replayed" } });
        return new Promise<void>((_resolve, reject) => options.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true }));
      }),
    });
    const first = renderChat(api, "thread-a");
    await waitFor(() => expect(streamCalls).toHaveLength(1));
    await waitFor(() => expect(useChatStore.getState().generation.partialContent).toBe("replayed"));

    first.unmount();
    renderChat(api, "thread-a");
    await waitFor(() => expect(streamCalls).toHaveLength(2));

    // Cold start replayed from 0; the remount resumed from the persisted
    // cursor instead of rewinding.
    expect(streamCalls[0].afterEventId).toBe(0);
    expect(streamCalls[1].afterEventId).toBe(8);

    // And the answer is not printed twice.
    await waitFor(() => expect(useChatStore.getState().generation.partialContent).toBe("replayed"));
  });

  it("resumes the global job from its in-memory cursor when storage access is denied", async () => {
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("Storage access denied", "SecurityError");
    });
    const streamCalls: Array<{ afterEventId?: number }> = [];
    const api = chatApi({
      streamGeneration: vi.fn((_jobId, _onEvent, options: { signal?: AbortSignal; afterEventId?: number } = {}) => {
        streamCalls.push({ afterEventId: options.afterEventId });
        return new Promise<void>((_resolve, reject) => {
          options.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
        });
      }),
    });
    useChatStore.getState().beginGeneration("job-memory", "thread-a");
    useChatStore.getState().setGenerationCursor("job-memory", 4);

    try {
      const first = renderChat(api, "thread-a");
      await waitFor(() => expect(streamCalls).toHaveLength(1));
      first.unmount();

      renderChat(api, "thread-a");
      await waitFor(() => expect(streamCalls).toHaveLength(2));
      expect(streamCalls).toEqual([{ afterEventId: 4 }, { afterEventId: 4 }]);
      expect(useChatStore.getState().generation.jobId).toBe("job-memory");
    } finally {
      getItem.mockRestore();
    }
  });

  it("collapses the pending reasoning panel in step with contentReady, ahead of the swap to the real message", async () => {
    // The final MessageCard's reasoning panel defaults collapsed. If the
    // pending bubble's stayed forced-open the whole time, it would visibly
    // snap shut the moment the real message swaps in. Closing it here, at
    // the same instant the "Live" badge goes away, means both states
    // already agree by the time that swap happens.
    window.sessionStorage.setItem("cortex.active.generation", JSON.stringify({ jobId: "job-reason", threadId: "thread-a", lastEventId: 0 }));
    let emit: ((event: unknown) => void) | null = null;
    const api = chatApi({
      streamGeneration: vi.fn((_jobId, onEvent, options: { signal?: AbortSignal } = {}) => {
        emit = onEvent as (event: unknown) => void;
        return new Promise<void>((_resolve, reject) => {
          options.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
        });
      }),
    });
    renderChat(api, "thread-a");
    await waitFor(() => expect(emit).not.toBeNull());

    act(() => {
      emit!({ event_id: 1, event: "generation.thinking_delta", job_id: "job-reason", thread_id: "thread-a", data: { delta: "reasoning..." } });
    });
    const details = (await screen.findByText("Reasoning")).closest("details");
    expect(details).toHaveAttribute("open");
    expect(screen.getByText("Live")).toBeInTheDocument();

    act(() => {
      emit!({ event_id: 2, event: "generation.persisting", job_id: "job-reason", thread_id: "thread-a", data: {} });
    });

    await waitFor(() => expect(details).not.toHaveAttribute("open"));
    expect(screen.queryByText("Live")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stop generating" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Finishing response" })).toBeDisabled();
  });

  it("retains the exact draft if generation acceptance fails", async () => {
    const user = userEvent.setup();
    const api = chatApi({ generate: vi.fn().mockRejectedValue(new ApiError(503, "Local runtime is unavailable.")) });
    renderChat(api);

    const composer = await screen.findByLabelText("Message Cortex");
    await user.type(composer, "Do not lose this message");
    await user.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Local runtime is unavailable."));
    expect(composer).toHaveValue("Do not lose this message");
    await waitFor(() => expect(composer).toHaveFocus());
  });

  it("regenerates the dangling user turn instead of duplicating it after a stream-level failure", async () => {
    // Regression guard: unlike an ambiguous admission failure (the request
    // above), this failure happens *after* the backend already accepted the
    // message and returned user_message_id -- the user's turn is durably
    // persisted with no reply. Retrying it used to call generate() again,
    // posting the same text as a second, duplicate user message.
    const user = userEvent.setup();
    let emit: ((event: unknown) => void) | null = null;
    const generate = vi.fn().mockResolvedValue({
      job_id: "job-fail-1",
      kind: "generation" as const,
      status: "queued" as const,
      thread_id: "thread-a",
      user_message_id: "message-user-1",
    });
    const regenerate = vi.fn().mockResolvedValue({
      job_id: "job-retry-1",
      kind: "generation" as const,
      status: "queued" as const,
      thread_id: "thread-a",
      user_message_id: "message-user-1",
    });
    const api = chatApi({
      generate,
      regenerate,
      // The failure path reconciles the chat from the server even though
      // the generation failed -- the user's turn is durably persisted
      // there regardless of what happened afterward.
      chat: vi.fn(async (id: string) => ({
        ...emptyChat(id),
        messages: [{ id: "message-user-1", role: "user" as const, content: "Will this work" }],
      })),
      // Mimics the real client: the stream resolves once a terminal event
      // has been delivered, matching how a real SSE connection closes.
      streamGeneration: vi.fn((_jobId, onEvent, options: { signal?: AbortSignal } = {}) => new Promise<void>((resolve, reject) => {
        options.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
        emit = (event) => {
          (onEvent as (event: unknown) => void)(event);
          if ((event as { event: string }).event === "generation.failed") resolve();
        };
      })),
    });
    renderChat(api);

    const composer = await screen.findByLabelText("Message Cortex");
    await user.type(composer, "Will this work");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(emit).not.toBeNull());

    act(() => {
      emit!({ event_id: 1, event: "generation.failed", job_id: "job-fail-1", thread_id: "thread-a", data: { message: "The model failed." } });
    });

    await screen.findByRole("button", { name: "Retry last message" });
    await user.click(screen.getByRole("button", { name: "Retry last message" }));

    await waitFor(() => expect(regenerate).toHaveBeenCalledTimes(1));
    expect(regenerate.mock.calls[0][0]).toBe("thread-a");
    expect(regenerate.mock.calls[0][1].message_id).toBe("message-user-1");
    expect(generate).toHaveBeenCalledTimes(1);
  });

  it("reuses the admission key when retrying after an ambiguous generation failure", async () => {
    const user = userEvent.setup();
    const generate = vi.fn()
      .mockRejectedValueOnce(new Error("The connection was lost."))
      .mockResolvedValueOnce({
        job_id: "job-replayed",
        kind: "generation" as const,
        status: "queued" as const,
        thread_id: "thread-a",
        user_message_id: "message-replayed",
      });
    const api = chatApi({ generate });
    renderChat(api);

    const composer = await screen.findByLabelText("Message Cortex");
    await user.type(composer, "Replay this admission");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByRole("button", { name: "Retry last message" });

    await user.click(screen.getByRole("button", { name: "Retry last message" }));
    await waitFor(() => expect(generate).toHaveBeenCalledTimes(2));
    expect(generate.mock.calls[1][0].request_id).toBe(generate.mock.calls[0][0].request_id);
  });

  it("retries the original admission payload after chat context and options change", async () => {
    const user = userEvent.setup();
    let revision = 3;
    const generate = vi.fn()
      .mockRejectedValueOnce(new Error("The connection was lost."))
      .mockResolvedValueOnce({
        job_id: "job-replayed-context",
        kind: "generation" as const,
        status: "queued" as const,
        thread_id: "thread-a",
        user_message_id: "message-replayed-context",
      });
    const api = chatApi({
      chat: vi.fn(async (id: string) => ({ ...emptyChat(id), revision })),
      generate,
    });
    useChatStore.getState().setThreadOptions("thread-a", { temperature: 0.2 });
    const view = renderChat(api, "thread-a");

    const composer = await screen.findByLabelText("Message Cortex");
    await user.type(composer, "Replay with its original context");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByRole("button", { name: "Retry last message" });
    const originalPayload = generate.mock.calls[0][0];

    revision = 9;
    useChatStore.getState().setThreadOptions("thread-a", { temperature: 0.8 });
    view.rerender(
      <ChatPage
        api={api}
        threadId="thread-b"
        runtimeReady
        runtimeMessage={null}
        localModels={["local-chat:7b"]}
        selectedModel="local-chat:7b"
        modelBusy={false}
        onSelectModel={async () => true}
        onRescanModels={async () => undefined}
        onThreadCreated={vi.fn()}
        onChatChanged={vi.fn()}
        onForked={vi.fn()}
        onSessionExpired={vi.fn()}
      />,
    );
    await waitFor(() => expect(api.chat).toHaveBeenCalledWith("thread-b"));
    view.rerender(
      <ChatPage
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
        onChatChanged={vi.fn()}
        onForked={vi.fn()}
        onSessionExpired={vi.fn()}
      />,
    );
    await screen.findByRole("button", { name: "Retry last message" });

    await user.click(screen.getByRole("button", { name: "Retry last message" }));
    await waitFor(() => expect(generate).toHaveBeenCalledTimes(2));
    expect(generate.mock.calls[1][0]).toEqual(originalPayload);
  });

  it("uses a fresh admission key for a deliberate new user turn after failure", async () => {
    const user = userEvent.setup();
    const generate = vi.fn()
      .mockRejectedValueOnce(new Error("The connection was lost."))
      .mockResolvedValueOnce({
        job_id: "job-new-turn",
        kind: "generation" as const,
        status: "queued" as const,
        thread_id: "thread-a",
        user_message_id: "message-new-turn",
      });
    const api = chatApi({ generate });
    renderChat(api);

    const composer = await screen.findByLabelText("Message Cortex");
    await user.type(composer, "First turn");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByRole("button", { name: "Retry last message" });

    await user.clear(composer);
    await user.type(composer, "Second turn");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(generate).toHaveBeenCalledTimes(2));
    expect(generate.mock.calls[1][0].request_id).not.toBe(generate.mock.calls[0][0].request_id);
    expect(generate.mock.calls[1][0].user_input).toBe("Second turn");
  });

  it("clears the submitted draft only after the backend accepts it", async () => {
    const user = userEvent.setup();
    let accept!: (value: { job_id: string; kind: "generation"; status: "queued"; thread_id: string; user_message_id: string }) => void;
    const accepted = new Promise<{ job_id: string; kind: "generation"; status: "queued"; thread_id: string; user_message_id: string }>((resolve) => { accept = resolve; });
    const api = chatApi({ generate: vi.fn(() => accepted) });
    renderChat(api);

    const composer = await screen.findByLabelText("Message Cortex");
    await user.type(composer, "Wait for acceptance");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    expect(composer).toHaveValue("Wait for acceptance");

    accept({ job_id: "job-1", kind: "generation", status: "queued", thread_id: "thread-a", user_message_id: "message-1" });
    await waitFor(() => expect(composer).toHaveValue(""));
  });

  it("restores separate session drafts for each conversation", async () => {
    const user = userEvent.setup();
    const api = chatApi();
    const view = renderChat(api, "thread-a");
    const composer = await screen.findByLabelText("Message Cortex");
    await user.type(composer, "Draft for A");

    view.rerender(
      <ChatPage
        api={api}
        threadId="thread-b"
        runtimeReady
        runtimeMessage={null}
        localModels={["local-chat:7b"]}
        selectedModel="local-chat:7b"
        modelBusy={false}
        onSelectModel={async () => true}
        onRescanModels={async () => undefined}
        onThreadCreated={vi.fn()}
        onChatChanged={vi.fn()}
        onForked={vi.fn()}
        onSessionExpired={vi.fn()}
      />,
    );
    const composerB = await screen.findByLabelText("Message Cortex");
    expect(composerB).toHaveValue("");
    await user.type(composerB, "Draft for B");

    view.rerender(
      <ChatPage
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
        onChatChanged={vi.fn()}
        onForked={vi.fn()}
        onSessionExpired={vi.fn()}
      />,
    );
    await waitFor(() => {
      expect(screen.getByLabelText("Message Cortex")).toHaveValue("Draft for A");
    });
  });

  it("keeps the active generation state available after changing conversations", async () => {
    const user = userEvent.setup();
    const api = chatApi({
      generate: vi.fn().mockResolvedValue({
        job_id: "job-active", kind: "generation", status: "queued", thread_id: "thread-a", user_message_id: "message-1",
      }),
    });
    const view = renderChat(api, "thread-a");
    const composer = await screen.findByLabelText("Message Cortex");
    await user.type(composer, "Keep working while I browse");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByRole("button", { name: "Stop generating" });

    view.rerender(
      <ChatPage
        api={api}
        threadId="thread-b"
        runtimeReady
        runtimeMessage={null}
        localModels={["local-chat:7b"]}
        selectedModel="local-chat:7b"
        modelBusy={false}
        onSelectModel={async () => true}
        onRescanModels={async () => undefined}
        onThreadCreated={vi.fn()}
        onChatChanged={vi.fn()}
        onForked={vi.fn()}
        onSessionExpired={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByText("Generating in another thread")).toBeVisible());
    expect(screen.getByRole("button", { name: "Stop generating" })).toBeVisible();
    expect(screen.getByLabelText("Message Cortex")).toBeEnabled();
  });

  it("does not stay stuck in stopping when cancellation loses to persistence", async () => {
    const user = userEvent.setup();
    const cancelGeneration = vi.fn(async () => ({
      job_id: "job-committing",
      kind: "generation" as const,
      status: "running" as const,
      sequence: 3,
    }));
    const api = chatApi({
      generate: vi.fn().mockResolvedValue({
        job_id: "job-committing",
        kind: "generation",
        status: "queued",
        thread_id: "thread-a",
        user_message_id: "message-1",
      }),
      cancelGeneration,
      streamGeneration: vi.fn((_jobId, _onEvent, options: { signal?: AbortSignal } = {}) =>
        new Promise<void>((_resolve, reject) => {
          options.signal?.addEventListener(
            "abort",
            () => reject(new DOMException("Aborted", "AbortError")),
            { once: true },
          );
        }),
      ),
    });
    renderChat(api);

    await user.type(await screen.findByLabelText("Message Cortex"), "Persist this answer");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await user.click(await screen.findByRole("button", { name: "Stop generating" }));

    await waitFor(() => expect(cancelGeneration).toHaveBeenCalledWith("job-committing"));
    expect(screen.queryByRole("button", { name: "Stopping response" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stop generating" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Finishing response" })).toBeDisabled();
    expect(screen.getByText("Finishing response...")).toBeVisible();
  });

  it("regenerates from the selected user turn instead of stale cross-chat or composer state", async () => {
    const user = userEvent.setup();
    const originalAttachment: ChatAttachment = {
      attachment_id: "original-doc",
      filename: "original.md",
      mime_type: "text/markdown",
      size: 12,
      sha256: "d".repeat(64),
      kind: "document",
      expires_at: "2099-01-01T00:00:00Z",
    };
    const draftAttachment: ChatAttachment = {
      attachment_id: "next-draft-doc",
      filename: "next-draft.md",
      mime_type: "text/markdown",
      size: 14,
      sha256: "e".repeat(64),
      kind: "document",
      expires_at: "2099-01-01T00:00:00Z",
    };
    const threadA = emptyChat("thread-a");
    const threadB: ChatResponse = {
      ...emptyChat("thread-b"),
      revision: 2,
      messages: [
        { id: "user-b", role: "user", content: "Prompt from B", attachments: [originalAttachment] },
        { id: "assistant-b", role: "assistant", content: "Answer from B" },
      ],
    };
    const api = chatApi({
      chat: vi.fn(async (id: string) => id === "thread-b" ? threadB : threadA),
      generate: vi.fn().mockResolvedValue({
        job_id: "job-a", kind: "generation", status: "queued", thread_id: "thread-a", user_message_id: "user-a",
      }),
      regenerate: vi.fn().mockResolvedValue({
        job_id: "job-b", kind: "generation", status: "queued", thread_id: "thread-b",
      }),
      stageChatAttachment: vi.fn().mockResolvedValue(draftAttachment),
      streamGeneration: vi.fn(async (jobId, onEvent) => {
        const completedThreadId = jobId === "job-a" ? "thread-a" : "thread-b";
        onEvent({
          event_id: 1,
          event: "generation.completed",
          job_id: jobId,
          thread_id: completedThreadId,
          data: {},
        });
      }),
    });
    const view = renderChat(api, "thread-a");
    const composer = await screen.findByLabelText("Message Cortex");
    await user.type(composer, "Prompt from A");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(window.sessionStorage.getItem("cortex.active.generation")).toBeNull());

    view.rerender(
      <ChatPage
        api={api}
        threadId="thread-b"
        runtimeReady
        runtimeMessage={null}
        localModels={["local-chat:7b"]}
        selectedModel="local-chat:7b"
        modelBusy={false}
        onSelectModel={async () => true}
        onRescanModels={async () => undefined}
        onThreadCreated={vi.fn()}
        onChatChanged={vi.fn()}
        onForked={vi.fn()}
        onSessionExpired={vi.fn()}
      />,
    );
    expect(await screen.findByText("Answer from B")).toBeInTheDocument();
    await user.upload(
      screen.getByLabelText("Attach images or documents"),
      new File(["next"], "next-draft.md", { type: "text/markdown" }),
    );
    expect(await screen.findByRole("button", { name: "Remove next-draft.md" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Regenerate response" }));

    await waitFor(() => expect(api.regenerate).toHaveBeenCalledWith("thread-b", expect.objectContaining({
      message_id: "assistant-b",
      user_input: "Prompt from B",
      attachments: [originalAttachment],
    })));
    expect(screen.getByRole("button", { name: "Remove next-draft.md" })).toBeInTheDocument();
  });

  it("stages a document without putting its contents into the composer and sends its opaque metadata", async () => {
    const user = userEvent.setup();
    const attachment: ChatAttachment = {
      attachment_id: "doc-1",
      filename: "notes.md",
      mime_type: "text/markdown",
      size: 12,
      sha256: "b".repeat(64),
      kind: "document",
      expires_at: "2099-01-01T00:00:00Z",
    };
    const api = chatApi({
      stageChatAttachment: vi.fn().mockResolvedValue(attachment),
      generate: vi.fn().mockResolvedValue({ job_id: "job-1", kind: "generation", status: "queued", thread_id: "thread-a", user_message_id: "message-1" }),
    });
    renderChat(api, "thread-a", true);

    const file = new File(["private document contents"], "notes.md", { type: "text/markdown" });
    await user.upload(await screen.findByLabelText("Attach images or documents"), file);
    expect(await screen.findByText("notes.md")).toBeInTheDocument();
    expect(screen.getByLabelText("Message Cortex")).toHaveValue("");

    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(api.generate).toHaveBeenCalledWith(expect.objectContaining({
      user_input: "Please review the attached file(s).",
      attachments: [attachment],
    })));
  });

  it("keeps attachments staged while generation acceptance is pending", async () => {
    const user = userEvent.setup();
    const firstAttachment: ChatAttachment = {
      attachment_id: "doc-first",
      filename: "first.md",
      mime_type: "text/markdown",
      size: 5,
      sha256: "f".repeat(64),
      kind: "document",
      expires_at: "2099-01-01T00:00:00Z",
    };
    const nextAttachment: ChatAttachment = {
      attachment_id: "doc-next",
      filename: "next.md",
      mime_type: "text/markdown",
      size: 4,
      sha256: "0".repeat(64),
      kind: "document",
      expires_at: "2099-01-01T00:00:00Z",
    };
    let accept!: (value: { job_id: string; kind: "generation"; status: "queued"; thread_id: string; user_message_id: string }) => void;
    const accepted = new Promise<{ job_id: string; kind: "generation"; status: "queued"; thread_id: string; user_message_id: string }>((resolve) => { accept = resolve; });
    const api = chatApi({
      stageChatAttachment: vi.fn()
        .mockResolvedValueOnce(firstAttachment)
        .mockResolvedValueOnce(nextAttachment),
      generate: vi.fn(() => accepted),
    });
    renderChat(api);

    const attachmentInput = await screen.findByLabelText("Attach images or documents");
    await user.upload(attachmentInput, new File(["first"], "first.md", { type: "text/markdown" }));
    await screen.findByRole("button", { name: "Remove first.md" });
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(api.generate).toHaveBeenCalledTimes(1));

    await user.upload(attachmentInput, new File(["next"], "next.md", { type: "text/markdown" }));
    expect(await screen.findByRole("button", { name: "Remove next.md" })).toBeInTheDocument();

    accept({ job_id: "job-1", kind: "generation", status: "queued", thread_id: "thread-a", user_message_id: "message-1" });

    await waitFor(() => expect(screen.queryByRole("button", { name: "Remove first.md" })).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Remove next.md" })).toBeInTheDocument();
  });

  it("explains the image capability mismatch before a generation request is made", async () => {
    const user = userEvent.setup();
    const attachment: ChatAttachment = {
      attachment_id: "image-1",
      filename: "photo.png",
      mime_type: "image/png",
      size: 12,
      sha256: "c".repeat(64),
      kind: "image",
      expires_at: "2099-01-01T00:00:00Z",
    };
    const api = chatApi({ stageChatAttachment: vi.fn().mockResolvedValue(attachment) });
    renderChat(api, "thread-a", false);

    const file = new File([new Uint8Array([137, 80, 78, 71])], "photo.png", { type: "image/png" });
    await user.upload(await screen.findByLabelText("Attach images or documents"), file);

    expect(await screen.findByRole("alert")).toHaveTextContent("cannot accept images");
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
    expect(api.generate).not.toHaveBeenCalled();
  });
});
