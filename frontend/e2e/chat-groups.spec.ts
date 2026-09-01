import { expect, test } from "./fixtures";
import type { Page } from "@playwright/test";

const settings = {
  appearance: { theme: "dark" },
  models: { chat: "local-chat:7b", title: null, translation: "translategemma:4b" },
  generation: { temperature: 0.7, num_ctx: 4096, seed: -1 },
  memory: { enabled: true },
  translation: { enabled: false },
  suggestions: { enabled: false, model: null },
};

async function stubWorkspace(page: Page, groups: unknown) {
  await page.route("**/api/v1/session/exchange", async (route) => {
    await route.fulfill({ json: { session_token: "session-groups", expires_at: "2099-01-01T00:00:00Z" } });
  });
  await page.route("**/api/v1/system", async (route) => {
    await route.fulfill({ json: { api_version: "v1", status: "ok", preview: true, session_required: true, started_at: "2026-01-01T00:00:00Z" } });
  });
  await page.route("**/api/v1/chats", async (route) => {
    await route.fulfill({ json: [{ id: "grouped-chat", title: "Grouped chat", timestamp: "2026-01-01T00:00:00Z", group_id: "group-one" }] });
  });
  await page.route("**/api/v1/settings", async (route) => {
    await route.fulfill({ json: { source: "defaults", settings } });
  });
  await page.route("**/api/v1/memories", async (route) => {
    await route.fulfill({ json: { memos: [] } });
  });
  await page.route("**/api/v1/models", async (route) => {
    await route.fulfill({ json: {
      required_models: [], optional_models: [], installed_models: ["local-chat:7b"],
      missing_models: [], optional_missing_models: [], models: [{ name: "local-chat:7b" }],
      connection: { success: true, status: "connected", message: "Connected to local runtime." },
    } });
  });
  await page.route("**/api/v1/chat-groups", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ json: groups });
      return;
    }
    const body = route.request().postDataJSON() as { name: string };
    await route.fulfill({ status: 201, json: {
      id: "group-created",
      name: body.name,
      collapsed: false,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    } });
  });
}

test("loads chat groups successfully and files their chats", async ({ page }) => {
  await stubWorkspace(page, [{
    id: "group-one",
    name: "Research",
    collapsed: false,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  }]);

  await page.goto("/?bootstrap=launcher-token");
  await expect(page.getByRole("button", { name: "Collapse Research" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Grouped chat", exact: true })).toBeVisible();
  await expect(page.getByText("Research")).toBeVisible();
});

test("does not let a late initial group response erase a group created while loading", async ({ page }) => {
  let releaseInitialGroups!: () => void;
  const initialGroups = new Promise<void>((resolve) => { releaseInitialGroups = resolve; });

  await stubWorkspace(page, []);
  await page.unroute("**/api/v1/chat-groups");
  await page.route("**/api/v1/chat-groups", async (route) => {
    if (route.request().method() === "GET") {
      await initialGroups;
      await route.fulfill({ json: [] });
      return;
    }
    const body = route.request().postDataJSON() as { name: string };
    await route.fulfill({ status: 201, json: {
      id: "group-created",
      name: body.name,
      collapsed: false,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    } });
  });

  await page.goto("/?bootstrap=launcher-token");
  await expect(page.getByLabel("Message Cortex")).toBeVisible();
  await page.getByRole("button", { name: "New group" }).click();
  await page.getByRole("textbox", { name: "Group name" }).fill("Created while loading");
  await page.getByRole("button", { name: "Create group" }).click();
  await expect(page.getByRole("button", { name: "Collapse Created while loading" })).toBeVisible();

  releaseInitialGroups();
  await expect(page.getByRole("button", { name: "Collapse Created while loading" })).toBeVisible();
  await expect(page.getByText("Created while loading")).toHaveCount(1);
});
