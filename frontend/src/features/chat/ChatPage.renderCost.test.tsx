import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ChatResponse } from "../../../../contracts/cortex-api";
import type { CortexApi } from "../../api/client";
import { useChatStore } from "../../stores/useChatStore";

// Count real Markdown parses by source text, passing through to the real
// component, so "did this message get re-parsed?" is a fact and not a proxy.
const parsed = vi.hoisted(() => ({ sources: [] as string[] }));

vi.mock("react-markdown", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-markdown")>();
  return {
    ...actual,
    default: (options: Parameters<typeof actual.default>[0]) => {
      parsed.sources.push(String(options.children));
      return actual.default(options);
    },
  };
});

import { ChatPage } from "./ChatPage";

const PERSISTED = ["Persisted answer **one**", "Persisted answer `two`"];

const chat: ChatResponse = {
  id: "thread-a",
  title: "Render cost",
  timestamp: "2026-01-01T00:00:00Z",
  revision: 1,
  messages: [
    { id: "u1", role: "user", content: "Question one" },
    { id: "a1", role: "assistant", content: PERSISTED[0] },
    { id: "u2", role: "user", content: "Question two" },
    { id: "a2", role: "assistant", content: PERSISTED[1] },
  ],
};

function renderChat() {
  const api = {
    chat: vi.fn(async () => chat),
    generate: vi.fn(),
    regenerate: vi.fn(),
    streamGeneration: vi.fn(() => new Promise<void>(() => undefined)),
    generationStatus: vi.fn(),
    cancelGeneration: vi.fn(),
    forkChat: vi.fn(),
    stageChatAttachment: vi.fn(),
  } as unknown as CortexApi;
  return render(
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
}

const parsesOf = (source: string) => parsed.sources.filter((candidate) => candidate === source).length;
const livePartials = () => parsed.sources.filter((source) => source.startsWith("tok"));

const scrollTopWrites: number[] = [];

beforeEach(() => {
  parsed.sources.length = 0;
  scrollTopWrites.length = 0;
  // jsdom lays nothing out; give the transcript a scrollable geometry and
  // record every programmatic scroll.
  Object.defineProperty(HTMLElement.prototype, "scrollHeight", { configurable: true, get: () => 1000 });
  Object.defineProperty(HTMLElement.prototype, "clientHeight", { configurable: true, get: () => 200 });
  Object.defineProperty(HTMLElement.prototype, "scrollTop", {
    configurable: true,
    get: () => 0,
    set: (value: number) => { scrollTopWrites.push(value); },
  });
});

afterEach(() => {
  // Own properties added above; removing them restores jsdom's inherited ones.
  for (const property of ["scrollHeight", "clientHeight", "scrollTop"]) {
    Reflect.deleteProperty(HTMLElement.prototype, property);
  }
  vi.restoreAllMocks();
  window.sessionStorage.clear();
});

async function openChatAndStartStreaming() {
  renderChat();
  await waitFor(() => expect(parsesOf(PERSISTED[0])).toBeGreaterThan(0));
  act(() => useChatStore.getState().beginGeneration("job-live", "thread-a"));
}

const stream = (token: string) => act(() => useChatStore.getState().appendContentToken("job-live", token));

describe("ChatPage while a reply streams", () => {
  it("does not re-parse the persisted messages for any streamed frame", async () => {
    await openChatAndStartStreaming();
    const before = PERSISTED.map(parsesOf);

    for (let frame = 0; frame < 40; frame += 1) stream("tok ");

    expect(PERSISTED.map(parsesOf)).toEqual(before);
  });

  it("parses the growing reply far less often than it arrives", async () => {
    await openChatAndStartStreaming();

    for (let frame = 0; frame < 40; frame += 1) stream("tok ");

    // Forty frames in one tick: the bubble mounts once and is at most refreshed
    // once more, rather than being parsed for every frame.
    expect(livePartials().length).toBeLessThanOrEqual(3);
  });

  it("still ends up showing every token, and the exact final text the moment it is ready", async () => {
    await openChatAndStartStreaming();
    const expected = "tok ".repeat(40);

    for (let frame = 0; frame < 40; frame += 1) stream("tok ");
    await waitFor(() => expect(screen.getByLabelText("Cortex response in progress")).toHaveTextContent(expected.trim()));

    // More text arrives, then the backend says the content is complete: the
    // last tokens must not wait out a throttle interval.
    stream("last words");
    act(() => useChatStore.getState().markContentReady("job-live"));

    expect(screen.getByLabelText("Cortex response ready, saving...")).toHaveTextContent(`${expected}last words`.replace(/\s+/g, " ").trim());
  });

  it("streams thinking text the same way", async () => {
    await openChatAndStartStreaming();
    for (let frame = 0; frame < 20; frame += 1) {
      act(() => useChatStore.getState().appendThinkingToken("job-live", "hm "));
    }

    await waitFor(() => expect(screen.getByText("Live")).toBeVisible());
    await waitFor(() => expect(document.querySelector(".reasoning")).toHaveTextContent("hm hm hm"));
    expect(PERSISTED.map(parsesOf)).toEqual(PERSISTED.map(() => 1));
  });

  it("follows the output down while the reader is at the bottom", async () => {
    await openChatAndStartStreaming();
    const writesBefore = scrollTopWrites.length;

    stream("tok ");
    await waitFor(() => expect(scrollTopWrites.length).toBeGreaterThan(writesBefore));

    expect(scrollTopWrites.at(-1)).toBe(1000);
    expect(screen.queryByRole("button", { name: "Jump to latest" })).toBeNull();
  });

  it("offers Jump to latest instead of yanking a reader who scrolled away", async () => {
    await openChatAndStartStreaming();
    // scrollHeight 1000, scrollTop 0, clientHeight 200: nowhere near the end.
    fireEvent.scroll(document.querySelector(".transcript")!);
    scrollTopWrites.length = 0;

    stream("tok ");

    expect(await screen.findByRole("button", { name: "Jump to latest" })).toBeVisible();
    expect(scrollTopWrites).toEqual([]);

    fireEvent.click(screen.getByRole("button", { name: "Jump to latest" }));
    expect(scrollTopWrites.at(-1)).toBe(1000);
    expect(screen.queryByRole("button", { name: "Jump to latest" })).toBeNull();
  });

  it("does not react to another thread's generation", async () => {
    renderChat();
    await waitFor(() => expect(parsesOf(PERSISTED[0])).toBeGreaterThan(0));
    fireEvent.scroll(document.querySelector(".transcript")!);
    act(() => useChatStore.getState().beginGeneration("job-elsewhere", "thread-b"));

    act(() => useChatStore.getState().appendContentToken("job-elsewhere", "tok "));

    expect(screen.queryByLabelText("Cortex response in progress")).toBeNull();
    expect(screen.queryByRole("button", { name: "Jump to latest" })).toBeNull();
  });
});
