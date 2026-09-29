import { expect, test, type Page } from "./fixtures";

async function stubWorkspace(page: Page) {
  let settings = {
    appearance: { theme: "dark" },
    models: { chat: "local-chat:7b", title: null, translation: "translategemma:4b" },
    generation: { temperature: 0.7, num_ctx: 4096, seed: -1 },
    memory: { enabled: true },
    translation: { enabled: false },
  };
  const staged: string[] = [];

  await page.route("**/api/v1/session/exchange", async (route) => {
    await route.fulfill({ json: { session_token: "session-attachments", expires_at: "2099-01-01T00:00:00Z" } });
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
        required_models: [], optional_models: [], installed_models: ["local-chat:7b"],
        missing_models: [], optional_missing_models: [],
        models: [{ name: "local-chat:7b" }],
        connection: { success: true, status: "connected", message: "Connected." },
      },
    });
  });
  await page.route("**/api/v1/attachments", async (route) => {
    const { filename } = await route.request().postDataJSON();
    staged.push(filename);
    await route.fulfill({
      json: {
        attachment_id: `id-${staged.length}`,
        filename,
        mime_type: filename.endsWith(".png") ? "image/png" : "text/markdown",
        size: 2048,
        sha256: "e".repeat(64),
        kind: filename.endsWith(".png") ? "image" : "document",
        expires_at: "2099-01-01T00:00:00Z",
      },
    });
  });
  return { staged };
}

test("a file dropped on the window is kept out of the navigation path, and one dropped on the composer is attached", async ({ page }) => {
  const { staged } = await stubWorkspace(page);
  await page.goto("/?bootstrap=launcher-token");
  await expect(page.getByLabel("Message Cortex")).toBeVisible();
  const url = page.url();

  // Outside every drop target: the window must cancel it or it would open the file.
  const stray = await page.evaluate(() => {
    const transfer = new DataTransfer();
    transfer.items.add(new File(["hi"], "notes.md", { type: "text/markdown" }));
    const fire = (type: string) => {
      const event = new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: transfer });
      document.body.dispatchEvent(event);
      return event.defaultPrevented;
    };
    return { over: fire("dragover"), dropped: fire("drop") };
  });
  expect(stray).toEqual({ over: true, dropped: true });
  expect(staged).toEqual([]);

  // On the composer it is attached instead.
  await page.evaluate(() => {
    const transfer = new DataTransfer();
    transfer.items.add(new File(["# Notes"], "notes.md", { type: "text/markdown" }));
    const surface = document.querySelector(".composer-surface") as HTMLElement;
    surface.dispatchEvent(new DragEvent("dragenter", { bubbles: true, cancelable: true, dataTransfer: transfer }));
    surface.dispatchEvent(new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer: transfer }));
  });
  await expect(page.getByRole("button", { name: "Remove notes.md" })).toBeVisible();
  expect(staged).toEqual(["notes.md"]);
  expect(page.url()).toBe(url);
});

test("pasting a screenshot attaches it and pasting text does not", async ({ page }) => {
  const { staged } = await stubWorkspace(page);
  await page.goto("/?bootstrap=launcher-token");
  const composer = page.getByLabel("Message Cortex");
  await composer.focus();

  await page.evaluate(() => {
    const transfer = new DataTransfer();
    transfer.items.add(new File([new Uint8Array([137, 80, 78, 71])], "image.png", { type: "image/png" }));
    const textarea = document.getElementById("chat-composer") as HTMLTextAreaElement;
    textarea.dispatchEvent(new ClipboardEvent("paste", { bubbles: true, cancelable: true, clipboardData: transfer }));
  });
  await expect(page.getByRole("button", { name: "Remove image.png" })).toBeVisible();
  expect(staged).toEqual(["image.png"]);

  await page.evaluate(() => {
    const transfer = new DataTransfer();
    transfer.setData("text/plain", "only words");
    const textarea = document.getElementById("chat-composer") as HTMLTextAreaElement;
    textarea.dispatchEvent(new ClipboardEvent("paste", { bubbles: true, cancelable: true, clipboardData: transfer }));
  });
  expect(staged).toEqual(["image.png"]);
});

test("settings asks before edits are lost and keeps them when told to keep editing", async ({ page }) => {
  await stubWorkspace(page);
  await page.goto("/settings?bootstrap=launcher-token");
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Save settings" })).toBeDisabled();

  await page.getByRole("combobox", { name: "Theme" }).click();
  await page.getByRole("option", { name: "Light" }).click();
  await expect(page.getByText("Unsaved changes")).toBeVisible();
  await expect(page.getByRole("button", { name: "Save settings" })).toBeEnabled();

  await page.getByRole("button", { name: "Close settings" }).click();
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Keep editing" }).click();
  await expect(dialog).toBeHidden();
  expect(new URL(page.url()).pathname).toBe("/settings");
  await expect(page.getByText("Unsaved changes")).toBeVisible();

  await page.getByRole("button", { name: "Close settings" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Discard" }).click();
  await expect.poll(() => new URL(page.url()).pathname).toBe("/chat/new");
});
