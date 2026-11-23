/**
 * Record the README tour: a set of short scenes driven through the real
 * Cortex frontend against tools/screenshots/tour_server.py.
 *
 * tools/screenshots/tour.ps1 runs the whole thing, one fresh server per scene.
 * By hand, with a server already running:
 *
 *   node tools/screenshots/record_tour.mjs <bootstrap-token> --port 8802 \
 *        --project C:\cortex-tour\csv-importer --out build/tour [--scene chat]
 *
 * Nothing here is a screen recording. Every visual state is a 2x screenshot
 * taken after the page has settled, and the scene writes a timeline of what
 * happens between those states: how long each one holds, where the pointer
 * glides, where it clicks, which keys are pressed, and the caption. render_tour.py
 * turns that timeline into frames and encodes the GIFs. Because the shots are
 * discrete, a slow machine records the same tour as a fast one.
 *
 * CSS transitions and animations are switched off while recording; the
 * renderer cross-fades between states instead, so the motion is smooth and
 * identical on every run.
 */
import { createHash } from "node:crypto";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..", "..");
const requireFromFrontend = createRequire(resolve(repoRoot, "frontend", "package.json"));
const { chromium } = requireFromFrontend("@playwright/test");

const browserToken = process.argv[2];
if (!browserToken || browserToken.startsWith("--")) {
  console.error("usage: node record_tour.mjs <bootstrap-token> --project DIR [--port N] [--out DIR] [--scene NAME]");
  process.exit(1);
}
const flag = (name, fallback) => {
  const index = process.argv.indexOf(`--${name}`);
  return index === -1 ? fallback : process.argv[index + 1];
};
const port = Number(flag("port", 8802));
const outRoot = resolve(flag("out", resolve(repoRoot, "build", "tour")));
const projectPath = flag("project", "");
const only = flag("scene", "");
const base = `http://127.0.0.1:${port}`;

export const VIEWPORT = { width: 1280, height: 800 };
const SCALE = 2;

const NO_MOTION = `
  *, *::before, *::after {
    transition: none !important;
    animation: none !important;
    scroll-behavior: auto !important;
  }
`;

/** A deterministic jitter for typing, so it reads as a person, not a macro. */
function* rhythm(seed = 7) {
  let value = seed;
  for (;;) {
    value = (value * 1103515245 + 12345) % 2147483648;
    yield value / 2147483648;
  }
}

class Reel {
  constructor(page, name) {
    this.page = page;
    this.name = name;
    this.dir = resolve(outRoot, name);
    rmSync(this.dir, { recursive: true, force: true });
    mkdirSync(this.dir, { recursive: true });
    this.ops = [];
    this.count = 0;
    this.lastHash = "";
    this.lastShot = "";
    this.beat = rhythm(name.length * 31);
  }

  push(op) {
    this.ops.push(op);
  }

  async settle(ms = 120) {
    await this.page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
    if (ms) await this.page.waitForTimeout(ms);
  }

  async snap() {
    const buffer = await this.page.screenshot({ type: "png", caret: "initial" });
    const hash = createHash("sha1").update(buffer).digest("hex");
    if (hash === this.lastHash) return this.lastShot;
    const file = `${String(++this.count).padStart(4, "0")}.png`;
    writeFileSync(resolve(this.dir, file), buffer);
    this.lastHash = hash;
    this.lastShot = file;
    return file;
  }

  /** Show the page as it is now and keep it on screen for `ms`. */
  async hold(ms = 0) {
    this.push({ op: "show", shot: await this.snap() });
    if (ms) this.wait(ms);
  }

  /** Cross-fade from what is on screen to the page as it is now. */
  async fade(ms = 160) {
    this.push({ op: "fade", shot: await this.snap(), ms });
  }

  wait(ms) {
    this.push({ op: "wait", ms });
  }

  caption(label, text) {
    this.push({ op: "caption", label, text });
  }

  /** A keycap chip for a shortcut; `y` is the page height to centre it on. */
  keys(text, { ms = 900, y } = {}) {
    this.push({ op: "keys", text, ms, ...(y ? { y } : {}) });
  }

