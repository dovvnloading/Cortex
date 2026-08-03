import { expect, test, type Page } from "@playwright/test";

type WorkspaceOptions = {
  models?: string[];
  selectedModel?: string | null;
  connectionSuccess?: boolean;
  connectionMessage?: string;
};

async function stubWorkspace(page: Page, {
  models = ["local-chat:7b", "local-chat:13b"],
  selectedModel = models[0] ?? null,
  connectionSuccess = true,
  connectionMessage = "Connected to local runtime.",
}: WorkspaceOptions = {}) {
  let settings = {
    appearance: { theme: "dark" },
    models: { chat: selectedModel, title: null, translation: "translategemma:4b" },
    generation: { temperature: 0.7, num_ctx: 4096, seed: -1 },
    memory: { enabled: true },
    translation: { enabled: false },
    suggestions: { enabled: false, model: null },
  };

  await page.route("**/api/v1/session/exchange", async (route) => {
    await route.fulfill({ json: { session_token: "session-composer", expires_at: "2099-01-01T00:00:00Z" } });
  });
  await page.route("**/api/v1/system", async (route) => {
    await route.fulfill({ json: { api_version: "v1", status: "ok", preview: true, session_required: true, started_at: "2026-01-01T00:00:00Z" } });
  });
  await page.route("**/api/v1/chats", async (route) => {
    if (route.request().method() === "GET") await route.fulfill({ json: [] });
    else await route.continue();
  });
  await page.route("**/api/v1/settings", async (route) => {
    if (route.request().method() === "PUT") settings = (await route.request().postDataJSON()).settings;
    await route.fulfill({ json: { source: "sqlite", settings, present_keys: [], invalid_keys: [] } });
  });
  await page.route("**/api/v1/memories", async (route) => {
    await route.fulfill({ json: { memos: [] } });
  });
  await page.route("**/api/v1/models", async (route) => {
    await route.fulfill({
      json: {
        required_models: [], optional_models: [], installed_models: models,
        missing_models: [], optional_missing_models: [],
        models: models.map((name) => ({ name, supports_vision: name.includes("vision") })),
        connection: { success: connectionSuccess, status: connectionSuccess ? "connected" : "error", message: connectionMessage },
      },
    });
  });
}

test("keeps the workspace and composer picker usable when the inventory is taller than the viewport", async ({ page }) => {
  const models = [
    "gemma4:12b",
    "granite4.1:8b",
    "igors/gemma-4-EB4-it-heretic-GGUF:latest",
    "ministral-3:8b",
    "mxbai-embed-large:latest",
    "nemotron-3-nano:4b",
    "nomic-embed-text:latest",
    "qwen3:8b",
    "qwen3.5:9b",
    "rnj-1:latest",
    "translategemma:4b",
  ];

  await page.setViewportSize({ width: 800, height: 500 });
  await stubWorkspace(page, { models, selectedModel: null });
  await page.goto("/?bootstrap=launcher-token");

  await expect(page.getByRole("heading", { name: "New thread" })).toBeVisible();
  await expect(page.getByLabel("Message Cortex")).toBeVisible();
  await page.getByRole("button", { name: "Select a local model" }).click();
  await expect(page.getByRole("listbox", { name: "Discovered local models" })).toBeVisible();

  const layout = await page.evaluate(() => {
    const listNode = document.querySelector<HTMLElement>(".local-model-menu-list");
    return {
      listScrolls: Boolean(listNode && listNode.scrollHeight > listNode.clientHeight),
      listBottom: listNode?.getBoundingClientRect().bottom ?? 0,
      viewportHeight: window.innerHeight,
    };
  });
  expect(layout.listScrolls).toBe(true);
  expect(layout.listBottom).toBeLessThanOrEqual(layout.viewportHeight);

  await page.getByRole("option", { name: /translategemma:4b/i }).click();
  await expect(page.getByRole("button", { name: "Selected local model: translategemma:4b" })).toBeVisible();
});

test("keeps a multiline draft when generation acceptance fails", async ({ page }) => {
  let generationRequests = 0;
  let submittedInput = "";
  await stubWorkspace(page);
  await page.route("**/api/v1/generations", async (route) => {
    generationRequests += 1;
    submittedInput = (await route.request().postDataJSON()).user_input;
    await route.fulfill({ status: 503, json: { detail: "Local runtime is unavailable." } });
  });

  await page.goto("/?bootstrap=launcher-token");
  const composer = page.getByLabel("Message Cortex");
  await expect(composer).toBeVisible();

  await composer.fill("First line");
  await composer.press("Shift+Enter");
  await composer.type("Second line");
  await composer.press("Enter");

  await expect.poll(() => generationRequests).toBe(1);
  expect(submittedInput).toBe("First line\nSecond line");
  await expect(page.getByRole("alert")).toContainText("Local runtime is unavailable.");
  await expect(composer).toHaveValue("First line\nSecond line");
  await expect(composer).toBeFocused();
});

