import { test as base, expect, type Page } from "@playwright/test";

/**
 * Browser tests must own every API request.  The Vite dev server has no API
 * implementation, so an unmocked request otherwise becomes a misleading
 * localhost failure that the application may quietly fall back from.
 *
 * Individual tests register their more specific routes after this fixture;
 * Playwright gives those routes precedence. Every authenticated workspace
 * reads groups and configured cloud providers in the background. Tests
 * that exercise those inventories override the empty defaults.
 */
async function installApiRequestGuard(page: Page) {
  const unexpected: string[] = [];

  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "GET" && url.pathname === "/api/v1/chat-groups") {
      await route.fulfill({ json: [] });
      return;
    }

    if (request.method() === "GET" && url.pathname === "/api/v1/cloud/providers") {
      await route.fulfill({ json: { providers: [] } });
      return;
    }

    unexpected.push(`${request.method()} ${url.pathname}${url.search}`);
    await route.abort("failed");
  });

  return () => {
    expect(unexpected, "unexpected unmocked API requests").toEqual([]);
  };
}

/** Bootstrap navigation may complete before the new-chat route mounts its composer. */
export async function waitForNewChatComposer(page: Page): Promise<void> {
  await expect(page.getByRole("heading", { name: "New thread" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Message Cortex" })).toBeVisible();
}

export const test = base.extend({
  page: async ({ page }, use) => {
    const assertRequests = await installApiRequestGuard(page);
    // Playwright's fixture callback is named "page" but its second argument
    // is the fixture lifecycle callback, not a React hook.
    // eslint-disable-next-line react-hooks/rules-of-hooks
    await use(page);
    assertRequests();
  },
});

export { expect };
export type { Page } from "@playwright/test";