  camera(rect, ms = 0) {
    this.push({ op: "camera", rect, ms });
  }

  cursor(visible) {
    this.push({ op: "cursor", visible });
  }

  /** Bring an off-screen target into view with a quick cross-fade first. */
  async reveal(target) {
    const inView = await target.evaluate((node) => {
      const box = node.getBoundingClientRect();
      return box.top >= 56 && box.bottom <= innerHeight - 8;
    });
    if (inView) return;
    await target.evaluate((node) => node.scrollIntoView({ block: "center", behavior: "instant" }));
    await this.settle(200);
    await this.fade(260);
  }

  async point(target, offset = [0, 0]) {
    if (Array.isArray(target)) return target;
    await this.reveal(target);
    const box = await target.boundingBox();
    if (!box) throw new Error(`${this.name}: no box for pointer target`);
    return [Math.round(box.x + box.width / 2 + offset[0]), Math.round(box.y + box.height / 2 + offset[1])];
  }

  /** Glide the pointer to a target; the hover state fades in when it lands. */
  async moveTo(target, { ms = 650, offset } = {}) {
    const [x, y] = await this.point(target, offset);
    this.push({ op: "move", to: [x, y], ms });
    await this.page.mouse.move(x, y);
    await this.settle();
    await this.fade(110);
    return [x, y];
  }

  async click(target, { ms = 650, offset, after = 220, fadeMs = 170 } = {}) {
    await this.moveTo(target, { ms, offset });
    this.push({ op: "press" });
    await this.page.mouse.down();
    await this.page.mouse.up();
    await this.settle(after);
    await this.fade(fadeMs);
  }

  async press(key, label, { after = 220, fadeMs = 170, chip = true, y } = {}) {
    if (chip) this.keys(label ?? key, { y });
    await this.page.keyboard.press(key);
    await this.settle(after);
    await this.fade(fadeMs);
  }

  /** Type like a person: one shot per character, uneven but repeatable pace. */
  async type(text, { cps = 22 } = {}) {
    const mean = 1000 / cps;
    for (const character of text) {
      await this.page.keyboard.type(character);
      await this.settle(0);
      const jitter = 0.55 + this.beat.next().value * 0.9;
      const pause = /[ ,.:?!]/.test(character) ? 1.35 : 1;
      this.push({ op: "show", shot: await this.snap() });
      this.wait(Math.round((mean * jitter * pause) / 10) * 10);
    }
  }

  save(extra = {}) {
    const timeline = {
      name: this.name,
      viewport: [VIEWPORT.width, VIEWPORT.height],
      scale: SCALE,
      ...extra,
      ops: this.ops,
    };
    writeFileSync(resolve(this.dir, "timeline.json"), JSON.stringify(timeline, null, 1));
    console.log(`  ${this.name}: ${this.count} shots, ${this.ops.length} steps`);
  }
}

const post = async (path) => {
  const response = await fetch(`${base}${path}`, { method: "POST" });
  if (!response.ok) throw new Error(`${path} -> ${response.status}`);
  return response.json();
};

// ---------------------------------------------------------------------------

const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: VIEWPORT,
  deviceScaleFactor: SCALE,
  colorScheme: "dark",
  locale: "en-US",
  timezoneId: "UTC",
  permissions: ["clipboard-read", "clipboard-write"],
});
await context.addInitScript((css) => {
  addEventListener("DOMContentLoaded", () => {
    const style = document.createElement("style");
    style.dataset.tour = "no-motion";
    style.textContent = css;
    document.head.append(style);
  });
}, NO_MOTION);

const page = await context.newPage();
await page.goto(`${base}/#bootstrap=${encodeURIComponent(browserToken)}`);
await page.getByLabel("Message Cortex").waitFor({ state: "visible" });
await page.waitForTimeout(600);
await page.mouse.move(1010, 560);

