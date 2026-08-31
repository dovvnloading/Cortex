/**
 * Capture a full set of product screenshots against the staged showcase
 * workspace.
 *
 * Start tools/screenshots/showcase_server.py first, then:
 *
 *   node tools/screenshots/capture_showcase.mjs <bootstrap-token> \
 *        --port 8801 --out "C:/Users/you/Desktop/Cortex Screenshots"
 *
 * Everything is fixed -- viewport, workspace content, and the order of
 * interactions -- so a re-run overwrites the images with the same pixels.
 */
import { mkdirSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..", "..");

// Playwright lives in frontend/node_modules, not next to this script.
const requireFromFrontend = createRequire(resolve(repoRoot, "frontend", "package.json"));
const { chromium } = requireFromFrontend("@playwright/test");

const browserToken = process.argv[2];
if (!browserToken || browserToken.startsWith("--")) {
  console.error("usage: node capture_showcase.mjs <bootstrap-token> [--port N] [--out DIR]");
  process.exit(1);
}
const flag = (name, fallback) => {
  const index = process.argv.indexOf(`--${name}`);
  return index === -1 ? fallback : process.argv[index + 1];
};
const port = Number(flag("port", 8801));
const outDir = resolve(flag("out", resolve(repoRoot, "docs", "images", "showcase")));
const base = `http://127.0.0.1:${port}`;

const VIEWPORT = { width: 1440, height: 900 };

mkdirSync(outDir, { recursive: true });

const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: VIEWPORT,
  deviceScaleFactor: 2, // crisp on high-DPI displays
  colorScheme: "dark",
  // Message times are rendered with Intl in the viewer's locale and zone.
  // Pin both, or the captured footers differ from machine to machine.
  locale: "en-US",
  timezoneId: "UTC",
});

// Freeze anything time-based so repeated runs stay identical.
await context.addInitScript(() => {
  const fixed = new Date("2026-08-09T17:00:00Z").valueOf();
  const OriginalDate = Date;
  // eslint-disable-next-line no-global-assign
  Date = class extends OriginalDate {
    constructor(...args) {
      super(...(args.length ? args : [fixed]));
    }
    static now() {
      return fixed;
    }
  };
});

const page = await context.newPage();

let index = 0;
const nextName = (name) => `${String(++index).padStart(2, "0")}-${name}`;

/**
 * Saving settings (the theme toggle, for one) raises a transient toast. It is
 * real UI, but it is nobody's subject, so let it retire before the shutter.
 */
const settle = async () => {
  await page.locator(".toast").first().waitFor({ state: "detached", timeout: 10000 }).catch(() => {});
  await page.waitForTimeout(150);
};

const shot = async (name) => {
  await settle();
  const file = nextName(name);
  await page.screenshot({ path: resolve(outDir, `${file}.png`) });
  console.log(`  ${file}.png`);
};

const clip = async (selector, name) => {
  await settle();
  const file = nextName(name);
  await page.locator(selector).first().screenshot({ path: resolve(outDir, `${file}.png`) });
  console.log(`  ${file}.png`);
};

/**
 * Clip a full-height container down to the content it actually holds. The
 * sidebar is 900px of chrome around ~700px of library; the empty remainder
 * says nothing.
 */
const clipToContent = async (container, boundaries, name, pad = 16) => {
  await settle();
  const box = await page.locator(container).first().boundingBox();
  // Measured in the page rather than through locators: an open row menu is
  // absolutely positioned and overflows its row, and only live rects see that.
  const bottom = await page.evaluate(([selectors, floor]) => {
    let lowest = floor;
    for (const selector of selectors) {
      for (const node of document.querySelectorAll(selector)) {
        const rect = node.getBoundingClientRect();
        if (rect.height > 0) lowest = Math.max(lowest, rect.bottom);
      }
    }
    return lowest;
  }, [boundaries, box.y]);
  const file = nextName(name);
  await page.screenshot({
    path: resolve(outDir, `${file}.png`),
    clip: {
      x: box.x,
      y: box.y,
      width: box.width,
      height: Math.min(box.height, bottom + pad - box.y),
    },
  });
  console.log(`  ${file}.png`);
};

