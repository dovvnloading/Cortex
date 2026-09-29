import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { CortexApi } from "../api/client";
import { useModelStore } from "../stores/useModelStore";
import { ToastProvider } from "./ToastProvider";

/**
 * A GGUF download is a job on the backend. These tests drive a whole
 * workspace with hand-fed event streams and check what the person can do with
 * it: cancel it, leave and find it still running, keep choosing a model for
 * chat while it runs, and pick the model out of a repository's file list.
 */

const WAIT = { timeout: 10_000 };
const GIB = 1024 ** 3;
const FILE = "demo.Q4_K_M.gguf";
const JOB_KEY = "cortex.active.model-job";

const respond = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { "Content-Type": "application/json" },
});

/** An event stream the test feeds by hand, so it decides when progress arrives and when the job ends. */
function handFedStream() {
  const encoder = new TextEncoder();
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({ start: (started) => { controller = started; } });
  let connectionLost = false;
  return {
    body,
    abortOn: (signal: AbortSignal | null | undefined) => {
      signal?.addEventListener("abort", () => {
        connectionLost = true;
        controller.error(new DOMException("Aborted", "AbortError"));
      }, { once: true });
    },
    push: (event: Record<string, unknown>) => act(() => {
      if (connectionLost) return;
      const full = { job_id: "gguf-job", phase: null, data: {}, ...event };
      controller.enqueue(encoder.encode(`id: ${String(event.id)}\ndata: ${JSON.stringify(full)}\n\n`));
    }),
    close: () => act(() => {
      if (!connectionLost) controller.close();
    }),
    get lost() { return connectionLost; },
  };
}

const progress = (id: number, completed: number, total: number) => ({
  id,
  kind: "progress",
  status: "running",
  phase: "gguf_download",
  data: { message: "downloading", filename: FILE, completed, total, percent: Math.round((completed / total) * 100) },
});

const LISTING = {
  repo_id: "owner/name",
  files: ["model.Q4_K_M.gguf", "weights/model.Q8_0.gguf"],
  entries: [
    { path: "model.Q4_K_M.gguf", size: 4 * GIB },
    { path: "weights/model.Q8_0.gguf", size: 8 * GIB },
  ],
};