const home = async () => {
  await page.goto(`${base}/`);
  await page.getByLabel("Message Cortex").waitFor({ state: "visible" });
  await page.waitForTimeout(500);
};

const scenes = {};

/** Release streamed tokens or Agent model replies on the tour server. */
const release = (count = 1) => post(`/showcase/step?count=${count}`);
const mainText = () => page.evaluate(() => document.querySelector("main")?.innerText ?? "");
const waitForChange = (before, timeout = 4000) =>
  page.waitForFunction((text) => (document.querySelector("main")?.innerText ?? "") !== text, before, { timeout })
    .catch(() => {});
const toastsGone = async () => {
  const toast = page.locator(".toast");
  // A save confirms itself a moment later; wait for that toast to appear
  // first, or this would pass before there was anything to wait for.
  await toast.first().waitFor({ state: "attached", timeout: 1500 }).catch(() => {});
  await toast.first().waitFor({ state: "detached", timeout: 9000 }).catch(() => {});
};
/** Scroll whatever scrolls `target` so it sits `margin` px below the top bar. */
const pinToTop = (target, margin = 64) => target.evaluate((node, gap) => {
  let box = node.parentElement;
  while (box && !(box.scrollHeight > box.clientHeight && /(auto|scroll)/.test(getComputedStyle(box).overflowY))) {
    box = box.parentElement;
  }
  const scroller = box ?? document.scrollingElement;
  scroller.scrollTop += node.getBoundingClientRect().top - gap;
}, margin);
const openChat = async (title) => {
  await page.getByRole("button", { name: title, exact: true }).first().click();
  await page.waitForTimeout(700);
};

// ---------------------------------------------------------------------------
// Chat: a question, the answer streaming in, the answer's own controls.
// ---------------------------------------------------------------------------
scenes.chat = async () => {
  await home();
  const reel = new Reel(page, "chat");
  reel.caption("Chat", "Ask a local model anything");
  await reel.hold(900);
  await reel.click(page.getByLabel("Message Cortex"), { ms: 700 });
  await reel.type("Some Excel CSV exports break my importer: row[\"id\"] raises KeyError even though the header says id. Why?", { cps: 32 });
  reel.wait(400);
  reel.caption("Chat", "The answer streams in as the model writes it");
  await reel.press("Enter", "Enter", { fadeMs: 220 });
  reel.wait(300);

  const finished = () => page.locator(".message-assistant").last().getByText(/tok\/s/).count();
  for (let index = 0; index < 90; index += 1) {
    const before = await mainText();
    await release(3);
    await waitForChange(before, 1500);
    await reel.settle(40);
    await reel.hold(70);
    if (await finished()) break;
  }
  await release(60);
  await page.locator(".message-assistant").last().getByText(/tok\/s/).waitFor({ timeout: 10000 });
  await reel.settle(400);
  await reel.fade(220);
  reel.wait(900);

  reel.caption("Chat", "Copy the code, regenerate, or fork the thread from any answer");
  const answer = page.locator(".message-assistant").last();
  await reel.click(answer.getByRole("button", { name: /^Copy python code$/ }), { ms: 700 });
  reel.wait(1100);
  await page.waitForTimeout(1300); // the button says "Copied" for 1.2 s
  await reel.fade(200);
  await reel.moveTo(answer.getByRole("button", { name: "Fork chat from this message" }), { ms: 700 });
  reel.wait(2600);
  reel.save();
};

