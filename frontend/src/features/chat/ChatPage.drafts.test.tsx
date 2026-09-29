import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChatResponse } from "../../../../contracts/cortex-api";
import type { CortexApi } from "../../api/client";
import { composerDraftKey } from "../../lib/composerDraft";
import { useChatStore } from "../../stores/useChatStore";
import { ChatPage } from "./ChatPage";
import { GenerationStreamHost } from "./GenerationStreamHost";

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
    streamGeneration: vi.fn((_jobId, _onEvent, options: { signal?: AbortSignal } = {}) => new Promise<void>((_resolve, reject) => {
      options.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
    })),
    generationStatus: vi.fn(),
    cancelGeneration: vi.fn(),
    stageChatAttachment: vi.fn(),
    ...overrides,
  } as unknown as CortexApi;
}

function renderPage(api: CortexApi, threadId: string | null) {
  return render(
    <>
      <GenerationStreamHost api={api} onSessionExpired={() => undefined} />
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
        onForked={vi.fn()}
      />
    </>,
  );
}

describe("ChatPage drafts across restarts", () => {
  afterEach(() => {
    window.sessionStorage.clear();
    window.localStorage.clear();
    useChatStore.setState({ generationOptionsByThread: {}, proposedMemoriesByMessage: {} });
  });

  it("restores a draft an earlier run left in local storage", async () => {
    // Nothing in session storage: the page is a fresh launch.
    window.localStorage.setItem(composerDraftKey("thread-a"), "Unsent from yesterday");

    renderPage(chatApi(), "thread-a");

    expect(await screen.findByLabelText("Message Cortex")).toHaveValue("Unsent from yesterday");
  });

  it("restores the new-chat draft on a fresh launch", async () => {
    window.localStorage.setItem(composerDraftKey(null), "Started a new idea");

    renderPage(chatApi(), null);

    expect(await screen.findByLabelText("Message Cortex")).toHaveValue("Started a new idea");
  });

  it("keeps typing in local storage and clears it from both storages when the message is sent", async () => {
    const user = userEvent.setup();
    const api = chatApi({
      generate: vi.fn().mockResolvedValue({ job_id: "job-1", kind: "generation", status: "queued", thread_id: "thread-a", user_message_id: "m-1" }),
    });
    renderPage(api, "thread-a");

    await user.type(await screen.findByLabelText("Message Cortex"), "typed and kept");
    expect(window.localStorage.getItem(composerDraftKey("thread-a"))).toBe("typed and kept");

    await user.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() => expect(api.generate).toHaveBeenCalled());
    await waitFor(() => expect(window.localStorage.getItem(composerDraftKey("thread-a"))).toBeNull());
    expect(window.sessionStorage.getItem(composerDraftKey("thread-a"))).toBeNull();
  });

  it("does not lose the composer when storage is denied", async () => {
    const user = userEvent.setup();
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("full", "QuotaExceededError"); });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new DOMException("denied", "SecurityError"); });
    try {
      renderPage(chatApi(), "thread-a");

      const composer = await screen.findByLabelText("Message Cortex");
      await user.type(composer, "still works");

      expect(composer).toHaveValue("still works");
    } finally {
      vi.restoreAllMocks();
    }
  });
});
