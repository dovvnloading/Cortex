import { test, expect } from "./fixtures";

test("does not replay old completions and groups repeated task notifications", async ({ page }) => {
  const task = {
    profile: "chat.attachment.v1",
    status: "succeeded",
    sequence: 2,
    phase: "completed",
    message: "Chat attachment staged.",
    can_cancel: false,
  };

  await page.route("**/api/v1/session/exchange", async (route) => {
    await route.fulfill({ json: { session_token: "session-execution", expires_at: "2099-01-01T00:00:00Z" } });
  });
  await page.route("**/api/v1/system", async (route) => {
    await route.fulfill({ json: {
      api_version: "v1",
      status: "ok",
      preview: true,
      session_required: true,
      execution_preview_available: true,
      started_at: "2026-07-21T18:00:00Z",
    } });
  });
  await page.route("**/api/v1/chats", async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route("**/api/v1/settings", async (route) => {
    await route.fulfill({ json: {
      source: "defaults",
      settings: {
        appearance: { theme: "dark" },
        models: { chat: "local-chat:7b", title: null, translation: "translategemma:4b" },
        generation: { temperature: 0.7, num_ctx: 4096, seed: -1 },
        memory: { enabled: true },
        translation: { enabled: false },
        suggestions: { enabled: true, model: null },
      },
    } });
  });
  await page.route("**/api/v1/memories", async (route) => {
    await route.fulfill({ json: { memos: [] } });
  });
  await page.route("**/api/v1/models", async (route) => {
    await route.fulfill({ json: {
      required_models: [],
      optional_models: [],
      installed_models: ["local-chat:7b"],
      missing_models: [],
      optional_missing_models: [],
      models: [{ name: "local-chat:7b" }],
      connection: { success: true, status: "connected", message: "Connected to local runtime." },
    } });
  });
  await page.route("**/api/v1/execution/tasks**", async (route) => {
    await route.fulfill({ json: {
      tasks: [
        {
          ...task,
          job_id: "current-attachment-1",
          created_at: "2026-07-21T18:00:01Z",
          updated_at: "2026-07-21T18:00:02Z",
        },
        {
          ...task,
          job_id: "current-attachment-2",
          created_at: "2026-07-21T18:00:03Z",
          updated_at: "2026-07-21T18:00:04Z",
        },
        {
          ...task,
          job_id: "old-attachment",
          message: "Old attachment staged.",
          created_at: "2026-07-20T18:00:00Z",
          updated_at: "2026-07-20T18:00:01Z",
        },
      ],
    } });
  });

  await page.goto("/?bootstrap=launcher-token");
  await expect(page.getByLabel("Message Cortex")).toBeVisible();

  const tray = page.getByRole("complementary", { name: "Task activity" });
  await expect(tray).toBeVisible();
  await expect(tray.getByText("2 × Chat attachment staged.")).toBeVisible();
  await expect(tray.getByText("Old attachment staged.")).toHaveCount(0);
  await expect(tray.getByRole("listitem")).toHaveCount(1);
});
