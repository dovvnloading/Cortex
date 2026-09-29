import { afterEach, describe, expect, it } from "vitest";
import {
  ATTACHMENT_ACCEPT,
  blockStrayFileDrops,
  carriesFiles,
  describeUnsupportedFiles,
  filesFromTransfer,
  isSupportedAttachment,
  splitSupportedFiles,
} from "./attachments";

const file = (name: string, type = "") => new File(["x"], name, { type });

describe("supported attachment types", () => {
  it("accepts images and text by MIME type whatever the name", () => {
    expect(isSupportedAttachment(file("image.png", "image/png"))).toBe(true);
    expect(isSupportedAttachment(file("clip", "image/webp"))).toBe(true);
    expect(isSupportedAttachment(file("notes", "text/plain"))).toBe(true);
    expect(isSupportedAttachment(file("NOTES.TXT", "TEXT/PLAIN"))).toBe(true);
  });

  it("accepts documents and code by extension when the system gives no MIME type", () => {
    for (const name of ["a.md", "b.PY", "c.tsx", "d.json", "e.yaml", "f.ipynb", "g.mjs", "h.zig", "archive.tar.gz.txt"]) {
      expect(isSupportedAttachment(file(name)), name).toBe(true);
    }
  });

  it("accepts the well-known text files that have no extension", () => {
    for (const name of ["Dockerfile", "Makefile", ".gitignore", ".editorconfig", ".env"]) {
      expect(isSupportedAttachment(file(name)), name).toBe(true);
    }
  });

  it("turns away binaries, archives and documents Cortex cannot read", () => {
    for (const name of ["setup.exe", "photo.psd", "bundle.zip", "report.pdf", "song.mp3", "movie.mp4", "noextension", "trailingdot."]) {
      expect(isSupportedAttachment(file(name, "application/octet-stream")), name).toBe(false);
    }
    expect(isSupportedAttachment(file("report.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))).toBe(false);
  });

  it("builds the picker's accept list from the same rules", () => {
    const accept = ATTACHMENT_ACCEPT.split(",");
    expect(accept).toEqual(expect.arrayContaining(["image/*", "text/*", ".md", ".py", ".editorconfig"]));
    expect(accept).not.toContain(".exe");
    expect(new Set(accept).size).toBe(accept.length);
  });

  it("splits a batch into what can be attached and what cannot", () => {
    const good = file("a.md", "text/markdown");
    const bad = file("b.exe");
    const image = file("c.png", "image/png");

    expect(splitSupportedFiles([good, bad, image])).toEqual({ supported: [good, image], rejected: [bad] });
  });

  it("names a single skipped file and counts several", () => {
    expect(describeUnsupportedFiles([file("setup.exe")])).toContain("setup.exe is not a supported attachment");
    expect(describeUnsupportedFiles([file("a.exe"), file("b.zip")])).toContain("2 files were skipped");
  });
});

describe("reading files from a transfer", () => {
  it("recognises a drag that carries files, and not one that carries only text", () => {
    expect(carriesFiles({ types: ["Files"] })).toBe(true);
    expect(carriesFiles({ types: ["text/plain", "Files"] })).toBe(true);
    expect(carriesFiles({ types: ["text/plain", "text/html"] })).toBe(false);
    expect(carriesFiles(null)).toBe(false);
    expect(carriesFiles(undefined)).toBe(false);
  });

  it("prefers the file list and falls back to file items", () => {
    const listed = file("listed.md");
    const viaItem = file("item.png", "image/png");
    const item = (kind: string, value: File | null) => ({ kind, getAsFile: () => value }) as unknown as DataTransferItem;

    expect(filesFromTransfer({ files: [listed] as unknown as FileList, items: [item("file", viaItem)] as unknown as DataTransferItemList })).toEqual([listed]);
    expect(filesFromTransfer({
      files: [] as unknown as FileList,
      items: [item("string", null), item("file", viaItem), item("file", null)] as unknown as DataTransferItemList,
    })).toEqual([viaItem]);
    expect(filesFromTransfer(null)).toEqual([]);
  });
});

describe("blockStrayFileDrops", () => {
  let remove: (() => void) | undefined;
  afterEach(() => {
    remove?.();
    remove = undefined;
  });

  const drag = (type: "dragover" | "drop", types: string[], onDocument = document) => {
    const event = new Event(type, { bubbles: true, cancelable: true });
    const dataTransfer = { types, dropEffect: "copy" };
    Object.defineProperty(event, "dataTransfer", { value: dataTransfer });
    onDocument.body.dispatchEvent(event);
    return { event, dataTransfer };
  };

  it("cancels a file dragged or dropped over the page so it cannot open in the window", () => {
    remove = blockStrayFileDrops();

    const over = drag("dragover", ["Files"]);
    const dropped = drag("drop", ["Files"]);

    expect(over.event.defaultPrevented).toBe(true);
    expect(dropped.event.defaultPrevented).toBe(true);
  });

  it("shows the not-allowed cursor over the page, where nothing accepts the file", () => {
    remove = blockStrayFileDrops();

    expect(drag("dragover", ["Files"]).dataTransfer.dropEffect).toBe("none");
  });

  it("leaves a drag a drop target already accepted as the target set it", () => {
    remove = blockStrayFileDrops();
    const target = document.createElement("div");
    document.body.appendChild(target);
    target.addEventListener("dragover", (event) => {
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
    });
    const event = new Event("dragover", { bubbles: true, cancelable: true });
    const dataTransfer = { types: ["Files"], dropEffect: "none" };
    Object.defineProperty(event, "dataTransfer", { value: dataTransfer });

    target.dispatchEvent(event);
    target.remove();

    expect(event.defaultPrevented).toBe(true);
    expect(dataTransfer.dropEffect).toBe("copy");
  });

  it("does not touch a dragged piece of text, which the composer must still accept", () => {
    remove = blockStrayFileDrops();

    expect(drag("dragover", ["text/plain"]).event.defaultPrevented).toBe(false);
    expect(drag("drop", ["text/plain"]).event.defaultPrevented).toBe(false);
  });

  it("stops cancelling once removed", () => {
    remove = blockStrayFileDrops();
    remove();
    remove = undefined;

    expect(drag("drop", ["Files"]).event.defaultPrevented).toBe(false);
  });
});
