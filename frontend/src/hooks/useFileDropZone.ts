import { useEffect, useRef, useState, type DragEvent } from "react";
import { carriesFiles, filesFromTransfer } from "../lib/attachments";

type Options = {
  /** A disabled zone claims nothing, so a drop on it is refused by the window. */
  disabled?: boolean;
  onFiles: (files: File[]) => void;
};

export type FileDropZone = {
  /** True while a drag that carries files is over the zone. */
  active: boolean;
  handlers: {
    onDragEnter: (event: DragEvent<HTMLElement>) => void;
    onDragOver: (event: DragEvent<HTMLElement>) => void;
    onDragLeave: (event: DragEvent<HTMLElement>) => void;
    onDrop: (event: DragEvent<HTMLElement>) => void;
  };
};

/**
 * Turns an element into a drop target for files.
 *
 * A zone handles the drags it sees and stops them there, so a zone nested in
 * another one (the composer inside the chat page) is the only one that reacts
 * while the pointer is over it. Drags that do not carry files (selected text)
 * are ignored entirely.
 */
export function useFileDropZone({ disabled = false, onFiles }: Options): FileDropZone {
  const [active, setActive] = useState(false);
  // dragenter/dragleave fire for every element the pointer crosses; counting
  // them is the reliable way to know when it has left the zone altogether.
  const depth = useRef(0);

  // The count can drift if the element under the pointer is replaced mid-drag,
  // because a removed element never reports leaving. Whatever else happens,
  // the drag is over once it is dropped anywhere, cancelled, or taken out of
  // the window (the last dragleave then reports a position outside it), so the
  // target must not stay lit past that.
  useEffect(() => {
    if (!active) return undefined;
    const reset = () => {
      depth.current = 0;
      setActive(false);
    };
    const resetWhenLeavingWindow = (event: DragEvent | globalThis.DragEvent) => {
      const outside = event.clientX <= 0 || event.clientY <= 0
        || event.clientX >= window.innerWidth || event.clientY >= window.innerHeight;
      if (outside) reset();
    };
    document.addEventListener("drop", reset, true);
    document.addEventListener("dragend", reset, true);
    document.addEventListener("dragleave", resetWhenLeavingWindow as EventListener, true);
    return () => {
      document.removeEventListener("drop", reset, true);
      document.removeEventListener("dragend", reset, true);
      document.removeEventListener("dragleave", resetWhenLeavingWindow as EventListener, true);
    };
  }, [active]);

  const claims = (event: DragEvent<HTMLElement>) => !disabled && carriesFiles(event.dataTransfer);

  return {
    active,
    handlers: {
      onDragEnter: (event) => {
        if (!claims(event)) return;
        event.preventDefault();
        event.stopPropagation();
        depth.current += 1;
        setActive(true);
      },
      onDragOver: (event) => {
        if (!claims(event)) return;
        event.preventDefault();
        event.stopPropagation();
        event.dataTransfer.dropEffect = "copy";
      },
      onDragLeave: (event) => {
        if (!claims(event)) return;
        event.stopPropagation();
        depth.current = Math.max(0, depth.current - 1);
        if (depth.current === 0) setActive(false);
      },
      onDrop: (event) => {
        if (!claims(event)) return;
        event.preventDefault();
        event.stopPropagation();
        depth.current = 0;
        setActive(false);
        const files = filesFromTransfer(event.dataTransfer);
        if (files.length) onFiles(files);
      },
    },
  };
}
