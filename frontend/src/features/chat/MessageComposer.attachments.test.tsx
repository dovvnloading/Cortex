import { useState } from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ChatAttachment } from "../../../../contracts/cortex-api";
import { MessageComposer } from "./MessageComposer";

function Composer({
  attachments = [],
  attachmentPreviews,
  onAddAttachments,
}: {
  attachments?: readonly ChatAttachment[];
  attachmentPreviews?: Readonly<Record<string, string>>;
  onAddAttachments?: (files: File[]) => Promise<void> | void;
}) {
  const [value, setValue] = useState("");
  return (
    <MessageComposer
      value={value}
      phase="ready"
      selectedModel="local-chat:7b"
      localModels={["local-chat:7b"]}
      attachments={attachments}
      attachmentPreviews={attachmentPreviews}
      onAddAttachments={onAddAttachments}
      onValueChange={setValue}
      onSubmit={vi.fn().mockResolvedValue(true)}
      onStop={vi.fn()}
      onSelectModel={vi.fn().mockResolvedValue(true)}
      onGenerationOptionsChange={vi.fn()}
    />
  );
}

const screenshot = () => new File([new Uint8Array([137, 80, 78, 71])], "image.png", { type: "image/png" });
const clipboard = (files: File[], text = "") => ({
  files,
  items: [],
  types: [...(files.length ? ["Files"] : []), ...(text ? ["text/plain"] : [])],
  getData: (format: string) => (format === "text/plain" ? text : ""),
});
const fileDrag = (files: File[]) => ({ files, items: [], types: ["Files"], dropEffect: "none" });
const surfaceOf = (container: HTMLElement) => container.querySelector(".composer-surface") as HTMLElement;

describe("MessageComposer paste", () => {
  it("stages a pasted screenshot and keeps it out of the textarea", () => {
    const onAddAttachments = vi.fn();
    render(<Composer onAddAttachments={onAddAttachments} />);
    const file = screenshot();

    const notPrevented = fireEvent.paste(screen.getByLabelText("Message Cortex"), { clipboardData: clipboard([file]) });

    expect(notPrevented).toBe(false);
    expect(onAddAttachments).toHaveBeenCalledWith([file]);
  });

  it("leaves a plain-text paste to the textarea", () => {
    const onAddAttachments = vi.fn();
    render(<Composer onAddAttachments={onAddAttachments} />);

    const notPrevented = fireEvent.paste(screen.getByLabelText("Message Cortex"), {
      clipboardData: clipboard([], "just some words"),
    });

    expect(notPrevented).toBe(true);
    expect(onAddAttachments).not.toHaveBeenCalled();
  });

  it("pastes the text when the clipboard holds both text and a picture of it", () => {
    // Copying spreadsheet cells or a passage from a document puts an image of
    // the selection on the clipboard as well as the text. Taking that over
    // would leave no way to paste the text.
    const onAddAttachments = vi.fn();
    render(<Composer onAddAttachments={onAddAttachments} />);

    const notPrevented = fireEvent.paste(screen.getByLabelText("Message Cortex"), {
      clipboardData: clipboard([screenshot()], "A1 B1"),
    });

    expect(notPrevented).toBe(true);
    expect(onAddAttachments).not.toHaveBeenCalled();
  });

  it("does not take over a file paste when it cannot attach anything", () => {
    render(<Composer />);

    const notPrevented = fireEvent.paste(screen.getByLabelText("Message Cortex"), { clipboardData: clipboard([screenshot()]) });

    expect(notPrevented).toBe(true);
  });
});

