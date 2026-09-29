/**
 * What a message may attach, and how files reach the composer from the file
 * picker, the clipboard, or a drag-and-drop.
 *
 * This is a courtesy filter, not a security boundary: the backend classifies
 * every upload by its content and enforces the size and count limits. The
 * extension list mirrors the backend's text-document list so a file is not
 * accepted here and refused there (a test compares the two).
 */

const ATTACHMENT_EXTENSIONS = [
  ".adoc", ".asm", ".astro", ".bash", ".bat", ".c", ".cc", ".cfg", ".clj", ".cljs", ".coffee",
  ".conf", ".cpp", ".cs", ".cshtml", ".css", ".csv", ".d", ".dart", ".diff", ".dockerfile",
  ".elm", ".env", ".ex", ".exs", ".fs", ".fsx", ".gitattributes", ".gitignore", ".go", ".gql",
  ".gradle", ".graphql", ".groovy", ".h", ".handlebars", ".hbs", ".hcl", ".hpp", ".hs", ".htm",
  ".html", ".ini", ".ipynb", ".java", ".js", ".json", ".jsonl", ".jsx", ".kt", ".kts", ".less",
  ".lhs", ".lock", ".log", ".lua", ".m", ".map", ".markdown", ".md", ".mjs", ".mm", ".mustache",
  ".ndjson", ".nim", ".org", ".pas", ".patch", ".php", ".pl", ".plist", ".properties", ".proto",
  ".ps1", ".py", ".pyi", ".r", ".razor", ".rb", ".rs", ".rst", ".rtf", ".sass", ".scss", ".sh",
  ".sol", ".sql", ".srt", ".svg", ".swift", ".tex", ".text", ".tf", ".tfvars", ".toml", ".ts",
  ".tsv", ".tsx", ".txt", ".vbs", ".vtt", ".vue", ".xhtml", ".xml", ".xsd", ".yaml", ".yml", ".zig",
] as const;

/** Well-known text files with no extension, or a name that is only a dot-extension. */
const ATTACHMENT_FILENAMES: ReadonlySet<string> = new Set([
  ".dockerignore", ".editorconfig", ".env", ".gitattributes", ".gitignore", "dockerfile", "makefile",
]);

const EXTENSION_SET: ReadonlySet<string> = new Set(ATTACHMENT_EXTENSIONS);

/** Value for the file picker's `accept` attribute. */
export const ATTACHMENT_ACCEPT = ["image/*", "text/*", ".editorconfig", ...ATTACHMENT_EXTENSIONS].join(",");

export function isSupportedAttachment(file: Pick<File, "name" | "type">): boolean {
  const type = file.type.toLowerCase();
  if (type.startsWith("image/") || type.startsWith("text/")) return true;
  const name = file.name.toLowerCase();
  if (ATTACHMENT_FILENAMES.has(name)) return true;
  const dot = name.lastIndexOf(".");
  return dot >= 0 && EXTENSION_SET.has(name.slice(dot));
}

export function splitSupportedFiles(files: readonly File[]): { supported: File[]; rejected: File[] } {
  const supported: File[] = [];
  const rejected: File[] = [];
  for (const file of files) (isSupportedAttachment(file) ? supported : rejected).push(file);
  return { supported, rejected };
}

/** A user-facing sentence for files that were skipped because of their type. */
export function describeUnsupportedFiles(rejected: readonly File[]): string {
  const hint = "Cortex attaches images and text, code, or configuration files.";
  if (rejected.length === 1) return `${rejected[0].name || "That file"} is not a supported attachment. ${hint}`;
  return `${rejected.length} files were skipped because they are not supported attachments. ${hint}`;
}

/**
 * Whether a drag carries files. Browsers hide the file list until the drop,
 * but the `types` list says "Files" for the whole drag, so this is the only
 * check available while the pointer is still moving. A dragged piece of text
 * is deliberately not a file drag: it must keep its normal behaviour.
 */
export function carriesFiles(transfer: Pick<DataTransfer, "types"> | null | undefined): boolean {
  return Array.from(transfer?.types ?? []).includes("Files");
}

/** The files in a paste or drop, or an empty list when it carried only text. */
export function filesFromTransfer(transfer: Pick<DataTransfer, "files" | "items"> | null | undefined): File[] {
  if (!transfer) return [];
  const listed = Array.from(transfer.files ?? []);
  if (listed.length) return listed;
  const files: File[] = [];
  for (const item of Array.from(transfer.items ?? [])) {
    const file = item.kind === "file" ? item.getAsFile() : null;
    if (file) files.push(file);
  }
  return files;
}

/**
 * Stop a file dropped outside an attachment target from navigating the window
 * to that file, which would replace Cortex with the file and leave no way
 * back. Only drags that carry files are touched; dragging selected text into
 * the composer keeps working. A target that already claimed the drag (it
 * cancelled the event) is left alone.
 *
 * Returns the function that removes the listeners.
 */
export function blockStrayFileDrops(target: Pick<Document, "addEventListener" | "removeEventListener"> = document): () => void {
  const onDragOver = (event: DragEvent) => {
    if (!carriesFiles(event.dataTransfer)) return;
    const claimed = event.defaultPrevented;
    event.preventDefault();
    if (!claimed && event.dataTransfer) event.dataTransfer.dropEffect = "none";
  };
  const onDrop = (event: DragEvent) => {
    if (carriesFiles(event.dataTransfer)) event.preventDefault();
  };
  target.addEventListener("dragover", onDragOver as EventListener);
  target.addEventListener("drop", onDrop as EventListener);
  return () => {
    target.removeEventListener("dragover", onDragOver as EventListener);
    target.removeEventListener("drop", onDrop as EventListener);
  };
}
