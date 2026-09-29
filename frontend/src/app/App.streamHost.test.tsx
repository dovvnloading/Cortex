import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { CortexApi } from "../api/client";
import { useChatStore } from "../stores/useChatStore";
import { ToastProvider } from "./ToastProvider";

/**
 * The stream host lives above the routes. These tests drive a whole workspace
 * with a hand-fed event stream, leave the chat for Settings while it is still
 * running, and check what the user finds when they come back.
 */

const WAIT = { timeout: 10_000 };
const THREAD = { id: "thread-a", title: "New Chat", timestamp: "2026-07-21T18:00:00Z" };
const USER_TURN = { id: "message-user", role: "user", content: "Tell me a story" };
const ANSWER = { id: "message-assistant", role: "assistant", content: "Once upon a time, the end." };

const respond = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { "Content-Type": "application/json" },
});

type StreamEvent = { event_id: number; event: string; data?: Record<string, unknown> };

/** An event stream the test feeds by hand, so it decides when text arrives and when the job ends. */
function handFedStream() {
  const encoder = new TextEncoder();
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({ start: (started) => { controller = started; } });
  let connectionLost = false;
  return {
    body,
    // What a real fetch does when its signal aborts: the body errors out.
    abortOn: (signal: AbortSignal | null | undefined) => {
      signal?.addEventListener("abort", () => {
        connectionLost = true;
        controller.error(new DOMException("Aborted", "AbortError"));
      }, { once: true });
    },
    push: (event: StreamEvent) => act(() => {
      // Nothing is delivered to a connection that is gone.
      if (connectionLost) return;
      const full = { job_id: "job-a", thread_id: "thread-a", data: {}, ...event };
      controller.enqueue(encoder.encode(`id: ${event.event_id}\ndata: ${JSON.stringify(full)}\n\n`));
    }),
    close: () => act(() => {
      if (!connectionLost) controller.close();
    }),
  };
}

