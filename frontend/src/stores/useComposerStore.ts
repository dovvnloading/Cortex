import { create } from "zustand";
import type { ChatAttachment } from "../../../contracts/cortex-api";

/**
 * Unsent composer text and staged attachments, by draft scope
 * (`composerDraftKey` / `composerAttachmentKey`).
 *
 * The chat page unmounts for Settings and other routes, but a send or an
 * upload it started can finish after that. Each mount used to keep its own
 * copy of the drafts, so the old mount settled into a stale copy and wrote it
 * to storage over what the remounted page held: a newer draft was erased
 * (#697), and a removed attachment came back while a new one vanished (#687).
 * Every mount now reads and writes this one store. A scope the store has not
 * seen yet is read from storage (lib/composerDraft.ts), which still persists
 * every change.
 */
export type ComposerState = {
  drafts: Record<string, string>;
  attachments: Record<string, ChatAttachment[]>;
};

export const useComposerStore = create<ComposerState>(() => ({ drafts: {}, attachments: {} }));
