import { test, expect, type Page } from "./fixtures";

const LIGHT_BG = "rgb(243, 241, 236)";
const DARK_BG = "rgb(16, 17, 18)";

/** Everything a workspace load needs; `/settings` is held until `release()` so the pre-settings state can be observed. */
async function routeWorkspace(page: Page, theme: "light" | "dark" | "system") {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  await page.route("**/api/v1/session/exchange", async (route) => {
    await route.fulfill({ json: { session_token: "session-theme", expires_at: "2099-01-01T00:00:00Z" } });
  });
  await page.route("**/api/v1/system", async (route) => {
    await route.fulfill({ json: { api_version: "v1", status: "ok", preview: true, session_required: true, started_at: "2026-01-01T00:00:00Z" } });
  });
  await page.route("**/api/v1/chats", async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route("**/api/v1/settings", async (route) => {
    await gate;
    await route.fulfill({ json: { source: "defaults", settings: { appearance: { theme }, models: { chat: "local-chat:7b", title: null, translation: "translategemma:4b" }, generation: { temperature: 0.7, num_ctx: 4096, seed: -1 }, memory: { enabled: true }, translation: { enabled: false } } } });
  });
  await page.route("**/api/v1/memories", async (route) => {
    await route.fulfill({ json: { memos: [] } });
  });
  await page.route("**/api/v1/models", async (route) => {
    await route.fulfill({ json: { required_models: [], optional_models: [], installed_models: ["local-chat:7b"], missing_models: [], optional_missing_models: [], models: [{ name: "local-chat:7b" }], connection: { success: true, status: "connected", message: "Connected to local runtime." } } });
  });
  return release;
}

const documentTheme = (page: Page) => page.evaluate(() => document.documentElement.dataset.theme);
const bodyBackground = (page: Page) => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
const themeColor = (page: Page) => page.evaluate(() => document.querySelector('meta[name="theme-color"]')?.getAttribute("content"));

test("a returning light-theme user never sees the dark ground while settings load", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light" });
  // What the previous launch left behind, as if this were the second launch.
  await page.addInitScript(() => window.localStorage.setItem("cortex.theme", "light"));
  const release = await routeWorkspace(page, "light");

  await page.goto("/chat/new?bootstrap=launcher-token");

  await expect(page.getByText("Loading local workspace")).toBeVisible();
  expect(await documentTheme(page)).toBe("light");
  expect(await bodyBackground(page)).toBe(LIGHT_BG);
  expect(await themeColor(page)).toBe("#f3f1ec");

  release();
  await expect(page.getByLabel("Message Cortex")).toBeVisible();
  expect(await documentTheme(page)).toBe("light");
  expect(await bodyBackground(page)).toBe(LIGHT_BG);
});

test("the inline script alone sets the theme, before the app bundle runs", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light" });
  await page.addInitScript(() => window.localStorage.setItem("cortex.theme", "system"));
  // Stop the bundle from ever loading: whatever theme is set now came from index.html.
  await page.route("**/src/main.tsx*", (route) => route.abort());

  await page.goto("/");

  expect(await documentTheme(page)).toBe("light");
  expect(await themeColor(page)).toBe("#f3f1ec");
});

test("system follows the operating system's colour scheme, and a first launch stays dark", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light" });
  const release = await routeWorkspace(page, "system");

  // No cache yet: the fallback is dark, matching the backend's default.
  await page.goto("/chat/new?bootstrap=launcher-token");
  await expect(page.getByText("Loading local workspace")).toBeVisible();
  expect(await documentTheme(page)).toBe("dark");
  expect(await bodyBackground(page)).toBe(DARK_BG);

  // Settings say "system" and the OS is light: the workspace turns light, and
  // it follows the OS live.
  release();
  await expect(page.getByLabel("Message Cortex")).toBeVisible();
  await expect.poll(() => documentTheme(page)).toBe("light");
  expect(await page.evaluate(() => window.localStorage.getItem("cortex.theme"))).toBe("system");

  await page.emulateMedia({ colorScheme: "dark" });
  await expect.poll(() => documentTheme(page)).toBe("dark");
  expect(await bodyBackground(page)).toBe(DARK_BG);
});
