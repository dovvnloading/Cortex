import type { HuggingFaceFileEntry } from "../../../contracts/cortex-api";

/**
 * One thing a person can pick from a repository: a single file, or a split
 * model (`name-00001-of-00003.gguf` ...) that only works with every part.
 */
export type GGUFFileChoice = {
  /** What to send as the file name: the file's path in the repository, or the first part's for a split model. */
  path: string;
  /** The folder it sits in inside the repository; empty at the top. */
  directory: string;
  /** What to call it: the file name, or the split model's name without the part numbers. */
  label: string;
  /** Bytes for the whole choice; `null` when any part's size is unknown. */
  size: number | null;
  /** How many files make it up: 1, or the part count of a split model. */
  parts: number;
  /** False for a split model whose listing lacks a part, which would download to a broken model. */
  complete: boolean;
};

/** Mirrors the backend's `_SPLIT_NAME_PATTERN`, so both sides agree on what a split model is. */
const SPLIT_NAME = /^(?<stem>.+)-(?<index>\d{5})-of-(?<total>\d{5})(?<suffix>\.gguf)$/i;
const MAX_SPLIT_PARTS = 256;

type Part = { entry: HuggingFaceFileEntry; index: number };

function splitOf(entry: HuggingFaceFileEntry): { key: string; stem: string; suffix: string; total: number; index: number; directory: string } | null {
  const slash = entry.path.lastIndexOf("/");
  const directory = slash < 0 ? "" : entry.path.slice(0, slash);
  const match = SPLIT_NAME.exec(entry.path.slice(slash + 1));
  const groups = match?.groups;
  if (!groups) return null;
  const total = Number(groups.total);
  const index = Number(groups.index);
  if (total < 2 || total > MAX_SPLIT_PARTS || index < 1 || index > total) return null;
  return {
    key: `${directory}\u0000${groups.stem}\u0000${total}\u0000${groups.suffix.toLowerCase()}`,
    stem: groups.stem,
    suffix: groups.suffix,
    total,
    index,
    directory,
  };
}

function sizeOf(entries: readonly HuggingFaceFileEntry[]): number | null {
  let total = 0;
  for (const entry of entries) {
    if (typeof entry.size !== "number") return null;
    total += entry.size;
  }
  return total;
}

/**
 * Turn the backend's flat listing into what to pick from: split models
 * collapsed into one choice, everything ordered folder by folder. A name that
 * only looks like a part (part 5 of 3) stays an ordinary file, as it does on
 * the backend.
 */
export function groupGGUFFiles(entries: readonly HuggingFaceFileEntry[]): GGUFFileChoice[] {
  const choices: GGUFFileChoice[] = [];
  const sets = new Map<string, { stem: string; suffix: string; total: number; directory: string; parts: Part[] }>();
  for (const entry of entries) {
    const split = splitOf(entry);
    if (!split) {
      const slash = entry.path.lastIndexOf("/");
      choices.push({
        path: entry.path,
        directory: slash < 0 ? "" : entry.path.slice(0, slash),
        label: entry.path.slice(slash + 1),
        size: typeof entry.size === "number" ? entry.size : null,
        parts: 1,
        complete: true,
      });
      continue;
    }
    const set = sets.get(split.key) ?? { stem: split.stem, suffix: split.suffix, total: split.total, directory: split.directory, parts: [] };
    set.parts.push({ entry, index: split.index });
    sets.set(split.key, set);
  }
  for (const set of sets.values()) {
    const present = new Set(set.parts.map((part) => part.index));
    const complete = present.size === set.total;
    const first = set.parts.find((part) => part.index === 1) ?? set.parts[0];
    choices.push({
      path: first.entry.path,
      directory: set.directory,
      label: `${set.stem}${set.suffix}`,
      size: complete ? sizeOf(set.parts.map((part) => part.entry)) : null,
      parts: set.total,
      complete,
    });
  }
  return choices.sort((left, right) => (
    left.directory.localeCompare(right.directory)
    || left.label.localeCompare(right.label, undefined, { numeric: true })
  ));
}

/** The `owner/name` a person can type or paste, in the form the backend accepts. */
export const HUGGING_FACE_REPO_ID = /^[\w.-]+\/[\w.-]+$/;
