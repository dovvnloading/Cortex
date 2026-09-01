import { test, expect } from "./fixtures";

// Real Chromium layout is required to prove react-virtuoso actually windows
// and renders content once a transcript crosses the virtualization
// threshold (jsdom reports zero height, so this path is untestable there —
// see MessageList.test.tsx for what the unit-level coverage can prove).
test("renders and scrolls a virtualized transcript once messages cross the threshold", async ({ page }) => {
  const threadId = "long-thread";
  const messageCount = 60;
  const messages = Array.from({ length: messageCount }, (_, index) => ({
    id: `m-${index}`,
    role: index % 2 === 0 ? "user" : "assistant",
    content: index === 0 ? "First message in a long conversation." : `Reply number ${index}.`,
    timestamp: "2026-01-01T00:00:00Z",
  }));
  const chat = { id: threadId, title: "Long conversation", timestamp: "2026-01-01T00:00:00Z", revision: 1, messages };

  await page.route("**/api/v1/session/exchange", async (route) => {
    await route.fulfill({ json: { session_token: "session-virtual", expires_at: "2099-01-01T00:00:00Z" } });
  });
  await page.route("**/api/v1/system", async (route) => {
    await route.fulfill({ json: { api_version: "v1", status: "ok", preview: true, session_required: true, started_at: "2026-01-01T00:00:00Z" } });
  });
  await page.route("**/api/v1/chats", async (route) => {
    if (route.request().method() === "GET") await route.fulfill({ json: [{ id: chat.id, title: chat.title, timestamp: chat.timestamp }] });
    else await route.continue();
  });
  await page.route(`**/api/v1/chats/${threadId}`, async (route) => {
    await route.fulfill({ json: chat });
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

  await page.goto(`/chat/${threadId}?bootstrap=launcher-token`);

  // The virtualized container is in play, not the plain scrollable transcript.
  await expect(page.locator(".transcript-virtual")).toBeVisible();

  // Loaded aligned-to-bottom: the last message is visible without scrolling.
  await expect(page.getByText(`Reply number ${messageCount - 1}.`)).toBeVisible();

  // The first message is windowed out at the bottom (not present at all, or
  // present but off-screen) — scrolling up must actually bring it in, which
  // only a genuinely virtualized/scrollable container can do.
  await page.locator(".transcript-virtual").evaluate((node) => { node.scrollTop = 0; });
  await expect(page.getByText("First message in a long conversation.")).toBeVisible();

  // Scrolling back down returns to the tail of the conversation (proves the
  // scroll container is a real, continuously scrollable virtualized list,
  // not a one-shot render). "Jump to latest" itself only appears once new
  // streamed content arrives while scrolled away — that interaction is
  // covered at the unit level in MessageList.test.tsx and is unchanged by
  // virtualization, so it is not re-asserted here.
  await page.locator(".transcript-virtual").evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await expect(page.getByText(`Reply number ${messageCount - 1}.`)).toBeVisible();
});