// ---------------------------------------------------------------------------
// Keyboard: the command palette opens a chat, switches model and theme.
// ---------------------------------------------------------------------------
scenes.palette = async () => {
  await home();
  // The theme this scene switches to is "follow the system", and the system
  // here is light.
  await page.emulateMedia({ colorScheme: "light" });
  await openChat("Reading a 4 GB CSV without exhausting memory");
  await page.evaluate(() => (document.activeElement instanceof HTMLElement) && document.activeElement.blur());
  await page.mouse.move(1240, 430);
  await page.waitForTimeout(300);
  const reel = new Reel(page, "palette");
  reel.cursor(false);
  const chipY = 96;
  reel.caption("Keyboard", "Ctrl+K opens the command palette");
  await reel.hold(1100);
  await reel.press("Control+k", "Ctrl K", { y: chipY });
  reel.wait(900);
  reel.caption("Keyboard", "Jump to any chat by name");
  await reel.type("postgres", { cps: 11 });
  reel.wait(800);
  await reel.press("Enter", "Enter", { y: chipY, fadeMs: 120 });
  reel.wait(1800);

  reel.caption("Keyboard", "Follow the system theme, or pin light or dark");
  await reel.press("Control+k", "Ctrl K", { y: chipY });
  reel.wait(400);
  await reel.type("theme", { cps: 11 });
  reel.wait(800);
  reel.keys("Enter", { y: chipY });
  await page.keyboard.press("Enter");
  // Cut straight to the new theme once the "Settings saved." toast is gone:
  // a cross-fade between themes repaints every pixel for no benefit.
  await toastsGone();
  await reel.settle(300);
  await reel.hold(3200);
  reel.save();
};

// ---------------------------------------------------------------------------
// Models: the composer's model picker and per-chat parameters, then Settings.
// ---------------------------------------------------------------------------
scenes.models = async () => {
  await home();
  await openChat("Vulkan or CPU for local inference");
  const reel = new Reel(page, "models");
  reel.caption("Models", "Pick any installed model from the composer");
  await reel.hold(900);
  await reel.click(page.getByRole("button", { name: /^Selected local model:/ }), { ms: 800 });
  reel.wait(900);
  const menu = page.getByLabel(/^Discovered .*models$/);
  await reel.moveTo(menu.getByText("nemotron-3-nano:4b").first(), { ms: 500 });
  reel.wait(300);
  await reel.click(menu.getByText("granite4.1:8b").first(), { ms: 450 });
  reel.wait(1400);
  await toastsGone();
  await reel.fade(200);
  reel.wait(300);

  reel.caption("Models", "Tune sampling and context for this chat only");
  await reel.click(page.getByLabel("Generation parameters for this chat"), { ms: 700 });
  reel.wait(900);
  const popover = page.locator(".params-popover");
  await reel.click(popover.getByText("Precise", { exact: true }), { ms: 700 });
  reel.wait(900);
  await reel.click(popover.getByText("16K", { exact: true }), { ms: 650 });
  reel.wait(1300);
  await reel.press("Escape", "Esc", { chip: false });
  reel.wait(400);

  reel.caption("Models", "Settings shows each model's size, quantization and context");
  await reel.click(page.getByRole("link", { name: "Settings", exact: true }), { ms: 800, after: 500 });
  reel.wait(600);
  await reel.click(page.getByRole("button", { name: "System", exact: true }).or(page.getByRole("tab", { name: "System", exact: true })).first(), { ms: 700, after: 600 });
  reel.wait(3000);
  reel.save();
};

// ---------------------------------------------------------------------------
// Approval-gated Python: nothing runs until the user has read it and allowed it.
// ---------------------------------------------------------------------------
scenes.approval = async () => {
  await home();
  await post("/showcase/execution/stage?completed=false");
  await openChat("Monthly payment on a car loan");
  await page.locator(".execution-task-tray").waitFor({ state: "visible" });
  await page.waitForTimeout(1200);
  const reel = new Reel(page, "approval");
  reel.caption("Code", "The model proposes Python; it waits for you");
  await reel.hold(1800);
  const tray = page.locator(".execution-task-tray");
  reel.caption("Code", "Read the exact source and the access it asks for");
  await reel.click(tray.getByText("Review generated source").first(), { ms: 800, after: 900 });
  reel.wait(2200);
  reel.caption("Code", "Allow it once; the restricted interpreter runs it");
  await reel.click(tray.getByRole("button", { name: /^Allow background task .* once$/ }), { ms: 800, after: 300 });
  reel.wait(500);
  const finished = await post("/showcase/execution/finish/code-loan-payment");
  if (!finished.finished) throw new Error(`approval: task did not finish (${JSON.stringify(finished)})`);
  await tray.getByText(/Succeeded/).first().waitFor({ timeout: 10000 });
  await toastsGone();
  await reel.settle(500);
  await reel.fade(260);
  reel.wait(3400);
  reel.save();
};

