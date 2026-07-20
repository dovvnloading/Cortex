import { test, expect } from "@playwright/test";

test("completes a streamed new-chat parity flow", async ({ page }) => {
  const threadId = "thread-e2e";
  const jobId = "job-e2e";
  let chatLoaded = false;
  let shutdownRequested = false;

  await page.route("**/api/v1/session/exchange", async (route) => {
    await route.fulfill({ json: { session_token: "session-e2e", expires_at: "2099-01-01T00:00:00Z" } });
  });
  await page.route("**/api/v1/system/shutdown", async (route) => {
    shutdownRequested = true;
    await route.fulfill({ json: { status: "accepted" } });
  });
  await page.route("**/api/v1/system", async (route) => {
    await route.fulfill({ json: { api_version: "v1", status: "ok", preview: true, qt_default: true, session_required: true, started_at: "2026-01-01T00:00:00Z" } });
  });
  await page.route("**/api/v1/chats", async (route) => {
    if (route.request().method() === "GET") await route.fulfill({ json: chatLoaded ? [{ id: threadId, title: "hello", timestamp: "2026-01-01T00:00:00Z" }] : [] });
    else await route.continue();
  });
  await page.route(`**/api/v1/chats/${threadId}`, async (route) => {
    chatLoaded = true;
    await route.fulfill({ json: { id: threadId, title: "hello", timestamp: "2026-01-01T00:00:00Z", revision: 2, messages: [{ id: "m-1", role: "user", content: "hello" }, { id: "m-2", role: "assistant", content: "Echo: hello" }] } });
  });
  await page.route("**/api/v1/settings", async (route) => {
    await route.fulfill({ json: { source: "defaults", settings: { appearance: { theme: "dark" }, generation: { temperature: 0.7, num_ctx: 4096, seed: -1 }, memory: { enabled: true }, translation: { enabled: false }, suggestions: { enabled: true } } } });
  });
  await page.route("**/api/v1/memories", async (route) => {
    await route.fulfill({ json: { memos: [] } });
  });
  await page.route("**/api/v1/models", async (route) => {
    await route.fulfill({ json: { required_models: ["qwen3:8b"], optional_models: [], installed_models: ["qwen3:8b"], missing_models: [], optional_missing_models: [], connection: { success: true, status: "connected", message: "Connected to Ollama." } } });
  });
  await page.route("**/api/v1/generations", async (route) => {
    await route.fulfill({ status: 202, json: { job_id: jobId, kind: "generation", status: "queued", thread_id: threadId, user_message_id: "m-1" } });
  });
  await page.route(`**/api/v1/generations/${jobId}/events`, async (route) => {
    const events = [
      'id: 1\nevent: generation.queued\ndata: {"event_id":1,"event":"generation.queued","job_id":"job-e2e","thread_id":"thread-e2e","data":{"message":"Queued"}}\n\n',
      'id: 2\nevent: generation.content_delta\ndata: {"event_id":2,"event":"generation.content_delta","job_id":"job-e2e","thread_id":"thread-e2e","data":{"delta":"Echo: hello"}}\n\n',
      'id: 3\nevent: generation.completed\ndata: {"event_id":3,"event":"generation.completed","job_id":"job-e2e","thread_id":"thread-e2e","data":{"suggestions":["Ask a follow-up question"]}}\n\n',
    ].join("");
    await route.fulfill({ status: 200, contentType: "text/event-stream", body: events });
  });

  await page.goto("/?bootstrap=launcher-token");
  await page.getByLabel("Launcher token").fill("launcher-token");
  await page.getByRole("button", { name: "Open workspace" }).click();
  await expect(page.getByLabel("Message Cortex")).toBeVisible();
  await page.getByLabel("Message Cortex").fill("hello");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("Echo: hello")).toBeVisible();
  await expect(page.getByRole("button", { name: "Ask a follow-up question" })).toBeVisible();
  page.once("dialog", (dialog) => void dialog.accept());
  await page.getByRole("button", { name: "Quit Cortex" }).first().click();
  await expect.poll(() => shutdownRequested).toBe(true);
});

