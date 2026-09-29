import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ComponentProps } from "react";
import type { ChatResponse } from "../../../../contracts/cortex-api";
import type { CortexApi } from "../../api/client";
import { useChatStore } from "../../stores/useChatStore";
import { useUiStore } from "../../stores/useUiStore";
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

function renderPage(api: CortexApi, props: Partial<ComponentProps<typeof ChatPage>> = {}) {
  return render(
    <>
      <GenerationStreamHost api={api} onSessionExpired={() => undefined} />
      <ChatPage
        api={api}
        threadId={null}
        runtimeReady
        runtimeMessage={null}
        localModels={["local-chat:7b"]}
        selectedModel="local-chat:7b"
        modelBusy={false}
        onSelectModel={async () => true}
        onRescanModels={async () => undefined}
        onThreadCreated={vi.fn()}
        onForked={vi.fn()}
        {...props}
      />
    </>,
  );
}

describe("ChatPage new thread", () => {
  afterEach(() => {
    window.sessionStorage.clear();
    window.localStorage.clear();
    useChatStore.setState({ generationOptionsByThread: {}, proposedMemoriesByMessage: {} });
    useUiStore.setState({ toasts: [] });
  });

  it("shows which model will answer, three keyboard hints, and a line about attachments", async () => {
    renderPage(chatApi());

    expect(await screen.findByText("local-chat:7b", { selector: "strong" })).toBeVisible();
    expect(screen.getByText(/Chatting with/)).toBeVisible();
    const hints = screen.getByRole("list", { name: "Keyboard shortcuts" });
    expect(hints.querySelectorAll("li")).toHaveLength(3);
    expect(hints).toHaveTextContent("Enter sends");
    expect(hints).toHaveTextContent("Ctrl/Cmd + K opens the command palette");
    expect(hints).toHaveTextContent("? lists every shortcut");
    expect(screen.getByText(/Drop, paste or attach images and text or code files/)).toBeVisible();
    // The copy the product deliberately dropped stays dropped (pinned by the e2e suite too).
    expect(screen.queryByText("What will you make today?")).not.toBeInTheDocument();
    expect(screen.queryByText("LOCAL, PRIVATE, YOURS")).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "New thread" })).not.toBeInTheDocument();
  });

  it("names the GGUF file rather than its internal id", async () => {
    renderPage(chatApi(), { selectedModel: "gguf:demo.Q4_K_M.gguf", localModels: ["gguf:demo.Q4_K_M.gguf"] });

    expect(await screen.findByText("demo.Q4_K_M.gguf", { selector: "strong" })).toBeVisible();
    expect(screen.queryByText(/gguf:/)).not.toBeInTheDocument();
  });

  it("points at the model picker instead of naming a model when none is selected", async () => {
    renderPage(chatApi(), { selectedModel: null, runtimeReady: false, runtimeMessage: "Select a local model before sending a message." });

    expect(await screen.findByText(/No model is selected yet/)).toBeVisible();
    expect(screen.queryByText(/Chatting with/)).not.toBeInTheDocument();
  });

  it("is not shown for a saved conversation, even an empty one", async () => {
    renderPage(chatApi(), { threadId: "thread-a" });

    await screen.findByLabelText("Message Cortex");
    expect(screen.queryByText(/Chatting with/)).not.toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Keyboard shortcuts" })).not.toBeInTheDocument();
  });

  it("goes away when the first message starts", async () => {
    const user = userEvent.setup();
    const api = chatApi({
      generate: vi.fn().mockResolvedValue({ job_id: "job-1", kind: "generation", status: "queued", thread_id: "thread-new", user_message_id: "m-1" }),
    });
    renderPage(api);

    expect(await screen.findByText(/Chatting with/)).toBeVisible();
    await user.type(screen.getByLabelText("Message Cortex"), "hello");
    await user.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() => expect(api.generate).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByText(/Chatting with/)).not.toBeInTheDocument());
  });
});