function backend(options: { installed?: string[]; chat?: string | null; listing?: () => Response } = {}) {
  const installed = options.installed ?? [];
  const streams: ReturnType<typeof handFedStream>[] = [];
  const eventRequests: { lastEventId: string | null }[] = [];
  const settingsWrites: { models: { chat: string | null } }[] = [];
  const downloads: Record<string, unknown>[] = [];
  const fetcher = vi.fn<typeof fetch>(async (input, init) => {
    const url = String(input);
    if (url.endsWith("/jobs/gguf-job/events")) {
      const stream = handFedStream();
      stream.abortOn(init?.signal);
      streams.push(stream);
      eventRequests.push({ lastEventId: new Headers(init?.headers).get("Last-Event-ID") });
      return new Response(stream.body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
    }
    if (url.endsWith("/models/gguf/downloads") && init?.method === "POST") {
      downloads.push(JSON.parse(String(init.body)) as Record<string, unknown>);
      return respond({ job_id: "gguf-job", kind: "gguf_download", status: "queued" }, 202);
    }
    if (url.endsWith("/jobs/gguf-job/cancel") && init?.method === "POST") {
      return respond({ job_id: "gguf-job", kind: "gguf_download", status: "cancelling", sequence: 3 });
    }
    if (url.includes("/models/gguf/huggingface-files")) return options.listing?.() ?? respond(LISTING);
    if (url.endsWith("/system")) {
      return respond({
        status: "ok",
        preview: true,
        session_required: true,
        started_at: "2026-07-21T18:00:00Z",
        llamacpp: { state: "idle", binary_present: false, models_directory: "/synthetic/models", models_directory_exists: true },
      });
    }
    if (url.endsWith("/chat-groups")) return respond([]);
    if (url.endsWith("/chats")) return respond([]);
    if (url.endsWith("/settings") && init?.method === "PUT") {
      const body = JSON.parse(String(init.body)) as { settings: { models: { chat: string | null } } };
      settingsWrites.push(body.settings);
      return respond({ settings: body.settings });
    }
    if (url.endsWith("/settings")) return respond({ settings: { models: { chat: options.chat ?? null, title: null }, appearance: { theme: "dark" } } });
    if (url.endsWith("/memories")) return respond({ memos: [] });
    if (url.endsWith("/models")) return respond({ required_models: [], optional_models: [], installed_models: installed, connection: { success: true, status: "connected", message: "Ready" } });
    return respond({ detail: "Unexpected test route." }, 404);
  });
  const calls = (suffix: string) => fetcher.mock.calls.filter(([input]) => String(input).endsWith(suffix));
  return { fetcher, streams, eventRequests, settingsWrites, downloads, calls };
}

type Server = ReturnType<typeof backend>;

describe("a model download", () => {
  beforeAll(async () => {
    await Promise.all([
      import("../features/chat/ChatPage"),
      import("../features/settings/SettingsPanel"),
    ]);
  }, 120_000);

  afterEach(() => {
    window.sessionStorage.clear();
    window.history.replaceState({}, "", "/");
  });

  const mount = (server: Server) => render(
    <ToastProvider><App api={new CortexApi("/api/v1", server.fetcher)} /></ToastProvider>,
  );

  const openApp = async (server: Server) => {
    window.sessionStorage.setItem("cortex.session.token", "local-session");
    const view = mount(server);
    expect(await screen.findByRole("heading", { name: "New thread" }, WAIT)).toBeVisible();
    return { ...view, user: userEvent.setup() };
  };

  /** The workspace opened again where the last one left off, which is Settings. */
  const reopenSettings = async (server: Server) => {
    const view = mount(server);
    expect(await screen.findByRole("button", { name: "System" }, WAIT)).toBeVisible();
    return { ...view, user: userEvent.setup() };
  };

  const openSystem = async (user: ReturnType<typeof userEvent.setup>) => {
    await user.click(screen.getByRole("link", { name: "Settings" }));
    await user.click(await screen.findByRole("button", { name: "System" }, WAIT));
  };

  const startDownload = async (user: ReturnType<typeof userEvent.setup>) => {
    await user.type(screen.getByLabelText(/Repo id/), "owner/name");
    await user.type(screen.getByLabelText(/File name/), FILE);
    await user.click(screen.getByRole("button", { name: /Download model/ }));
  };

  const remembered = () => JSON.parse(window.sessionStorage.getItem(JOB_KEY) ?? "null") as { jobId: string; lastEventId: number } | null;

  it("is picked up again, from the last event shown, after the workspace is remounted", async () => {
    const server = backend();
    const first = await openApp(server);
    await openSystem(first.user);
    await startDownload(first.user);
    await waitFor(() => expect(server.streams).toHaveLength(1), WAIT);
    await server.streams[0].push(progress(2, GIB, 4 * GIB));
    expect(await screen.findByText("Downloading (1.0 GB of 4.0 GB)", {}, WAIT)).toBeVisible();

    first.unmount();

    // The screen is gone, its stream is cut, and the job is still remembered.
    expect(server.streams[0].lost).toBe(true);
    expect(useModelStore.getState().modelBusy).toBe(false);
    expect(remembered()).toMatchObject({ jobId: "gguf-job", lastEventId: 2 });

    const second = await reopenSettings(server);
    await waitFor(() => expect(server.streams).toHaveLength(2), WAIT);
    expect(server.eventRequests[1].lastEventId).toBe("2");
    await second.user.click(await screen.findByRole("button", { name: "System" }, WAIT));
    const row = await screen.findByRole("status", { name: "Model operation progress" }, WAIT);
    expect(row).toHaveAttribute("aria-busy", "true");
    expect(row).toHaveTextContent(FILE);
    expect(screen.getByRole("button", { name: `Cancel ${FILE}` })).toBeEnabled();

    // It ends while the new screen is watching: the model is fetched and chosen,
    // as it would have been had the first screen stayed.
    await server.streams[1].push({ id: 3, kind: "completed", status: "succeeded", data: { filename: FILE } });
    await server.streams[1].close();
    await waitFor(() => expect(server.settingsWrites).toHaveLength(1), WAIT);
    expect(server.settingsWrites[0].models.chat).toBe(`gguf:${FILE}`);
    await waitFor(() => expect(remembered()).toBeNull(), WAIT);
    expect(useModelStore.getState().modelBusy).toBe(false);
  });

  it("is cancelled from the progress row, and the form says so without calling it a failure", async () => {
    const server = backend();
    const { user } = await openApp(server);
    await openSystem(user);
    await startDownload(user);
    await waitFor(() => expect(server.streams).toHaveLength(1), WAIT);
    await server.streams[0].push(progress(2, GIB, 4 * GIB));

    await user.click(await screen.findByRole("button", { name: `Cancel ${FILE}` }, WAIT));

    await waitFor(() => expect(server.calls("/jobs/gguf-job/cancel")).toHaveLength(1), WAIT);
    expect(server.calls("/jobs/gguf-job/cancel")[0][1]?.method).toBe("POST");
    expect(screen.getByRole("button", { name: `Cancel ${FILE}` })).toBeDisabled();
    expect(screen.getByRole("button", { name: `Cancel ${FILE}` })).toHaveTextContent("Cancelling…");

    await server.streams[0].push({ id: 3, kind: "state", status: "cancelled", data: { message: "Job cancelled." } });
    await server.streams[0].close();

    expect((await screen.findAllByText("Download cancelled.", {}, WAIT)).length).toBeGreaterThan(0);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Cancel/ })).not.toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Model operation progress" })).toHaveTextContent("Cancelled.");
    // What was typed is still there to try again, and nothing was chosen.
    expect(screen.getByLabelText(/Repo id/)).toHaveValue("owner/name");
    expect(screen.getByRole("button", { name: "Download model" })).toBeEnabled();
    expect(server.settingsWrites).toHaveLength(0);
    expect(remembered()).toBeNull();
  });

  it("leaves the chat's model picker usable while it runs", async () => {
    const server = backend({ installed: ["gguf:other.gguf"], chat: "gguf:other.gguf" });
    const { user } = await openApp(server);
    await openSystem(user);
    await startDownload(user);
    await waitFor(() => expect(server.streams).toHaveLength(1), WAIT);
    await waitFor(() => expect(useModelStore.getState().modelBusy).toBe(true), WAIT);

    await user.click(screen.getByRole("button", { name: "Close settings" }));

    const picker = await screen.findByRole("button", { name: "Selected local model: other.gguf" }, WAIT);
    expect(useModelStore.getState().modelBusy).toBe(true);
    expect(picker).toBeEnabled();
  });

  it("says why it failed beside the button, in the backend's words", async () => {
    const reason = "This Hugging Face repository is gated or private. Set the HF_TOKEN environment variable.";
    const server = backend();
    const { user } = await openApp(server);
    await openSystem(user);
    await startDownload(user);
    await waitFor(() => expect(server.streams).toHaveLength(1), WAIT);

    await server.streams[0].push({ id: 2, kind: "error", status: "failed", data: { message: reason } });
    await server.streams[0].close();

    expect(await screen.findByRole("alert", {}, WAIT)).toHaveTextContent(reason);
    expect(screen.getByLabelText(/Repo id/)).toHaveValue("owner/name");
    expect(remembered()).toBeNull();
  });

  it("can be chosen from a repository's file list instead of typed", async () => {
    const server = backend();
    const { user } = await openApp(server);
    await openSystem(user);

    await user.type(screen.getByLabelText(/Repo id/), "owner/name");
    await user.click(screen.getByRole("button", { name: /Browse files/ }));
    await user.click(await screen.findByRole("radio", { name: /model\.Q8_0\.gguf.*8\.0 GB/ }, WAIT));
    await user.click(screen.getByRole("button", { name: "Download model" }));

    await waitFor(() => expect(server.downloads).toHaveLength(1), WAIT);
    expect(server.downloads[0]).toEqual({ source: "huggingface", repo_id: "owner/name", filename: "weights/model.Q8_0.gguf" });
    expect(String(server.calls("?repo_id=owner%2Fname")[0]?.[0])).toContain("/models/gguf/huggingface-files");
  });

  it("names why Hugging Face would not list a repository, and lets the file name be typed anyway", async () => {
    const server = backend({
      listing: () => respond({ detail: { message: "This Hugging Face repository is gated or private. Set HF_TOKEN.", code: "gated" } }, 400),
    });
    const { user } = await openApp(server);
    await openSystem(user);

    await user.type(screen.getByLabelText(/Repo id/), "owner/name");
    await user.click(screen.getByRole("button", { name: /Browse files/ }));

    const alert = await screen.findByRole("alert", {}, WAIT);
    expect(alert).toHaveTextContent("gated or private");
    expect(within(alert).getByRole("link", { name: /Open owner\/name on Hugging Face/ })).toHaveAttribute("href", "https://huggingface.co/owner/name");
    expect(screen.getByLabelText(/File name/)).toBeVisible();
  });
});
