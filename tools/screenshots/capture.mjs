/**
 * Capture the documentation screenshots against the staged demo workspace.
 *
 * Start tools/screenshots/demo_server.py first, then:
 *
 *   node tools/screenshots/capture.mjs <bootstrap-token> [--port 8799]
 *
 * Everything is fixed -- viewport, workspace content, and the order of
 * interactions -- so a re-run overwrites the images with the same pixels.
 * Run from the frontend/ directory so the local playwright install resolves.
 */
import { mkdirSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..", "..");
// .github/images is where the README actually reads its screenshots from.
const outDir = resolve(repoRoot, ".github", "images");

// Playwright lives in frontend/node_modules, not next to this script, so
// resolve it from there rather than relying on the caller's directory.
const requireFromFrontend = createRequire(resolve(repoRoot, "frontend", "package.json"));
const { chromium } = requireFromFrontend("@playwright/test");

const token = process.argv[2];
if (!token) {
  console.error("usage: node capture.mjs <bootstrap-token> [--port N]");
  process.exit(1);
}
const portFlag = process.argv.indexOf("--port");
const port = portFlag === -1 ? 8799 : Number(process.argv[portFlag + 1]);
const base = `http://127.0.0.1:${port}`;

const VIEWPORT = { width: 1440, height: 900 };

mkdirSync(outDir, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({
  viewport: VIEWPORT,
  deviceScaleFactor: 2, // crisp on high-DPI displays
  colorScheme: "dark",
  // Message times are rendered with Intl in the viewer's locale and zone.
  // Pin both, or the captured footers differ from machine to machine.
  locale: "en-US",
  timezoneId: "UTC",
});

const shot = async (name) => {
  const path = resolve(outDir, `${name}.png`);
  await page.screenshot({ path });
  console.log(`  wrote .github/images/${name}.png`);
};

// Freeze anything time-based so repeated runs stay identical.
await page.addInitScript(() => {
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

console.log("Capturing...");

// --- 1. Workspace: sidebar library + a real transcript -----------------------
await page.goto(`${base}/?bootstrap=${encodeURIComponent(token)}`);
await page.getByLabel("Message Cortex").waitFor({ state: "visible" });

// The sidebar is already expanded at this width, so open the hero
// conversation straight from the library.
await page
  .getByRole("button", { name: "Reading a 4 GB CSV without exhausting memory", exact: true })
  .click();
await page.getByText("Read it as a").first().waitFor({ state: "visible" });
await page.waitForTimeout(600); // let syntax highlighting settle

// Frame the shot on the final exchange: put the closing question at the top so
// the whole answer, its footer, and the composer all land in one viewport.
await page.evaluate(() => {
  const asked = document.querySelectorAll(".message-user");
  asked[asked.length - 1]?.scrollIntoView({ block: "start" });
});
await page.waitForTimeout(900); // virtualized list re-renders the window

// The per-message controls (copy, regenerate, fork) are opacity:0 until the
// card is hovered or focused, so a plain screenshot would omit them entirely.
// Hover the final answer -- the only one where regenerate is enabled.
await page.locator(".message-assistant").last().hover();
await page.waitForTimeout(400); // 140ms opacity transition
await shot("workspace");

// --- 2. Settings: model + generation controls --------------------------------
await page.goto(`${base}/settings`);
await page.getByRole("heading", { name: "Settings", exact: true }).first().waitFor();
await page.getByRole("button", { name: "AI Model" }).click();
await page.getByLabel("System instructions").waitFor({ state: "visible" });

await page.waitForTimeout(300);
await shot("settings");

// --- 3. Command palette over the workspace -----------------------------------
await page.goto(`${base}/`);
await page.getByLabel("Message Cortex").waitFor({ state: "visible" });
await page.keyboard.press("Control+k");
await page.waitForTimeout(500);
await shot("command-palette");

await browser.close();
console.log("Done.");
