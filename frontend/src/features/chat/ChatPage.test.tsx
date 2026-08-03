import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useState } from "react";
import type { ChatAttachment, ChatResponse } from "../../../../contracts/cortex-api";
import { ApiError, CortexApi } from "../../api/client";
import { humanizeGenerationStatus } from "../../lib/generationStatus";
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

function renderChat(api: CortexApi, threadId = "thread-a", selectedModelSupportsVision: boolean | null = null) {
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
    />,
  );
}

describe("ChatPage composer integration", () => {
  afterEach(() => window.sessionStorage.clear());

  it("keeps a blank conversation focused on the composer", async () => {
    renderChat(chatApi());

    await screen.findByLabelText("Message Cortex");

    expect(screen.queryByRole("heading", { name: "New thread" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Message Cortex")).toHaveValue("");
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

    expect(await screen.findByText("Cortex")).toBeInTheDocument();
    expect(screen.getByText("You")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Result", level: 1 })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy ts code" })).toBeInTheDocument();
    expect(screen.getByText("Reasoning")).toBeInTheDocument();
    expect(screen.getByText("Reasoning").closest("details")?.parentElement).toHaveClass("message-card");
    expect(screen.queryByText("This must never render in the user bubble.")).not.toBeInTheDocument();
    expect(screen.getByText("Sources")).toBeInTheDocument();
    expect(screen.getByText("result.md")).toBeInTheDocument();
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

  it("replays an active generation from the beginning after a remount", async () => {
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
    first.unmount();
    renderChat(api, "thread-a");
    await waitFor(() => expect(streamCalls).toHaveLength(2));

    expect(streamCalls[0].afterEventId).toBe(0);
    expect(streamCalls[1].afterEventId).toBe(0);
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
      />,
    );

    await waitFor(() => expect(screen.getByText("Generating in another thread")).toBeVisible());
    expect(screen.getByRole("button", { name: "Stop generating" })).toBeVisible();
    expect(screen.getByLabelText("Message Cortex")).toBeEnabled();
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
