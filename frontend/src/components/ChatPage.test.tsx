import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChatResponse } from "../../../contracts/cortex-api";
import { ApiError, CortexApi } from "../api/client";
import { humanizeGenerationStatus } from "../lib/generationStatus";
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
    ...overrides,
  } as unknown as CortexApi;
}

function renderChat(api: CortexApi, threadId = "thread-a") {
  return render(
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
      onThreadCreated={vi.fn()}
      onChatChanged={vi.fn()}
      onForked={vi.fn()}
    />,
  );
}

describe("ChatPage composer integration", () => {
  afterEach(() => window.sessionStorage.clear());

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
    await waitFor(() => expect(composer).toHaveValue(""));
    await user.type(composer, "Draft for B");

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
    await waitFor(() => expect(composer).toHaveValue("Draft for A"));
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

    await waitFor(() => expect(screen.getByText("Generating in another conversation")).toBeVisible());
    expect(screen.getByRole("button", { name: "Stop generating" })).toBeVisible();
    expect(screen.getByLabelText("Message Cortex")).toBeEnabled();
  });
});