// ---------------------------------------------------------------------------
// Agent mode: a real run against a sample project, with a scripted model.
// ---------------------------------------------------------------------------
scenes.agent = async () => {
  if (!projectPath) throw new Error("agent scene needs --project");
  await page.goto(`${base}/agent`);
  await page.locator("#agent-goal").waitFor();
  await page.waitForTimeout(500);
  await page.mouse.move(1010, 300);
  const reel = new Reel(page, "agent");
  reel.caption("Agent", "Give Agent mode a goal and a project folder");
  await reel.hold(900);
  await reel.click(page.locator("#agent-goal"), { ms: 650 });
  await reel.type("Excel exports make customer_ids() fail with KeyError: 'id'. Fix the importer.", { cps: 55 });
  reel.wait(250);
  await reel.click(page.locator("#agent-folder"), { ms: 550 });
  await reel.type(projectPath, { cps: 50 });
  reel.wait(300);
  await reel.click(page.getByRole("checkbox").first(), { ms: 600 });
  reel.wait(300);
  await reel.click(page.getByRole("radio", { name: /Ask before every command/ }), { ms: 700 });
  reel.wait(400);
  reel.caption("Agent", "It works in a copy of the project");
  await reel.click(page.getByRole("button", { name: "Start Agent" }), { ms: 700, after: 900 });
  const run = page.getByText("Live timeline", { exact: true }).first();
  await run.waitFor({ timeout: 10000 });
  await pinToTop(run, 150);
  await reel.settle(300);
  await reel.fade(220);
  reel.wait(600);

  // Model steps: plan, search, read, plan, then the edit that needs approval.
  const latest = () => page.locator("main details, main li").last();
  for (let index = 0; index < 5; index += 1) {
    const before = await mainText();
    await release(1);
    await waitForChange(before);
    await reel.settle(700);
    await pinToTop(run, 150);
    await reel.fade(160);
    reel.wait(index === 4 ? 600 : 520);
  }
  reel.caption("Agent", "Every edit asks first, and shows exactly what will change");
  const details = page.getByRole("button", { name: "Show details" }).first();
  await details.waitFor({ timeout: 10000 });
  reel.wait(600);
  await reel.click(details, { ms: 700, after: 300 });
  reel.wait(2400);
  await reel.click(page.getByRole("button", { name: "Allow once" }).first(), { ms: 700, after: 700 });
  reel.caption("Agent", "Review the diff, then accept it into your project");
  reel.wait(400);
  for (let index = 0; index < 3; index += 1) {
    const before = await mainText();
    await release(1);
    await waitForChange(before);
    await reel.settle(900);
    await pinToTop(run, 150);
    await reel.fade(160);
    reel.wait(420);
  }
  await toastsGone();

  const show = page.getByRole("button", { name: "Show changes" }).first();
  await show.waitFor({ timeout: 15000 });
  await reel.settle(400);
  await reel.fade(220);
  reel.wait(900);
  await reel.click(show, { ms: 700, after: 500 });
  reel.wait(2600);
  await reel.click(page.getByRole("button", { name: "Accept selected to project" }).first(), { ms: 700, after: 400 });
  reel.wait(900);
  await reel.click(page.getByRole("button", { name: "Confirm accept" }).first(), { ms: 650, after: 900 });
  await page.getByText(/Accepted to the project/).first().waitFor({ timeout: 10000 }).catch(() => {});
  await reel.settle(400);
  await reel.fade(200);
  reel.wait(3000);
  reel.save();
};

// ---------------------------------------------------------------------------

const order = ["chat", "palette", "models", "approval", "agent"];
for (const name of order) {
  if (only && only !== name) continue;
  console.log(`Recording ${name}...`);
  await scenes[name]();
}
await browser.close();