const openChat = async (title) => {
  await page.getByRole("button", { name: title, exact: true }).click();
  await page.waitForTimeout(700); // virtualized list + syntax highlighting settle
};

/** Put the last question at the top so answer, footer, and composer all fit. */
const frameFinalExchange = async () => {
  await page.evaluate(() => {
    const asked = document.querySelectorAll(".message-user");
    asked[asked.length - 1]?.scrollIntoView({ block: "start" });
  });
  await page.waitForTimeout(800);
};

const closeOverlay = async () => {
  await page.keyboard.press("Escape");
  await page.waitForTimeout(300);
};

console.log(`Capturing to ${outDir}`);

// ---------------------------------------------------------------------------
// Chat: transcript, code, reasoning, sources, tables
// ---------------------------------------------------------------------------
await page.goto(`${base}/?bootstrap=${encodeURIComponent(browserToken)}`);
await page.getByLabel("Message Cortex").waitFor({ state: "visible" });
await page.waitForTimeout(500);

await openChat("Reading a 4 GB CSV without exhausting memory");
await frameFinalExchange();
// Per-message controls are opacity:0 until the card is hovered or focused, so
// a plain screenshot would omit them entirely.
await page.locator(".message-assistant").last().hover();
await page.waitForTimeout(400); // 140ms opacity transition
await shot("workspace");

await page.evaluate(() => document.querySelector(".message-assistant")?.scrollIntoView({ block: "start" }));
await page.waitForTimeout(700);
await page.locator(".message-assistant").first().hover();
await page.waitForTimeout(400);
await clip(".message-assistant", "answer-code-and-stats");

await openChat("Why attention scales quadratically");
await page.evaluate(() => {
  for (const node of document.querySelectorAll("details.reasoning, details.sources")) node.open = true;
});
await page.waitForTimeout(600);
await page.evaluate(() => document.querySelector("details.reasoning")?.scrollIntoView({ block: "center" }));
await page.waitForTimeout(500);
await shot("reasoning-and-sources");
await clip("details.reasoning", "reasoning-detail");

await openChat("Rust or Go for a small internal CLI");
await frameFinalExchange();
await page.waitForTimeout(400);
await shot("markdown-table");

await openChat("Postgres skips my composite index");
await frameFinalExchange();
await page.waitForTimeout(400);
await shot("transcript-sql");

// ---------------------------------------------------------------------------
// The chat library
// ---------------------------------------------------------------------------
const LIBRARY_BOUNDS = [".chat-row", ".sidebar-empty", ".chat-row-menu-list"];
await clipToContent(".sidebar", LIBRARY_BOUNDS, "chat-library-groups");

const search = page.getByLabel("Search chats by title");
await search.click();
await search.type("log", { delay: 90 });
await page.waitForTimeout(700); // search is debounced
await clipToContent(".sidebar", LIBRARY_BOUNDS, "chat-library-search");
await search.fill("");
await page.waitForTimeout(600);

// Filing a chat into a group: a menu, not drag-and-drop. Opened on a row near
// the top of the library -- .chat-list scrolls, so a menu on the last row is
// clipped by its own container.
try {
  const row = page.locator(".chat-row", { hasText: "Totalling a month of orders on disk" }).first();
  await row.hover();
  await page.waitForTimeout(250);
  await row.getByRole("button", { name: /^Move .* to a group$/ }).click();
  await page.locator(".chat-row-menu-list").first().waitFor({ state: "visible" });
  await page.waitForTimeout(400);
  await clipToContent(".sidebar", LIBRARY_BOUNDS, "chat-library-move-to-group");
  await closeOverlay();
} catch (error) {
  console.warn(`  (skipped move-to-group: ${error.message.split("\n")[0]})`);
}

// ---------------------------------------------------------------------------
// Keyboard surfaces
// ---------------------------------------------------------------------------
await page.goto(`${base}/`);
await page.getByLabel("Message Cortex").waitFor({ state: "visible" });
await page.waitForTimeout(400);

