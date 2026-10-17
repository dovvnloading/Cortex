import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import { E2E_BACKEND, E2E_BACKEND_LIVENESS, E2E_BACKEND_ORIGIN } from "./e2eBackend";

/**
 * The one browser test that does not mock the API.
 *
 * Every other spec intercepts `api/v1` routes and answers from a hand-written
 * fixture, so the contract between the two halves of Cortex was checked only by
 * the generated TypeScript types. Nothing executed the real routes together
 * with the real client -- and the fixtures had already drifted, which is how a
 * removed settings field survived in seven of them.
 *
 * This runs against `scripts/e2e_backend.py`: the real application, real
 * routing, real Pydantic serialisation, real status codes, real SSE framing and
 * the real session exchange, with deterministic stand-ins only for the model
 * runtime and the stores behind it.
 *
 * Deliberately not using `./fixtures`, whose guard aborts any request it did
 * not mock.
 */

/**
 * These tests share one backend process, which holds exactly one bootstrap
 * token at a time -- `/session/handoff` replaces the previous one and marks it
 * unused. That is correct for the launcher, which mints one token for one
 * window, but it means two tests minting concurrently invalidate each other.
 * `fullyParallel` in the config would do exactly that, so this file opts out
 * and runs its tests in order in a single worker.
 */
test.describe.configure({ mode: "default" });

const HANDOFF_SECRET = "e2e-handoff-secret";

/**
 * Locally, Playwright reuses whatever already answers on the backend port and
 * the dev server's port, and its readiness probe only looks at the status
 * code. Refuse to run against anything that is not `scripts/e2e_backend.py`:
 * checked directly, and through the dev server's proxy the page actually uses.
 */
test.beforeEach(async ({ request }) => {
  const header = E2E_BACKEND.markerHeader.toLowerCase();
  for (const url of [`${E2E_BACKEND_ORIGIN}${E2E_BACKEND_LIVENESS}`, E2E_BACKEND_LIVENESS]) {
    const live = await request.get(url);
    expect(
      live.headers()[header],
      `${url} is not served by scripts/e2e_backend.py; stop whatever is listening there`,
    ).toBe(E2E_BACKEND.marker);
  }
});

/** A fresh bootstrap token, the way the launcher obtains one. */
async function bootstrapToken(request: APIRequestContext): Promise<string> {
  const handoff = await request.post("/api/v1/session/handoff", {
    headers: { "X-Cortex-Handoff": HANDOFF_SECRET },
  });
  expect(handoff.ok(), await handoff.text()).toBeTruthy();
  const { bootstrap_token: token } = await handoff.json();
  expect(token).toBeTruthy();
  return token;
}

/**
 * A bearer token for direct API assertions.
 *
 * Playwright's request context is a separate client from the page, so it does
 * not inherit the session the browser exchanged. Each caller does its own
 * exchange -- which is itself part of what this file is testing.
 */
async function sessionHeaders(request: APIRequestContext) {
  const exchanged = await request.post("/api/v1/session/exchange", {
    data: { bootstrap_token: await bootstrapToken(request) },
  });
  expect(exchanged.ok(), await exchanged.text()).toBeTruthy();
  const { session_token: session } = await exchanged.json();
  return { Authorization: `Bearer ${session}` };
}

async function openWorkspace(page: Page, request: APIRequestContext): Promise<string> {
  const token = await bootstrapToken(request);
  await page.goto(`/#bootstrap=${encodeURIComponent(token)}`);
  await expect(page.getByLabel("Message Cortex")).toBeVisible();
  return token;
}

test("exchanges a real session and scrubs the credential from the URL", async ({ page, request }) => {
  const token = await openWorkspace(page, request);

  // The credential must not survive in the address bar once it is spent.
  expect(page.url()).not.toContain(token);
  expect(page.url()).not.toContain("bootstrap=");
});

test("a spent bootstrap token cannot be exchanged twice", async ({ request }) => {
  const token = await bootstrapToken(request);

  const first = await request.post("/api/v1/session/exchange", {
    data: { bootstrap_token: token },
  });
  expect(first.status()).toBe(200);

  const second = await request.post("/api/v1/session/exchange", {
    data: { bootstrap_token: token },
  });
  expect(second.status()).toBe(401);
});

test("refuses a request with no session", async ({ request }) => {
  const response = await request.get("/api/v1/settings");

  expect(response.status()).toBe(401);
});

test("serves settings in the shape the generated contract describes", async ({ request }) => {
  const headers = await sessionHeaders(request);

  const settings = await request.get("/api/v1/settings", { headers });
  expect(settings.ok(), await settings.text()).toBeTruthy();
  const body = await settings.json();

  expect(body).toHaveProperty("settings.generation.num_ctx");
  expect(body).toHaveProperty("settings.models");
  expect(body).toHaveProperty("settings.execution");
  // The retired field must not come back.
  expect(body.settings).not.toHaveProperty("suggestions");
});

test("completes a real generation end to end", async ({ page, request }) => {
  const headers = await sessionHeaders(request);

  // A fresh workspace has no model selected, so the composer refuses to send.
  // Choosing one through the real settings route is part of the flow.
  const current = await (await request.get("/api/v1/settings", { headers })).json();
  const update = await request.put("/api/v1/settings", {
    headers,
    data: {
      settings: {
        ...current.settings,
        revision: current.settings.revision + 1,
        models: { ...current.settings.models, chat: "qwen3:8b" },
      },
      expected_revision: current.settings.revision,
    },
  });
  expect(update.ok(), await update.text()).toBeTruthy();

  await openWorkspace(page, request);

  // A reused local backend keeps every chat from earlier runs, so this test
  // names its own: a prompt no other run sent, and the thread id the server
  // assigned to this send.
  const prompt = `hello from the browser ${Date.now().toString(36)}`;
  const accepted = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" && new URL(response.url()).pathname === "/api/v1/generations",
  );
  await page.getByLabel("Message Cortex").fill(prompt);
  await page.getByRole("button", { name: "Send message" }).click();
  const acceptance = await accepted;
  expect(acceptance.ok(), await acceptance.text()).toBeTruthy();
  const { thread_id: threadId } = await acceptance.json();
  expect(threadId).toBeTruthy();

  // The reply text comes from the deterministic engine, but everything between
  // the click and this assertion is real: admission, the job registry, the SSE
  // stream, persistence, and the chat reload.
  await expect(page.getByText(`Echo: ${prompt}`)).toBeVisible({ timeout: 20_000 });

  // Persisted and listed, not merely rendered from the stream.
  const chats = await request.get("/api/v1/chats", { headers });
  expect(chats.ok()).toBeTruthy();
  const summaries: { id: string }[] = await chats.json();
  expect(summaries.map((summary) => summary.id)).toContain(threadId);

  const thread = await request.get(`/api/v1/chats/${encodeURIComponent(threadId)}`, { headers });
  expect(thread.ok()).toBeTruthy();
  const messages: { role: string; content: string }[] = (await thread.json()).messages;
  expect(messages.map((message) => message.role)).toEqual(["user", "assistant"]);
  expect(messages[0].content).toBe(prompt);
  expect(messages[1].content).toBe(`Echo: ${prompt}`);
});