test("selects and persists a model from the composer picker", async ({ page }) => {
  await stubWorkspace(page);
  await page.goto("/?bootstrap=launcher-token");

  const trigger = page.getByRole("button", { name: "Selected local model: local-chat:7b" });
  await expect(trigger).toBeVisible();
  await trigger.click();

  const option = page.getByRole("option", { name: "local-chat:13b" });
  await expect(option).toBeVisible();
  const overflow = await page.evaluate(() => ({
    toolbar: getComputedStyle(document.querySelector(".composer-toolbar-leading")!).overflow,
    modelControl: getComputedStyle(document.querySelector(".composer-model-control")!).overflow,
  }));
  expect(overflow).toEqual({ toolbar: "visible", modelControl: "visible" });

  await option.click();
  await expect(page.getByRole("button", { name: "Selected local model: local-chat:13b" })).toBeVisible();
  await expect(page.getByText("local-chat:13b is ready for local chat.")).toBeVisible();
});

test("keeps composer model options inside a compact viewport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await stubWorkspace(page);
  await page.goto("/?bootstrap=launcher-token");

  const trigger = page.getByRole("button", { name: "Selected local model: local-chat:7b" });
  await expect(trigger).toBeVisible();
  await trigger.click();
  const option = page.getByRole("option", { name: "local-chat:13b" });
  await expect(option).toBeVisible();
  const optionBox = await option.boundingBox();
  expect(optionBox).not.toBeNull();
  expect(optionBox!.x).toBeGreaterThanOrEqual(0);
  expect(optionBox!.x + optionBox!.width).toBeLessThanOrEqual(390);

  await option.click();
  await expect(page.getByRole("button", { name: "Selected local model: local-chat:13b" })).toBeVisible();
});

test("keeps a next draft available while a response is stopped", async ({ page }) => {
  const threadId = "thread-composer";
  const jobId = "job-composer";
  let generationRequests = 0;
  let cancelRequests = 0;
  let releaseEvents!: () => void;
  const eventsReady = new Promise<void>((resolve) => { releaseEvents = resolve; });

  await stubWorkspace(page);
  await page.route(`**/api/v1/chats/${threadId}`, async (route) => {
    await route.fulfill({ json: { id: threadId, title: "Composer", timestamp: "2026-01-01T00:00:00Z", revision: 1, messages: [{ id: "m-1", role: "user", content: "Start a response" }] } });
  });
  await page.route("**/api/v1/generations", async (route) => {
    generationRequests += 1;
    await route.fulfill({ status: 202, json: { job_id: jobId, kind: "generation", status: "queued", thread_id: threadId, user_message_id: "m-1" } });
  });
  await page.route(`**/api/v1/generations/${jobId}/cancel`, async (route) => {
    cancelRequests += 1;
    await route.fulfill({ json: { job_id: jobId, kind: "generation", status: "cancelling", thread_id: threadId, sequence: 2 } });
  });
  await page.route(`**/api/v1/generations/${jobId}/events`, async (route) => {
    await eventsReady;
    await route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: [
        `id: 1\nevent: generation.queued\ndata: {"event_id":1,"event":"generation.queued","job_id":"${jobId}","thread_id":"${threadId}","data":{"message":"Queued"}}\n\n`,
        `id: 2\nevent: generation.cancelled\ndata: {"event_id":2,"event":"generation.cancelled","job_id":"${jobId}","thread_id":"${threadId}","data":{"message":"Response stopped."}}\n\n`,
      ].join(""),
    });
  });

  await page.goto("/?bootstrap=launcher-token");
  const composer = page.getByLabel("Message Cortex");
  await composer.fill("Start a response");
  await composer.press("Enter");
  await expect(page.getByRole("button", { name: "Stop generating" })).toBeVisible();

  await composer.fill("Prepared after this response");
  await composer.press("Enter");
  await expect(composer).toHaveValue("Prepared after this response\n");
  expect(generationRequests).toBe(1);

  await page.getByRole("button", { name: "Stop generating" }).click();
  await expect.poll(() => cancelRequests).toBe(1);
  releaseEvents();

  await expect(page.getByRole("alert")).toContainText("Response stopped.");
  await expect(page.getByRole("button", { name: "Send message" })).toBeEnabled();
  await expect(composer).toHaveValue("Prepared after this response\n");
});

