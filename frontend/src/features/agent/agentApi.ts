import type {
  AgentRunListResponse,
  AgentRunResponse,
  AgentSSEEvent,
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
  };
}