describe("MessageComposer drop", () => {
  it("stages dropped files and shows the drop target only while a file is over it", () => {
    const onAddAttachments = vi.fn();
    const { container } = render(<Composer onAddAttachments={onAddAttachments} />);
    const file = new File(["# Notes"], "notes.md", { type: "text/markdown" });

    fireEvent.dragEnter(surfaceOf(container), { dataTransfer: fileDrag([file]) });
    expect(screen.getByText("Drop to attach")).toBeInTheDocument();

    const notPrevented = fireEvent.drop(surfaceOf(container), { dataTransfer: fileDrag([file]) });

    expect(notPrevented).toBe(false);
    expect(onAddAttachments).toHaveBeenCalledWith([file]);
    expect(screen.queryByText("Drop to attach")).not.toBeInTheDocument();
  });

  it("keeps the drop target up while the pointer crosses the composer's children", () => {
    const { container } = render(<Composer onAddAttachments={vi.fn()} />);
    const textarea = screen.getByLabelText("Message Cortex");
    const dataTransfer = fileDrag([]);

    fireEvent.dragEnter(surfaceOf(container), { dataTransfer });
    fireEvent.dragEnter(textarea, { dataTransfer });
    fireEvent.dragLeave(surfaceOf(container), { dataTransfer });
    expect(screen.getByText("Drop to attach")).toBeInTheDocument();

    fireEvent.dragLeave(textarea, { dataTransfer });
    expect(screen.queryByText("Drop to attach")).not.toBeInTheDocument();
  });

  describe("when the pointer count cannot be trusted", () => {
    // A removed element never reports the pointer leaving it, so the count of
    // entered elements can drift. The target must still go dark when the drag
    // is over.
    const dragEvent = (type: string, position?: { x: number; y: number }) => {
      const event = new Event(type, { bubbles: true, cancelable: true });
      if (position) {
        Object.defineProperty(event, "clientX", { value: position.x });
        Object.defineProperty(event, "clientY", { value: position.y });
      }
      return event;
    };
    const lit = (container: HTMLElement) => {
      fireEvent.dragEnter(surfaceOf(container), { dataTransfer: fileDrag([]) });
      fireEvent.dragEnter(screen.getByLabelText("Message Cortex"), { dataTransfer: fileDrag([]) });
      expect(screen.getByText("Drop to attach")).toBeInTheDocument();
    };

    it("goes dark when the drag leaves the window", () => {
      const { container } = render(<Composer onAddAttachments={vi.fn()} />);
      lit(container);

      act(() => { document.body.dispatchEvent(dragEvent("dragleave", { x: 0, y: 0 })); });

      expect(screen.queryByText("Drop to attach")).not.toBeInTheDocument();
    });

    it("stays lit for a leave that happens inside the window", () => {
      const { container } = render(<Composer onAddAttachments={vi.fn()} />);
      lit(container);

      act(() => { document.body.dispatchEvent(dragEvent("dragleave", { x: 120, y: 80 })); });

      expect(screen.getByText("Drop to attach")).toBeInTheDocument();
    });

    it("goes dark when the file is dropped somewhere else", () => {
      const { container } = render(<Composer onAddAttachments={vi.fn()} />);
      lit(container);

      act(() => { document.body.dispatchEvent(dragEvent("drop")); });

      expect(screen.queryByText("Drop to attach")).not.toBeInTheDocument();
    });

    it("goes dark when the drag is cancelled", () => {
      const { container } = render(<Composer onAddAttachments={vi.fn()} />);
      lit(container);

      act(() => { document.body.dispatchEvent(dragEvent("dragend")); });

      expect(screen.queryByText("Drop to attach")).not.toBeInTheDocument();
    });
  });

  it("marks the drag as a copy so the drop is accepted", () => {
    const { container } = render(<Composer onAddAttachments={vi.fn()} />);
    const dataTransfer = fileDrag([]);

    const notPrevented = fireEvent.dragOver(surfaceOf(container), { dataTransfer });

    expect(notPrevented).toBe(false);
    expect(dataTransfer.dropEffect).toBe("copy");
  });

  it("ignores a dragged piece of text so it can still be dropped into the textarea", () => {
    const onAddAttachments = vi.fn();
    const { container } = render(<Composer onAddAttachments={onAddAttachments} />);
    const text = { files: [], items: [], types: ["text/plain"], dropEffect: "none" };

    expect(fireEvent.dragEnter(surfaceOf(container), { dataTransfer: text })).toBe(true);
    expect(fireEvent.drop(surfaceOf(container), { dataTransfer: text })).toBe(true);

    expect(screen.queryByText("Drop to attach")).not.toBeInTheDocument();
    expect(onAddAttachments).not.toHaveBeenCalled();
  });

  it("claims nothing when it cannot attach", () => {
    const { container } = render(<Composer />);

    expect(fireEvent.dragEnter(surfaceOf(container), { dataTransfer: fileDrag([]) })).toBe(true);
    expect(fireEvent.drop(surfaceOf(container), { dataTransfer: fileDrag([screenshot()]) })).toBe(true);
    expect(screen.queryByText("Drop to attach")).not.toBeInTheDocument();
  });

  it("stops a drop it handled from reaching an enclosing drop target", () => {
    const outer = vi.fn();
    const { container } = render(
      <div onDrop={outer}>
        <Composer onAddAttachments={vi.fn()} />
      </div>,
    );

    fireEvent.drop(surfaceOf(container), { dataTransfer: fileDrag([screenshot()]) });

    expect(outer).not.toHaveBeenCalled();
  });
});

describe("MessageComposer staged chips", () => {
  const base = { mime_type: "x", sha256: "c".repeat(64), expires_at: "2099-01-01T00:00:00Z" };

  it("shows a size on every chip and a thumbnail for an image that has one", () => {
    const image: ChatAttachment = { ...base, attachment_id: "image-1", filename: "photo.png", size: 2_500_000, kind: "image" };
    const document: ChatAttachment = { ...base, attachment_id: "doc-1", filename: "notes.md", size: 900, kind: "document" };
    const { container } = render(
      <Composer attachments={[image, document]} attachmentPreviews={{ "image-1": "blob:thumb-1", "doc-1": "blob:ignored" }} />,
    );

    expect(screen.getByText("2.4 MB")).toBeVisible();
    expect(screen.getByText("900 B")).toBeVisible();
    const thumbs = container.querySelectorAll("img.composer-attachment-thumb");
    // A document never shows a picture, even if one were supplied for it.
    expect(thumbs).toHaveLength(1);
    expect(thumbs[0]).toHaveAttribute("src", "blob:thumb-1");
    expect(thumbs[0]).toHaveAttribute("alt", "");
  });

  it("falls back to the icon for an image restored without a thumbnail", () => {
    const image: ChatAttachment = { ...base, attachment_id: "image-2", filename: "restored.png", size: 10, kind: "image" };
    const { container } = render(<Composer attachments={[image]} />);

    expect(container.querySelector("img.composer-attachment-thumb")).toBeNull();
    expect(container.querySelector(".composer-attachment > svg")).not.toBeNull();
  });
});
