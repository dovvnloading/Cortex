/** Private, temporary coding-workspace contract. */
export type WorkspaceOpened = {
  session_id: string; root: string; file_count: number; total_bytes: number;
  skipped_entries: number; truncated: boolean; expires_at: string; state: string;
};
export type WorkspaceHit = { path: string; line: number; text: string };
export type WorkspaceExcerpt = {
  path: string; start_line: number; end_line: number; total_lines: number;
  sha256: string; text: string; truncated: boolean;
};
export type WorkspaceAgentAnswer = {
  status: string; answer: string | null; model_calls: number; tool_calls: number;
  preview_diff: string | null; preview_paths: string[]; preview_replacements: number;
  applied_to_checkout: boolean;
};
export type WorkspacePreview = {
  diff: string; paths: string[]; replacements: number; applied_to_checkout: boolean;
};
export type WorkspaceActionResult = { status: string; paths: string[]; undo_available: boolean };
export type WorkspaceCheckResult = {
  status: "passed" | "failed" | "timed_out" | "cancelled";
  command: string; exit_code: number; output: string; output_truncated: boolean;
  duration_seconds: number;
};

/** Metadata only; saved source bytes remain in the private local journal. */
export type WorkspaceCheckpointSummary = {
  id: string;
  root: string;
  state: "active" | "pending";
  created_at: string;
  updated_at: string;
  paths: string[];
  file_count: number;
};

export type WorkspaceRestoreResult = {
  status: "restored";
  checkpoint_id: string;
  paths: string[];
};