test("supports retry, regenerate, and fork without losing the persisted thread", async ({ page }) => {
  const threadId = "thread-controls";
  let generationAttempt = 0;
  let chatState: "user" | "assistant" | "regenerated" = "user";

  await page.route("**/api/v1/session/exchange", async (route) => {
    await route.fulfill({ json: { session_token: "session-e2e", expires_at: "2099-01-01T00:00:00Z" } });
  });
  await page.route("**/api/v1/system", async (route) => {
    await route.fulfill({ json: { api_version: "v1", status: "ok", preview: true, qt_default: true, session_required: true, started_at: "2026-01-01T00:00:00Z" } });
  });
  await page.route("**/api/v1/chats", async (route) => {
    if (route.request().method() === "GET") await route.fulfill({ json: [] });
    else await route.continue();
  });
  await page.route(`**/api/v1/chats/${threadId}`, async (route) => {
    const messages = [{ id: "m-1", role: "user", content: "fail" }];
    if (chatState !== "user") messages.push({ id: "m-2", role: "assistant", content: chatState === "regenerated" ? "Echo: regenerated" : "Echo: fail" });
    await route.fulfill({ json: { id: threadId, title: "fail", timestamp: "2026-01-01T00:00:00Z", revision: messages.length, messages } });
  });
  await page.route("**/api/v1/chats/fork-e2e", async (route) => {
    await route.fulfill({ json: { id: "fork-e2e", title: "Fork of fail", timestamp: "2026-01-01T00:00:00Z", revision: 2, messages: [{ id: "fm-1", role: "user", content: "fail" }, { id: "fm-2", role: "assistant", content: "Echo: fail" }] } });
  });
  await page.route("**/api/v1/chats/thread-controls/regenerations", async (route) => {
    await route.fulfill({ status: 202, json: { job_id: "job-regen", kind: "generation", status: "queued", thread_id: threadId } });
  });
  await page.route("**/api/v1/chats/thread-controls/forks", async (route) => {
    await route.fulfill({ status: 201, json: { id: "fork-e2e", title: "Fork of fail", timestamp: "2026-01-01T00:00:00Z", revision: 2, messages: [{ id: "fm-1", role: "user", content: "fail" }, { id: "fm-2", role: "assistant", content: "Echo: fail" }] } });
  });
  await page.route("**/api/v1/settings", async (route) => {
    await route.fulfill({ json: { source: "defaults", settings: { appearance: { theme: "dark" }, generation: { temperature: 0.7, num_ctx: 4096, seed: -1 }, memory: { enabled: true }, translation: { enabled: false }, suggestions: { enabled: true } } } });
  });
  await page.route("**/api/v1/memories", async (route) => {
    await route.fulfill({ json: { memos: [] } });
  });
  await page.route("**/api/v1/models", async (route) => {
    await route.fulfill({ json: { required_models: ["qwen3:8b"], optional_models: [], installed_models: ["qwen3:8b"], missing_models: [], optional_missing_models: [], connection: { success: true, status: "connected", message: "Connected to Ollama." } } });
  });
  await page.route("**/api/v1/generations", async (route) => {
    generationAttempt += 1;
    await route.fulfill({ status: 202, json: { job_id: `job-${generationAttempt}`, kind: "generation", status: "queued", thread_id: threadId, user_message_id: "m-1" } });
  });
  await page.route("**/api/v1/generations/*/events", async (route) => {
    const jobId = route.request().url().split("/").at(-2);
    const failed = jobId === "job-1";
    const regenerated = jobId === "job-regen";
    const event = failed
      ? `id: 1\nevent: generation.failed\ndata: {"event_id":1,"event":"generation.failed","job_id":"job-1","thread_id":"thread-controls","data":{"message":"Generation failed. Please try again."}}\n\n`
      : `id: 1\nevent: generation.content_delta\ndata: {"event_id":1,"event":"generation.content_delta","job_id":"${jobId}","thread_id":"thread-controls","data":{"delta":"${regenerated ? "Echo: regenerated" : "Echo: fail"}"}}\n\nid: 2\nevent: generation.completed\ndata: {"event_id":2,"event":"generation.completed","job_id":"${jobId}","thread_id":"thread-controls","data":{}}\n\n`;
    if (!failed) chatState = regenerated ? "regenerated" : "assistant";
    await route.fulfill({ status: 200, contentType: "text/event-stream", body: event });
  });

  await page.goto("/?bootstrap=launcher-token");
  await page.getByLabel("Launcher token").fill("launcher-token");
  await page.getByRole("button", { name: "Open workspace" }).click();
  await page.getByLabel("Message Cortex").fill("fail");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByRole("alert")).toContainText("Generation failed");
  await page.getByRole("button", { name: "Retry" }).click();
  await expect(page.getByText("Echo: fail")).toBeVisible();
  await page.getByRole("button", { name: "Regenerate response" }).click();
  await expect(page.getByText("Echo: regenerated")).toBeVisible();
  await page.getByRole("button", { name: "Fork chat from this message" }).click();
  await expect(page.getByRole("heading", { name: "Fork of fail" })).toBeVisible();
});

