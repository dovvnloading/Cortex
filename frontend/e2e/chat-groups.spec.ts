import { expect, test } from "./fixtures";
import type { Page } from "@playwright/test";

const settings = {
  appearance: { theme: "dark" },
  models: { chat: "local-chat:7b", title: null, translation: "translategemma:4b" },
  generation: { temperature: 0.7, num_ctx: 4096, seed: -1 },
  memory: { enabled: true },
  translation: { enabled: false },
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

  await page.goto("/#bootstrap=launcher-token");
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

  await page.goto("/#bootstrap=launcher-token");
  await expect(page.getByLabel("Message Cortex")).toBeVisible();
  await page.getByRole("button", { name: "New group" }).click();
  await page.getByRole("textbox", { name: "Group name" }).fill("Created while loading");
  await page.getByRole("button", { name: "Create group" }).click();
  await expect(page.getByRole("button", { name: "Collapse Created while loading" })).toBeVisible();

  releaseInitialGroups();
  await expect(page.getByRole("button", { name: "Collapse Created while loading" })).toBeVisible();
  await expect(page.getByText("Created while loading")).toHaveCount(1);
});

test("rapid moves persist the final destination even when the earlier PATCH is slow", async ({ page }) => {
  const groupA = {
    id: "group-one", name: "Research", collapsed: false,
    created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
  };
  const groupB = { ...groupA, id: "group-two", name: "Planning" };
  await stubWorkspace(page, [groupA, groupB]);

  let storedGroup = "group-one";
  await page.unroute("**/api/v1/chats");
  await page.route("**/api/v1/chats", async (route) => {
    await route.fulfill({ json: [{
      id: "grouped-chat", title: "Grouped chat", timestamp: "2026-01-01T00:00:00Z",
      group_id: storedGroup,
    }] });
  });

  let releaseFirst!: () => void;
  const firstHeld = new Promise<void>((resolve) => { releaseFirst = resolve; });
  const writes: string[] = [];
  await page.route("**/api/v1/chats/grouped-chat/group", async (route) => {
    const body = route.request().postDataJSON() as { group_id: string };
    writes.push(body.group_id);
    if (writes.length === 1) await firstHeld;
    storedGroup = body.group_id;
    await route.fulfill({ json: {
      id: "grouped-chat", title: "Grouped chat", timestamp: "2026-01-01T00:00:00Z",
      group_id: storedGroup,
    } });
  });

  try {
    await page.goto("/#bootstrap=launcher-token");
    await expect(page.getByRole("button", { name: "Grouped chat", exact: true })).toBeVisible();

    await page.getByRole("button", { name: "Move Grouped chat to a group" }).focus();
    await page.getByRole("button", { name: "Move Grouped chat to a group" }).press("Enter");
    await page.getByRole("menuitem", { name: "Planning" }).click();
    await expect.poll(() => writes).toEqual(["group-two"]);

    // The UI moves optimistically, so the second selection can be made even
    // though the first PATCH has not arrived at the backend yet.
    await page.getByRole("button", { name: "Move Grouped chat to a group" }).focus();
    await page.getByRole("button", { name: "Move Grouped chat to a group" }).press("Enter");
    await page.getByRole("menuitem", { name: "Research" }).click();
    expect(writes).toEqual(["group-two"]); // no overtaking second request

    releaseFirst();
    await expect.poll(() => writes).toEqual(["group-two", "group-one"]);
    await expect.poll(() => storedGroup).toBe("group-one");
    await page.reload();
    await expect(page.getByRole("button", { name: "Grouped chat", exact: true })).toBeVisible();
    await expect(page.locator(".chat-group").filter({ hasText: "Research" }).getByRole(
      "button", { name: "Grouped chat", exact: true },
    )).toBeVisible();
  } finally {
    releaseFirst();
  }
});

test("rapid collapse and expand persist the final preference after reload", async ({ page }) => {
  const group = {
    id: "group-one", name: "Research", collapsed: false,
    created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
  };
  await stubWorkspace(page, [group]);
  let releaseFirst!: () => void;
  const firstHeld = new Promise<void>((resolve) => { releaseFirst = resolve; });
  const writes: boolean[] = [];
  await page.route("**/api/v1/chat-groups/group-one", async (route) => {
    const body = route.request().postDataJSON() as { collapsed: boolean };
    writes.push(body.collapsed);
    if (writes.length === 1) await firstHeld;
    group.collapsed = body.collapsed;
    await route.fulfill({ json: group });
  });

  try {
    await page.goto("/#bootstrap=launcher-token");
    await page.getByRole("button", { name: "Collapse Research" }).click();
    await expect.poll(() => writes).toEqual([true]);
    await page.getByRole("button", { name: "Expand Research" }).click();
    expect(writes).toEqual([true]);

    releaseFirst();
    await expect.poll(() => writes).toEqual([true, false]);
    await expect.poll(() => group.collapsed).toBe(false);
    await page.reload();
    await expect(page.getByRole("button", { name: "Collapse Research" })).toBeVisible();
  } finally {
    releaseFirst();
  }
});
