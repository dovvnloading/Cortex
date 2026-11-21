import { test, expect } from "./fixtures";

test("Agent mode remains opt-in and exposes confirmed legacy recovery after retiring Project workspace", async ({ page }) => {
  let savedSettings = {
    schema_version: 1 as const,
    revision: 1,
    appearance: { theme: "dark" },
    models: { chat: "qwen3:8b", title: null, translation: "translategemma:4b" },
    agent: { enabled: false },
  };
  const started: unknown[] = [];
  const restored: string[] = [];
  const discarded: string[] = [];
  let checkpoints = [
    { id: "legacy-pending", root: "C:\\dev\\old-project", state: "pending",
      created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:05:00Z",
      paths: ["app.py"], file_count: 1 },
    { id: "legacy-active", root: "C:\\dev\\another-project", state: "active",
      created_at: "2026-01-02T00:00:00Z", updated_at: "2026-01-02T00:05:00Z",
      paths: ["notes.txt"], file_count: 1 },
  ];

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
    await route.fulfill({ json: { checkpoints } });
  });
  await page.route("**/api/v1/workspace-checkpoints/*/restore", async (route) => {
    const id = route.request().url().split("/").at(-2)!;
    const payload = route.request().postDataJSON() as { confirmed: boolean };
    if (payload.confirmed !== true) throw new Error("Restore must be confirmed.");
    restored.push(id);
    checkpoints = checkpoints.filter((item) => item.id !== id);
    await route.fulfill({ json: { status: "restored", checkpoint_id: id, paths: ["app.py"] } });
  });
  await page.route("**/api/v1/workspace-checkpoints/*/discard", async (route) => {
    const id = route.request().url().split("/").at(-2)!;
    const payload = route.request().postDataJSON() as { confirmed: boolean };
    if (payload.confirmed !== true) throw new Error("Discard must be confirmed.");
    discarded.push(id);
    checkpoints = checkpoints.filter((item) => item.id !== id);
    await route.fulfill({ json: { discarded: true } });
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
      throw new Error("Unexpected /agent/runs request method: " + method);
    }
  });

  await page.goto("/#bootstrap=launcher-token");
  await expect(page.getByLabel("Message Cortex")).toBeVisible();
  await page.getByRole("link", { name: "Agent mode" }).first().click();
  await expect(page.getByRole("heading", { name: "Agent mode is off" })).toBeVisible();
  // Saved checkpoints remain reachable before enabling new Agent executions.
  await expect(page.getByRole("heading", { name: "Saved recovery checkpoints" })).toBeVisible();
  await expect(page.getByText("C:\\dev\\old-project")).toBeVisible();

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

  // Old navigation is gone; legacy checkpoints remain independently recoverable.
  await expect(page.getByRole("link", { name: "Project workspace" })).toHaveCount(0);
  page.once("dialog", (dialog) => { void dialog.dismiss(); });
  await page.getByRole("button", { name: "Restore originals" }).first().click();
  expect(restored).toEqual([]);

  page.once("dialog", (dialog) => { void dialog.accept(); });
  await page.getByRole("button", { name: "Restore originals" }).first().click();
  await expect.poll(() => restored.slice()).toEqual(["legacy-pending"]);
  await expect(page.getByText("C:\\dev\\old-project")).toHaveCount(0);
  await expect(page.getByRole("status")).toContainText("Restored 1 source files");

  page.once("dialog", (dialog) => { void dialog.accept(); });
  await page.getByRole("button", { name: "Discard backup" }).click();
  await expect.poll(() => discarded.slice()).toEqual(["legacy-active"]);
  await expect(page.getByText("No saved recovery checkpoints.")).toBeVisible();
  expect(started).toEqual([]);

  // Existing bookmarks now resolve to Agent mode instead of a dead page.
  await page.goto("/workspace#bootstrap=launcher-token");
  await expect(page).toHaveURL(/\/agent$/);
  await expect(page.getByRole("heading", { name: "Agent mode", level: 2 })).toBeVisible();
});
