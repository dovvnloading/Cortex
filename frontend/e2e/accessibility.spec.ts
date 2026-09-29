import type { Locator } from "@playwright/test";
import { test, expect, type Page } from "./fixtures";

/**
 * Windows High Contrast (forced-colors) mode drops box-shadows and author
 * backgrounds. WebView2 is Chromium, so Playwright's `forcedColors: "active"`
 * emulation applies the same rules the desktop window does. These checks read
 * computed style rather than pixels: they prove focus and control state are
 * carried by something forced colours keep (outlines, system colours, glyphs).
 */
const CHATS = [{ id: "thread-a", title: "Weekend plans", timestamp: "2026-01-01T00:00:00Z" }];

async function openWorkspace(page: Page, path: string) {
  // Not a `test.use` option: Playwright's fixtures do not expose forcedColors.
  // Reduced motion switches the stylesheet's 150ms colour transitions off, so
  // computed style is the settled value rather than a frame partway between
  // the on and off palette.
  await page.emulateMedia({ forcedColors: "active", reducedMotion: "reduce" });
  await page.route("**/api/v1/session/exchange", async (route) => {
    await route.fulfill({ json: { session_token: "session-a11y", expires_at: "2099-01-01T00:00:00Z" } });
  });
  await page.route("**/api/v1/system", async (route) => {
    await route.fulfill({ json: { api_version: "v1", status: "ok", preview: true, session_required: true, started_at: "2026-01-01T00:00:00Z" } });
  });
  await page.route("**/api/v1/chats", async (route) => {
    await route.fulfill({ json: CHATS });
  });
  await page.route("**/api/v1/chats/thread-a", async (route) => {
    await route.fulfill({ json: { id: "thread-a", title: "Weekend plans", timestamp: "2026-01-01T00:00:00Z", revision: 1, messages: [{ id: "m-1", role: "user", content: "hi" }] } });
  });
  await page.route("**/api/v1/settings", async (route) => {
    await route.fulfill({ json: { source: "defaults", settings: { appearance: { theme: "dark" }, models: { chat: "local-chat:7b", title: null, translation: "translategemma:4b" }, generation: { temperature: 0.7, num_ctx: 4096, seed: -1 }, memory: { enabled: true }, translation: { enabled: false } } } });
  });
  await page.route("**/api/v1/memories", async (route) => {
    await route.fulfill({ json: { memos: [] } });
  });
  await page.route("**/api/v1/models", async (route) => {
    await route.fulfill({ json: { required_models: [], optional_models: [], installed_models: ["local-chat:7b"], missing_models: [], optional_missing_models: [], models: [{ name: "local-chat:7b" }], connection: { success: true, status: "connected", message: "Connected to local runtime." } } });
  });
  await page.goto(`${path}?bootstrap=launcher-token`);
}

/** Move focus onto `target` the way a keyboard does, so :focus-visible applies to buttons too. */
async function focusByKeyboard(page: Page, target: Locator) {
  await target.focus();
  await page.keyboard.press("Shift+Tab");
  await page.keyboard.press("Tab");
  await expect(target).toBeFocused();
}

async function outline(target: Locator) {
  return target.evaluate((element) => {
    const style = getComputedStyle(element);
    return { style: style.outlineStyle, width: parseFloat(style.outlineWidth) };
  });
}

test("in forced-colors mode the emulation is really active", async ({ page }) => {
  await openWorkspace(page, "/chat/thread-a");
  await expect(page.getByLabel("Message Cortex")).toBeVisible();
  expect(await page.evaluate(() => window.matchMedia("(forced-colors: active)").matches)).toBe(true);
});