function backend() {
  const stream = handFedStream();
  const saved = { title: "New Chat", messages: [USER_TURN] as unknown[] };
  const fetcher = vi.fn<typeof fetch>(async (input, init) => {
    const url = String(input);
    if (url.endsWith("/generations/job-a/events")) {
      stream.abortOn(init?.signal);
      return new Response(stream.body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
    }
    if (url.endsWith("/system")) return respond({ status: "ok", preview: true, session_required: true, started_at: "2026-07-21T18:00:00Z" });
    if (url.endsWith("/chat-groups")) return respond([]);
    if (url.endsWith("/chats")) return respond([{ ...THREAD, title: saved.title }]);
    if (url.endsWith("/chats/thread-a")) return respond({ ...THREAD, title: saved.title, revision: saved.messages.length, messages: saved.messages });
    if (url.endsWith("/settings")) return respond({ settings: { models: { chat: "model-a", title: null }, appearance: { theme: "dark" } } });
    if (url.endsWith("/memories")) return respond({ memos: [] });
    if (url.endsWith("/models")) return respond({ required_models: [], optional_models: [], installed_models: ["model-a"], connection: { success: true, status: "connected", message: "Ready" } });
    return respond({ detail: "Unexpected test route." }, 404);
  });
  const calls = (suffix: string) => fetcher.mock.calls.filter(([input]) => String(input).endsWith(suffix));
  return { fetcher, stream, saved, calls };
}

describe("a generation while Settings is open", () => {
  // Load the lazy routes before any test runs, exactly as App.test.tsx does, so a
  // test's own time covers what it does and not a module graph's first transform.
  beforeAll(async () => {
    await Promise.all([
      import("../features/chat/ChatPage"),
      import("../features/settings/SettingsPanel"),
    ]);
  }, 120_000);

  afterEach(() => {
    const { generation, endGeneration } = useChatStore.getState();
    if (generation.jobId) endGeneration(generation.jobId);
    window.sessionStorage.clear();
    window.history.replaceState({}, "", "/");
  });

  /** A chat with a job already running on the backend, as a reload would find it. */
  const openRunningChat = async (server: ReturnType<typeof backend>, { strict = false }: { strict?: boolean } = {}) => {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    window.sessionStorage.setItem("cortex.active.generation", JSON.stringify({ jobId: "job-a", threadId: "thread-a", lastEventId: 0 }));
    window.history.replaceState({}, "", "/chat/thread-a");
    // main.tsx renders the app inside StrictMode, so that is the shape to test.
    const app = <ToastProvider><App api={new CortexApi("/api/v1", server.fetcher)} /></ToastProvider>;
    render(strict ? <StrictMode>{app}</StrictMode> : app);
    expect(await screen.findByText("Tell me a story", {}, WAIT)).toBeVisible();
    await waitFor(() => expect(server.calls("/generations/job-a/events")).toHaveLength(1), WAIT);
    const user = userEvent.setup();
    return user;
  };

  const openSettings = async (user: ReturnType<typeof userEvent.setup>) => {
    await user.click(screen.getByRole("link", { name: "Settings" }));
    expect(await screen.findByRole("heading", { name: "Settings", level: 2 }, WAIT)).toBeVisible();
  };

  const partialContent = () => useChatStore.getState().generation.partialContent;

  it("keeps streaming into the store while Settings is open", async () => {
    const server = backend();
    const user = await openRunningChat(server);

    await server.stream.push({ event_id: 1, event: "generation.content_delta", data: { delta: "Once upon" } });
    await waitFor(() => expect(partialContent()).toBe("Once upon"), WAIT);

    await openSettings(user);
    await server.stream.push({ event_id: 2, event: "generation.content_delta", data: { delta: " a time," } });
    await waitFor(() => expect(partialContent()).toBe("Once upon a time,"), WAIT);

    // The same connection carried it: leaving the chat did not reconnect.
    expect(server.calls("/generations/job-a/events")).toHaveLength(1);
    expect(useChatStore.getState().generationCursor).toBe(2);

    // The job ends while Settings is still up. The chat and the sidebar are
    // reconciled without waiting for the user to come back.
    server.saved.title = "A story";
    server.saved.messages = [USER_TURN, ANSWER];
    await server.stream.push({ event_id: 3, event: "generation.completed" });
    await server.stream.close();
    await waitFor(() => expect(useChatStore.getState().generation.jobId).toBeNull(), WAIT);
    expect(useChatStore.getState().chats.find((chat) => chat.id === "thread-a")?.title).toBe("A story");
    expect(screen.getByRole("heading", { name: "Settings", level: 2 })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Close settings" }));
    expect(await screen.findByText("Once upon a time, the end.", {}, WAIT)).toBeVisible();
    expect(screen.queryByLabelText(/cortex response in progress/i)).not.toBeInTheDocument();
    expect(server.calls("/generations/job-a/events")).toHaveLength(1);
  });

  it("holds one consumer for the job under StrictMode, through Settings and back", async () => {
    // The app is mounted, unmounted and mounted again on the same instance in
    // development. That must leave exactly one live connection to the job, and
    // it has to be the one that carries the text.
    const server = backend();
    const user = await openRunningChat(server, { strict: true });
    await server.stream.push({ event_id: 1, event: "generation.content_delta", data: { delta: "Once upon" } });
    await waitFor(() => expect(partialContent()).toBe("Once upon"), WAIT);
    expect(server.calls("/generations/job-a/events")).toHaveLength(1);

    await openSettings(user);
    await server.stream.push({ event_id: 2, event: "generation.content_delta", data: { delta: " a time" } });
    await waitFor(() => expect(partialContent()).toBe("Once upon a time"), WAIT);
    await user.click(screen.getByRole("button", { name: "Close settings" }));
    await screen.findByLabelText("Message Cortex", {}, WAIT);

    expect(server.calls("/generations/job-a/events")).toHaveLength(1);
    expect(useChatStore.getState().generation.gap).toBe(false);
    expect(partialContent()).toBe("Once upon a time");
  });

  it("hands the text streamed while away to the bubble on return, without a hole", async () => {
    const server = backend();
    const user = await openRunningChat(server);
    await server.stream.push({ event_id: 1, event: "generation.content_delta", data: { delta: "Once upon" } });
    await waitFor(() => expect(partialContent()).toBe("Once upon"), WAIT);

    await openSettings(user);
    await server.stream.push({ event_id: 2, event: "generation.content_delta", data: { delta: " a time," } });
    await server.stream.push({ event_id: 3, event: "generation.content_delta", data: { delta: " there was" } });
    await waitFor(() => expect(partialContent()).toBe("Once upon a time, there was"), WAIT);

    await user.click(screen.getByRole("button", { name: "Close settings" }));

    // Every token, in order, nothing repeated -- and no notice about a gap.
    expect(await screen.findByText("Once upon a time, there was", {}, WAIT)).toBeVisible();
    expect(screen.queryByText(/part of this answer was missed/i)).not.toBeInTheDocument();
    expect(useChatStore.getState().generation.gap).toBe(false);
  });

  it("shows a failure that arrived while Settings was open when the chat returns, once", async () => {
    const server = backend();
    const user = await openRunningChat(server);
    await openSettings(user);

    await server.stream.push({ event_id: 1, event: "generation.failed", data: { message: "The model stopped unexpectedly." } });
    await server.stream.close();
    await waitFor(() => expect(useChatStore.getState().generation.jobId).toBeNull(), WAIT);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Close settings" }));

    expect(await screen.findByRole("alert", {}, WAIT)).toHaveTextContent("The model stopped unexpectedly.");
    // Shown once: leaving and returning does not bring a dismissed error back.
    await user.click(screen.getByRole("button", { name: /dismiss/i }));
    await openSettings(user);
    await user.click(screen.getByRole("button", { name: "Close settings" }));
    await screen.findByLabelText("Message Cortex", {}, WAIT);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("asks about a memory clear the model requested while Settings was open, once the chat returns", async () => {
    const server = backend();
    const user = await openRunningChat(server);
    await openSettings(user);

    server.saved.messages = [USER_TURN, ANSWER];
    await server.stream.push({ event_id: 1, event: "generation.completed", data: { clear_requested: true } });
    await server.stream.close();
    await waitFor(() => expect(useChatStore.getState().generation.jobId).toBeNull(), WAIT);
    expect(screen.queryByRole("heading", { name: "Clear permanent memories?" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Close settings" }));

    expect(await screen.findByRole("heading", { name: "Clear permanent memories?" }, WAIT)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("heading", { name: "Clear permanent memories?" })).not.toBeInTheDocument());
    // Asked once. Going away and coming back does not ask again.
    await openSettings(user);
    await user.click(screen.getByRole("button", { name: "Close settings" }));
    await screen.findByLabelText("Message Cortex", {}, WAIT);
    expect(screen.queryByRole("heading", { name: "Clear permanent memories?" })).not.toBeInTheDocument();
  });

  it("says the full answer will appear when it is saved when the stream comes back after events were dropped", async () => {
    const server = backend();
    await openRunningChat(server);

    // The backend keeps a bounded tail: the first event this reader can be
    // served is far past the start.
    await server.stream.push({ event_id: 41, event: "generation.content_delta", data: { delta: "the middle of an answer" } });

    expect(await screen.findByText(/full answer will appear when it is saved/i, {}, WAIT)).toBeVisible();
    expect(screen.queryByText("the middle of an answer")).not.toBeInTheDocument();
    expect(partialContent()).toBe("");

    server.saved.messages = [USER_TURN, ANSWER];
    await server.stream.push({ event_id: 42, event: "generation.completed" });
    await server.stream.close();
    expect(await screen.findByText("Once upon a time, the end.", {}, WAIT)).toBeVisible();
    expect(screen.queryByText(/full answer will appear when it is saved/i)).not.toBeInTheDocument();
  });
});