test.describe("compact window", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test("keeps the composer usable without horizontal overflow", async ({ page }) => {
    await stubWorkspace(page, { models: ["local-chat:7b"] });
    await page.goto("/?bootstrap=launcher-token");
    await expect(page.getByRole("button", { name: "Show chat history" })).toBeVisible();

    const composer = page.getByLabel("Message Cortex");
    await expect(composer).toBeVisible();
    await composer.fill(Array.from({ length: 10 }, (_, index) => `Line ${index + 1}`).join("\n"));

    await expect(page.getByRole("button", { name: "Send message" })).toBeVisible();
    const layout = await page.evaluate(() => ({
      documentWidth: document.documentElement.scrollWidth,
      viewportWidth: window.innerWidth,
      composerHeight: (document.querySelector("#chat-composer") as HTMLTextAreaElement).clientHeight,
      composerScrollHeight: (document.querySelector("#chat-composer") as HTMLTextAreaElement).scrollHeight,
    }));
    expect(layout.documentWidth).toBeLessThanOrEqual(layout.viewportWidth);
    expect(layout.composerHeight).toBeLessThan(layout.composerScrollHeight);
  });
});

test("clips a long runtime status before the send control", async ({ page }) => {
  await stubWorkspace(page, {
    models: ["local-chat:7b"],
    connectionSuccess: false,
    connectionMessage: "Could not connect to Ollama. Please start Ollama and retry this local runtime connection.",
  });
  await page.goto("/?bootstrap=launcher-token");

  const surface = page.locator(".composer-surface");
  const meta = page.locator(".composer-toolbar-trailing");
  const status = page.locator(".composer-status");
  const send = page.getByRole("button", { name: "Send message" });
  await expect(surface).toBeVisible();
  await expect(meta).toContainText("Could not connect to Ollama.");

  const statusBox = await status.boundingBox();
  const sendBox = await send.boundingBox();
  expect(statusBox).not.toBeNull();
  expect(sendBox).not.toBeNull();
  expect(statusBox!.x + statusBox!.width).toBeLessThanOrEqual(sendBox!.x - 6);
});

test("stages a document as a composer chip and sends only its opaque metadata", async ({ page }) => {
  let stagedRequests = 0;
  let generationPayload: Record<string, unknown> | null = null;
  await stubWorkspace(page);
  await page.route("**/api/v1/attachments", async (route) => {
    stagedRequests += 1;
    await route.fulfill({
      status: 201,
      json: {
        attachment_id: "attachment-doc-1",
        filename: "notes.md",
        mime_type: "text/markdown",
        size: 15,
        sha256: "a".repeat(64),
        kind: "document",
        expires_at: "2099-01-01T00:00:00Z",
      },
    });
  });
  await page.route("**/api/v1/generations", async (route) => {
    generationPayload = await route.request().postDataJSON();
    await route.fulfill({ status: 503, json: { detail: "Local runtime is unavailable." } });
  });

  await page.goto("/?bootstrap=launcher-token");
  await page.getByLabel("Attach images or documents").setInputFiles({
    name: "notes.md",
    mimeType: "text/markdown",
    buffer: Buffer.from("private document"),
  });

  await expect.poll(() => stagedRequests).toBe(1);
  await expect(page.getByText("notes.md")).toBeVisible();
  await expect(page.getByLabel("Message Cortex")).toHaveValue("");
  await page.getByRole("button", { name: "Send message" }).click();

  await expect.poll(() => generationPayload).not.toBeNull();
  expect(generationPayload).toMatchObject({
    user_input: "Please review the attached file(s).",
    attachments: [{ attachment_id: "attachment-doc-1", filename: "notes.md", kind: "document" }],
  });
  expect(JSON.stringify(generationPayload)).not.toContain("private document");
});

test("explains when the selected model cannot accept an image attachment", async ({ page }) => {
  await stubWorkspace(page, { models: ["local-chat:7b"] });
  await page.route("**/api/v1/attachments", async (route) => {
    await route.fulfill({
      status: 201,
      json: {
        attachment_id: "attachment-image-1",
        filename: "photo.png",
        mime_type: "image/png",
        size: 8,
        sha256: "b".repeat(64),
        kind: "image",
        expires_at: "2099-01-01T00:00:00Z",
      },
    });
  });

  await page.goto("/?bootstrap=launcher-token");
  await page.getByLabel("Attach images or documents").setInputFiles({
    name: "photo.png",
    mimeType: "image/png",
    buffer: Buffer.from([137, 80, 78, 71]),
  });

  await expect(page.getByRole("alert")).toContainText("cannot accept images");
  await expect(page.getByRole("button", { name: "Send message" })).toBeDisabled();
});