test("keyboard focus stays visible on buttons, wrapped fields and the toggle", async ({ page }) => {
  await openWorkspace(page, "/chat/thread-a");
  await expect(page.getByLabel("Message Cortex")).toBeVisible();

  // A plain button.
  const newChat = page.getByRole("button", { name: "New thread" });
  await focusByKeyboard(page, newChat);
  expect(await outline(newChat)).toMatchObject({ style: "solid" });
  expect((await outline(newChat)).width).toBeGreaterThanOrEqual(2);

  // The composer's ring is drawn on its surface, not on the textarea, which
  // deliberately has no outline of its own.
  const surface = page.locator(".composer-surface");
  await page.getByLabel("Message Cortex").focus();
  await expect(surface).toBeVisible();
  expect(await outline(surface)).toMatchObject({ style: "solid" });
  expect((await outline(surface)).width).toBeGreaterThanOrEqual(2);

  // The sidebar search is the same pattern: an input inside a ringed wrapper.
  const search = page.locator(".sidebar-search");
  await search.locator("input").focus();
  expect(await outline(search)).toMatchObject({ style: "solid" });

  // The toggle switch, on the Memory settings page.
  await page.getByRole("link", { name: "Settings" }).click();
  await page.getByRole("button", { name: "Memory" }).click();
  const toggle = page.locator("#memory-enabled");
  await focusByKeyboard(page, toggle);
  expect(await outline(toggle)).toMatchObject({ style: "solid" });
});

test("the toggle switch shows on and off with different system colours and a check mark", async ({ page }) => {
  await openWorkspace(page, "/settings");
  await page.getByRole("button", { name: "Memory" }).click();
  const toggle = page.locator("#memory-enabled");
  await expect(toggle).toBeChecked();

  const paint = () => toggle.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      background: style.backgroundColor,
      thumb: getComputedStyle(element, "::after").backgroundColor,
      glyph: getComputedStyle(element, "::before").content,
    };
  });
  const on = await paint();
  await toggle.uncheck();
  // A style change starts a transition, and a computed-style read in the same
  // frame still returns the old value even at a 0.01ms duration. Poll until the
  // new state has painted rather than sampling once.
  await expect.poll(async () => (await paint()).background).not.toBe(on.background);
  const off = await paint();

  expect(on.background).not.toBe(off.background);
  expect(on.glyph).toContain("✓");
  expect(off.glyph).not.toContain("✓");
  // The thumb must not vanish into the track in either state.
  expect(on.thumb).not.toBe(on.background);
  expect(off.thumb).not.toBe(off.background);
});

test("the slider track is drawn: filled part in Highlight, the rest in ButtonText", async ({ page }) => {
  await openWorkspace(page, "/settings");
  await page.getByRole("button", { name: "AI Model" }).click();
  const slider = page.locator("input.range-input").first();
  await expect(slider).toBeVisible();

  // The track is a shadow-DOM pseudo-element, so getComputedStyle cannot see
  // it. Forced colours do not paint a gradient background at all, which made
  // the track vanish; sample the rendered pixels instead.
  const box = (await slider.boundingBox())!;
  const png = await page.screenshot({ clip: { x: box.x, y: box.y, width: box.width, height: box.height } });
  const sampled = await page.evaluate(async (base64) => {
    const bitmap = await createImageBitmap(await (await fetch(`data:image/png;base64,${base64}`)).blob());
    const canvas = document.createElement("canvas");
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    const context = canvas.getContext("2d")!;
    context.drawImage(bitmap, 0, 0);
    const pixel = (fraction: number) =>
      Array.from(context.getImageData(Math.floor(bitmap.width * fraction), Math.floor(bitmap.height / 2), 1, 1).data.slice(0, 3));
    const resolve = (keyword: string) => {
      const probe = document.createElement("span");
      probe.style.color = keyword;
      document.body.append(probe);
      const value = getComputedStyle(probe).color.match(/\d+/g)!.slice(0, 3).map(Number);
      probe.remove();
      return value;
    };
    return { filled: pixel(0.08), empty: pixel(0.95), highlight: resolve("Highlight"), buttonText: resolve("ButtonText") };
  }, png.toString("base64"));

  expect(sampled.filled).toEqual(sampled.highlight);
  expect(sampled.empty).toEqual(sampled.buttonText);
});

test("the current chat row is outlined, not only tinted", async ({ page }) => {
  await openWorkspace(page, "/chat/thread-a");
  await expect(page.getByLabel("Message Cortex")).toBeVisible();
  const row = page.locator(".chat-row-active");
  await expect(row).toHaveCount(1);
  expect(await outline(row)).toMatchObject({ style: "solid" });
  expect((await outline(row)).width).toBeGreaterThanOrEqual(2);
});
