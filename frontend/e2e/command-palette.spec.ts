import { test, expect } from "@playwright/test";

test("opens the command palette with Ctrl+K, filters, and navigates to settings", async ({ page }) => {
  const chats = [{ id: "thread-a", title: "Weekend plans", timestamp: "2026-01-01T00:00:00Z" }];

  await page.route("**/api/v1/session/exchange", async (route) => {
    await route.fulfill({ json: { session_token: "session-palette", expires_at: "2099-01-01T00:00:00Z" } });
  });
  await page.route("**/api/v1/system", async (route) => {
    await route.fulfill({ json: { api_version: "v1", status: "ok", preview: true, session_required: true, started_at: "2026-01-01T00:00:00Z" } });
  });
  await page.route("**/api/v1/chats", async (route) => {
    if (route.request().method() === "GET") await route.fulfill({ json: chats });
    else await route.continue();
  });
  await page.route("**/api/v1/chats/thread-a", async (route) => {
    await route.fulfill({ json: { id: "thread-a", title: "Weekend plans", timestamp: "2026-01-01T00:00:00Z", revision: 1, messages: [{ id: "m-1", role: "user", content: "hi" }] } });
  });
  await page.route("**/api/v1/settings", async (route) => {
    await route.fulfill({ json: { source: "defaults", settings: { appearance: { theme: "dark" }, models: { chat: "local-chat:7b", title: null, translation: "translategemma:4b" }, generation: { temperature: 0.7, num_ctx: 4096, seed: -1 }, memory: { enabled: true }, translation: { enabled: false }, suggestions: { enabled: false, model: null } } } });
  });
  await page.route("**/api/v1/memories", async (route) => {
    await route.fulfill({ json: { memos: [] } });
  });
  await page.route("**/api/v1/models", async (route) => {
    await route.fulfill({ json: { required_models: [], optional_models: [], installed_models: ["local-chat:7b"], missing_models: [], optional_missing_models: [], models: [{ name: "local-chat:7b" }], connection: { success: true, status: "connected", message: "Connected to local runtime." } } });
  });

  await page.goto("/chat/thread-a?bootstrap=launcher-token");
  await expect(page.getByLabel("Message Cortex")).toBeVisible();

  await page.keyboard.press("Control+k");
  const paletteInput = page.getByPlaceholder("Type a command or search chats…");
  await expect(paletteInput).toBeVisible();

  await paletteInput.fill("settings");
  await expect(page.getByText("Open settings")).toBeVisible();
  await expect(page.getByText("New chat")).toHaveCount(0);

  await page.getByText("Open settings").click();

  await expect(paletteInput).toHaveCount(0);
  await expect(page.locator("#settings-title")).toBeVisible();
  await expect(page).toHaveURL(/\/settings$/);
});

test("the shortcuts help dialog opens on '?' and closes on Escape", async ({ page }) => {
  await page.route("**/api/v1/session/exchange", async (route) => {
    await route.fulfill({ json: { session_token: "session-shortcuts", expires_at: "2099-01-01T00:00:00Z" } });
  });
  await page.route("**/api/v1/system", async (route) => {
    await route.fulfill({ json: { api_version: "v1", status: "ok", preview: true, session_required: true, started_at: "2026-01-01T00:00:00Z" } });
  });
  await page.route("**/api/v1/chats", async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route("**/api/v1/settings", async (route) => {
    await route.fulfill({ json: { source: "defaults", settings: { appearance: { theme: "dark" }, models: { chat: "local-chat:7b", title: null, translation: "translategemma:4b" }, generation: { temperature: 0.7, num_ctx: 4096, seed: -1 }, memory: { enabled: true }, translation: { enabled: false }, suggestions: { enabled: false, model: null } } } });
  });
  await page.route("**/api/v1/memories", async (route) => {
    await route.fulfill({ json: { memos: [] } });
  });
  await page.route("**/api/v1/models", async (route) => {
    await route.fulfill({ json: { required_models: [], optional_models: [], installed_models: ["local-chat:7b"], missing_models: [], optional_missing_models: [], models: [{ name: "local-chat:7b" }], connection: { success: true, status: "connected", message: "Connected to local runtime." } } });
  });

  await page.goto("/?bootstrap=launcher-token");
  await expect(page.getByLabel("Message Cortex")).toBeVisible();

  // "?" must not fire while focus is inside the composer, so blur it first.
  await page.locator("body").click({ position: { x: 4, y: 4 } });
  await page.keyboard.press("?");

  await expect(page.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
});
