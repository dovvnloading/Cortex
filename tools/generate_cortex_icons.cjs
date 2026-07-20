const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require("../frontend/node_modules/playwright");

const ROOT = path.resolve(__dirname, "..");
const SOURCE = path.join(ROOT, "assets", "cortex.svg");
const FRONTEND_ICON = path.join(ROOT, "frontend", "public", "cortex.svg");
const WINDOWS_ICON = path.join(ROOT, "assets", "cortex.ico");
const SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256];

function buildIco(images) {
  const headerSize = 6;
  const entrySize = 16;
  let imageOffset = headerSize + entrySize * images.length;
  const header = Buffer.alloc(imageOffset);

  header.writeUInt16LE(0, 0);
  header.writeUInt16LE(1, 2);
  header.writeUInt16LE(images.length, 4);

  images.forEach(({ size, png }, index) => {
    const offset = headerSize + index * entrySize;
    header.writeUInt8(size === 256 ? 0 : size, offset);
    header.writeUInt8(size === 256 ? 0 : size, offset + 1);
    header.writeUInt8(0, offset + 2);
    header.writeUInt8(0, offset + 3);
    header.writeUInt16LE(1, offset + 4);
    header.writeUInt16LE(32, offset + 6);
    header.writeUInt32LE(png.length, offset + 8);
    header.writeUInt32LE(imageOffset, offset + 12);
    imageOffset += png.length;
  });

  return Buffer.concat([header, ...images.map(({ png }) => png)]);
}

async function main() {
  const svg = fs.readFileSync(SOURCE, "utf8");
  fs.mkdirSync(path.dirname(FRONTEND_ICON), { recursive: true });
  fs.copyFileSync(SOURCE, FRONTEND_ICON);

  const browser = await chromium.launch({ headless: true });
  try {
    const images = [];
    for (const size of SIZES) {
      const page = await browser.newPage({
        viewport: { width: size, height: size },
        deviceScaleFactor: 1,
      });
      await page.setContent(
        `<style>html,body{margin:0;width:100%;height:100%;overflow:hidden}svg{display:block;width:100%;height:100%}</style>${svg}`,
      );
      images.push({ size, png: await page.screenshot({ type: "png" }) });
      await page.close();
    }
    fs.writeFileSync(WINDOWS_ICON, buildIco(images));

    if (process.argv.includes("--preview")) {
      const preview = images.at(-1).png;
      const previewPath = path.join(os.tmpdir(), "cortex-icon-preview.png");
      fs.writeFileSync(previewPath, preview);
      console.log(`Preview: ${previewPath}`);
    }
  } finally {
    await browser.close();
  }

  console.log(`Generated ${WINDOWS_ICON} from ${SOURCE}`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