test("manages settings, permanent memory, and model pull progress", async ({ page }) => {
  let memos = ["Remember tea"];
  let settings = { appearance: { theme: "dark" }, models: { chat: "qwen3:8b", title: "granite4:tiny-h", translation: "translategemma:4b" }, generation: { temperature: 0.7, num_ctx: 4096, seed: -1, system_instructions: "" }, memory: { enabled: true }, translation: { enabled: false, target_language: "Spanish" }, suggestions: { enabled: true, model: "qwen3:8b" } };

  await page.route("**/api/v1/session/exchange", async (route) => {
    await route.fulfill({ json: { session_token: "session-system", expires_at: "2099-01-01T00:00:00Z" } });
  });
  await page.route("**/api/v1/system", async (route) => {
    await route.fulfill({ json: { api_version: "v1", status: "ok", preview: true, qt_default: true, session_required: true, started_at: "2026-01-01T00:00:00Z", ollama_setup_url: "https://ollama.com/download" } });
  });
  await page.route("**/api/v1/chats", async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route("**/api/v1/settings", async (route) => {
    if (route.request().method() === "PUT") {
      settings = (await route.request().postDataJSON()).settings;
    }
    await route.fulfill({ json: { source: "sqlite", settings, present_keys: [], invalid_keys: [] } });
  });
  await page.route("**/api/v1/memories", async (route) => {
    if (route.request().method() === "POST") memos = [...memos, (await route.request().postDataJSON()).memo];
    if (route.request().method() === "PUT") memos = (await route.request().postDataJSON()).memos;
    await route.fulfill({ json: { memos } });
  });
  await page.route("**/api/v1/memories/clear", async (route) => {
    expect((await route.request().postDataJSON()).confirmation_intent).toBe("clear_permanent_memory");
    memos = [];
    await route.fulfill({ json: { memos } });
  });
  await page.route("**/api/v1/models", async (route) => {
    await route.fulfill({ json: { required_models: ["qwen3:8b"], optional_models: [], installed_models: ["qwen3:8b"], missing_models: [], optional_missing_models: [], connection: { success: true, status: "connected", message: "Connected to Ollama." } } });
  });
  await page.route("**/api/v1/models/pulls", async (route) => {
    await route.fulfill({ status: 202, json: { job_id: "job-model", kind: "models", status: "queued" } });
  });
  await page.route("**/api/v1/jobs/job-model/events", async (route) => {
    await route.fulfill({ status: 200, contentType: "text/event-stream", body: 'id: 1\nevent: progress\ndata: {"id":1,"job_id":"job-model","kind":"progress","status":"running","phase":"model_pull","data":{"model":"nemotron-3-nano:4b","message":"pulling","percent":50}}\n\nid: 2\nevent: completed\ndata: {"id":2,"job_id":"job-model","kind":"completed","status":"succeeded","data":{}}\n\n' });
  });

  await page.goto("/?bootstrap=launcher-token");
  await page.getByLabel("Launcher token").fill("launcher-token");
  await page.getByRole("button", { name: "Open workspace" }).click();
  await page.getByRole("link", { name: "Settings" }).click();
  await expect(page.getByRole("heading", { name: "Models and connectivity" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Memory 1" })).toHaveValue("Remember tea");
  await page.getByLabel("System instructions").fill("Be concise.");
  await page.getByRole("button", { name: "Save settings" }).click();
  await expect(page.getByText("Settings saved.")).toBeVisible();
  page.once("dialog", (dialog) => void dialog.accept());
  await page.getByRole("button", { name: "Clear all" }).click();
  await expect(page.getByText("Permanent memories cleared.")).toBeVisible();
  await page.getByLabel("New memory").fill("Remember local data");
  await page.getByRole("button", { name: "Add memory" }).click();
  await expect(page.getByText("Memory saved.")).toBeVisible();
  await page.getByRole("button", { name: "Remove memory 1" }).click();
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByText("Memory changes saved.")).toBeVisible();
  await page.getByLabel("Pull an exact model tag").fill("nemotron-3-nano:4b");
  await page.getByRole("button", { name: "Pull model" }).click();
  await expect(page.getByText("50%")).toBeVisible();
});
