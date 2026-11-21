/** Metadata only; saved source bytes remain in the private local recovery journal. */
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
