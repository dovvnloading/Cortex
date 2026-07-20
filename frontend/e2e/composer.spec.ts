import { expect, test, type Page } from "@playwright/test";

type WorkspaceOptions = {
  models?: string[];
};

async function stubWorkspace(page: Page, { models = ["local-chat:7b", "local-chat:13b"] }: WorkspaceOptions = {}) {
  let settings = {
    appearance: { theme: "dark" },
    models: { chat: models[0] ?? null, title: null, translation: "translategemma:4b" },
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
        models: models.map((name) => ({ name })),
        connection: { success: true, status: "connected", message: "Connected to local runtime." },
      },
    });
  });
}

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