await page.keyboard.press("Control+k");
await page.waitForTimeout(500);
await shot("command-palette");

await page.keyboard.type("ru", { delay: 110 });
await page.waitForTimeout(500);
await shot("command-palette-filtered");
await closeOverlay();

await page.keyboard.press("?");
await page.waitForTimeout(500);
await shot("keyboard-shortcuts");
await closeOverlay();

// ---------------------------------------------------------------------------
// The composer: model switching and per-chat generation parameters
// ---------------------------------------------------------------------------
await openChat("Totalling a month of orders on disk");
await frameFinalExchange();

await page.getByRole("button", { name: /^Selected local model:/ }).click();
await page.waitForTimeout(450);
await shot("composer-model-picker");
await closeOverlay();

await page.getByLabel("Generation parameters for this chat").click();
await page.waitForTimeout(450);
await shot("per-chat-generation-params");
await closeOverlay();

// ---------------------------------------------------------------------------
// Settings, one shot per section
// ---------------------------------------------------------------------------
const settingsSection = async (label, name) => {
  await page.goto(`${base}/settings`);
  await page.getByRole("heading", { name: "Settings", exact: true }).first().waitFor();
  await page.getByRole("button", { name: label, exact: true }).click();
  await page.waitForTimeout(500);
  await shot(name);
};

await settingsSection("General", "settings-general");

await settingsSection("AI Model", "settings-ai-model");
// Further down the same section: standing instructions, and the deliberate
// opt-in that takes Cortex's own system prompt out of the request entirely.
await page.getByLabel("Bypass Cortex's default system prompt").scrollIntoViewIfNeeded();
await page.waitForTimeout(500);
await shot("settings-system-prompt");

await settingsSection("Memory", "settings-memory");
await settingsSection("Translation", "settings-translation");
await settingsSection("System", "settings-system");

// ---------------------------------------------------------------------------
// Light theme
// ---------------------------------------------------------------------------
const toggleTheme = async () => {
  await page.keyboard.press("Control+k");
  await page.waitForTimeout(400);
  await page.getByText("Toggle theme", { exact: true }).click();
  await page.waitForTimeout(800);
};

await page.goto(`${base}/`);
await page.getByLabel("Message Cortex").waitFor({ state: "visible" });
await page.waitForTimeout(400);
await toggleTheme();
await page.emulateMedia({ colorScheme: "light" });

await openChat("Reading a 4 GB CSV without exhausting memory");
await frameFinalExchange();
await page.locator(".message-assistant").last().hover();
await page.waitForTimeout(400);
await shot("workspace-light");

await page.keyboard.press("Control+k");
await page.waitForTimeout(500);
await shot("command-palette-light");
await closeOverlay();

// Back to dark for the remaining shots.
await toggleTheme();
await page.emulateMedia({ colorScheme: "dark" });
await page.waitForTimeout(500);

// ---------------------------------------------------------------------------
// Approval-gated local execution -- staged last, because the tray is a fixed
// overlay that would otherwise sit on top of every other capture.
// ---------------------------------------------------------------------------
const staged = await fetch(`${base}/showcase/execution/stage`, { method: "POST" });
if (!staged.ok) throw new Error(`Could not stage execution tasks: ${staged.status}`);

await openChat("Totalling a month of orders on disk");
await frameFinalExchange();
await page.locator(".execution-task-tray").waitFor({ state: "visible" });
await page.waitForTimeout(900); // let the 1s task poll settle
await shot("local-execution-approval");
await clip(".execution-task-tray", "local-execution-tray");

// The generated source is inspectable before anything runs.
await page.locator("details.execution-task-code-details").first().evaluate((node) => { node.open = true; });
await page.waitForTimeout(900); // source is fetched on open
await clip(".execution-task-tray", "local-execution-source-review");
await shot("local-execution-source-review-full");

await browser.close();
console.log(`Done. ${index} images in ${outDir}`);
