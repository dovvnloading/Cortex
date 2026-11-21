import { test, expect } from "./fixtures";

test("Agent mode remains off until saved in Settings and never starts without project consent", async ({ page }) => {
  let savedSettings = {
    schema_version: 1 as const,
    revision: 1,
    appearance: { theme: "dark" },
    models: { chat: "qwen3:8b", title: null, translation: "translategemma:4b" },
    agent: { enabled: false },
  };
  const started: unknown[] = [];

  await page.route("**/api/v1/session/exchange", async (route) => {
    await route.fulfill({ json: { session_token: "session-e2e", expires_at: "2099-01-01T00:00:00Z" } });
  });
  await page.route("**/api/v1/system", async (route) => {
    await route.fulfill({ json: { api_version: "v1", status: "ok", preview: true, session_required: true, started_at: "2026-01-01T00:00:00Z" } });
  });
  await page.route("**/api/v1/chats", async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route("**/api/v1/settings", async (route) => {
    if (route.request().method() !== "GET") {
      const submitted = route.request().postDataJSON() as { settings: typeof savedSettings };
      savedSettings = { ...submitted.settings, revision: savedSettings.revision + 1 };
    }
    await route.fulfill({ json: { source: "settings", settings: savedSettings } });
  });
  await page.route("**/api/v1/models", async (route) => {
    await route.fulfill({ json: {
      required_models: [], optional_models: [], installed_models: ["qwen3:8b"], models: [{ name: "qwen3:8b" }],
      connection: { success: true, status: "connected", message: "Connected." },
    } });
  });
  await page.route("**/api/v1/workspace-checkpoints", async (route) => {
    await route.fulfill({ json: { checkpoints: [] } });
  });
  await page.route("**/api/v1/memories", async (route) => {
    await route.fulfill({ json: { memos: [] } });
  });
  await page.route("**/api/v1/agent/runs", async (route) => {
    const method = route.request().method();
    if (method === "GET") {
      await route.fulfill({ json: { runs: [] } });
    } else if (method === "POST") {
      started.push(route.request().postDataJSON());
      await route.fulfill({ status: 500, json: { detail: "Run should not have started without consent" } });
    } else {
      throw new Error(`Unexpected /agent/runs request method: ${method}`);
    }
  });

  await page.goto("/#bootstrap=launcher-token");
  await expect(page.getByLabel("Message Cortex")).toBeVisible();
  await page.getByRole("link", { name: "Agent mode" }).first().click();
  await expect(page.getByRole("heading", { name: "Agent mode is off" })).toBeVisible();

  await page.getByRole("link", { name: "Open Settings" }).click();
  await page.getByRole("checkbox", { name: "Enable Agent mode" }).check();
  await page.getByRole("button", { name: "Save settings" }).click();
  await expect.poll(() => savedSettings.agent.enabled).toBe(true);
  await page.getByRole("link", { name: "Agent mode" }).first().click();
  await expect(page.getByRole("heading", { name: "Agent mode", level: 2 })).toBeVisible();
  await page.getByRole("button", { name: "Fix failing tests" }).click();
  await page.getByRole("textbox", { name: "Project folder" }).fill("C:\\dev\\sample");
  await expect(page.getByRole("button", { name: "Start Agent" })).toBeDisabled();
  expect(started).toEqual([]);

  // The prior Project workspace route is still reachable.
  await page.getByRole("link", { name: "Project workspace" }).first().click();
  await expect(page.getByRole("heading", { name: "Project workspace", level: 2 })).toBeVisible();
});
