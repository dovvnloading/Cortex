import type {
  AgentAcceptedResponse,
  AgentChangesetResponse,
  AgentDiscardedResponse,
  AgentRunListResponse,
  AgentRunResponse,
  AgentSSEEvent,
  AgentUndoneResponse,
  DecideAgentApproval,
  StartAgentRun,
} from "../../../../contracts/cortex-api";
import type { CortexApi } from "../../api/client";
import { cloudModelProvider } from "../../lib/localModels";

export type AgentApi = {
  start: (payload: StartAgentRun) => Promise<AgentRunResponse>;
  list: () => Promise<AgentRunListResponse>;
  get: (runId: string) => Promise<AgentRunResponse>;
  events: (
    runId: string,
    onEvent: (event: AgentSSEEvent) => void,
    options?: { signal?: AbortSignal; afterEventId?: number },
  ) => Promise<void>;
  decide: (runId: string, seq: number, decision: DecideAgentApproval["decision"]) => Promise<AgentRunResponse>;
  resume: (runId: string) => Promise<AgentRunResponse>;
  stop: (runId: string) => Promise<AgentRunResponse>;
  changeset: (runId: string) => Promise<AgentChangesetResponse>;
  accept: (runId: string, paths: string[], confirmed: boolean) => Promise<AgentAcceptedResponse>;
  discard: (runId: string, confirmed: boolean) => Promise<AgentDiscardedResponse>;
  undo: (runId: string, confirmed: boolean) => Promise<AgentUndoneResponse>;
};

/**
 * Agent requests use the same authenticated, loopback-only CortexApi transport
 * as the rest of the desktop UI. Consent is checked at this boundary as well
 * as in the page and the backend; no implicit affirmative defaults.
 */
export function createAgentApi(client: CortexApi): AgentApi {
  return {
    start(payload) {
      if (!payload.project_root.trim() || !payload.goal.trim() || !payload.model.trim()) {
        return Promise.reject(new Error("A goal, project folder, and model are required."));
      }
      if (payload.confirmed_project !== true) {
        return Promise.reject(new Error("Confirm your project folder before starting."));
      }
      if (cloudModelProvider(payload.model) && payload.confirmed_cloud !== true) {
        return Promise.reject(new Error("Confirm the cloud provider warning before starting."));
      }
      return client.startAgentRun(payload);
    },
    list: () => client.agentRuns(),
    get: (runId) => client.agentRun(runId),
    events: (runId, onEvent, options) => client.streamAgentRun(runId, onEvent, options),
    decide: (runId, seq, decision) => client.decideAgentApproval(runId, seq, decision),
    resume: (runId) => client.resumeAgentRun(runId),
    stop: (runId) => client.cancelAgentRun(runId),
    changeset: (runId) => client.agentChangeset(runId).then((result) => {
      if (result?.run_id !== runId || !Array.isArray(result.changes)
        || !result.changes.every((change) => typeof change.path === "string"
          && ["add", "modify", "delete"].includes(change.kind)
          && typeof change.diff === "string")) {
        throw new Error("The changeset response is invalid.");
      }
      return result;
    }),
    accept(runId, paths, confirmed) {
      if (confirmed !== true || paths.length < 1 || paths.length > 32
        || paths.some((path) => typeof path !== "string" || !path.trim())
        || new Set(paths).size !== paths.length) {
        return Promise.reject(new Error("Select changed files and explicitly confirm acceptance."));
      }
      return client.acceptAgentChanges(runId, { paths, confirmed: true }).then((result) => {
        if (!Array.isArray(result?.paths) || result.paths.length !== paths.length
          || !paths.every((path) => result.paths.includes(path))) {
          throw new Error("The server did not confirm the accepted paths.");
        }
        return result;
      });
    },
    discard(runId, confirmed) {
      if (confirmed !== true) return Promise.reject(new Error("Confirm discard before deleting a work copy."));
      return client.discardAgentRun(runId).then((result) => {
        if (result?.discarded !== true) throw new Error("The server did not confirm discard.");
        return result;
      });
    },
    undo(runId, confirmed) {
      if (confirmed !== true) return Promise.reject(new Error("Confirm undo before restoring project files."));
      return client.undoAgentChanges(runId, { confirmed: true }).then((result) => {
        if (!Array.isArray(result?.paths) || result.paths.length === 0
          || !result.paths.every((path) => typeof path === "string")) {
          throw new Error("The server did not confirm restored paths.");
        }
        return result;
      });
    },
  };
}
