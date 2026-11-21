import { afterEach, describe, expect, it, vi } from "vitest";
import type { AgentSSEEvent, StartAgentRun } from "../../../../contracts/cortex-api";
import { CortexApi } from "../../api/client";
import { createAgentApi } from "./agentApi";

const payload: StartAgentRun = {
  project_root: "C:\\src\\sample",
  goal: "Explain the project",
  mode: "plan_only",
  model: "qwen3:8b",
  confirmed_project: true,
};

describe("Agent API transport", () => {
  afterEach(() => window.sessionStorage.clear());

  it("reuses the bearer session and sends Last-Event-ID on a reconnect", async () => {
    const event: AgentSSEEvent = {
      event_id: 7, event: "step", run_id: "run with space",
      data: { seq: 2, kind: "tool", summary: "Read 3 files" },
    };
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(
        JSON.stringify({ session_token: "local-session", expires_at: "2030-01-01T00:00:00Z" }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ))
      .mockResolvedValueOnce(new Response(
        "id: 7\r\nevent: step\r\ndata: " + JSON.stringify(event) + "\r\n\r\n",
        { status: 200, headers: { "Content-Type": "text/event-stream" } },
      ));
    const api = new CortexApi("/api/v1", fetcher);
    await api.exchangeBootstrapToken("bootstrap");
    const onEvent = vi.fn();
    await createAgentApi(api).events("run with space", onEvent, { afterEventId: 6 });
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(fetcher.mock.calls[1][0]).toBe("/api/v1/agent/runs/run%20with%20space/events");
    const headers = new Headers(fetcher.mock.calls[1][1]?.headers);
    expect(headers.get("Authorization")).toBe("Bearer local-session");
    expect(headers.get("Last-Event-ID")).toBe("6");
    expect(onEvent).toHaveBeenCalledWith(event);
  });

  it("refuses missing folder consent before issuing a start request", async () => {
    const startAgentRun = vi.fn().mockResolvedValue({ id: "run-1" });
    const agent = createAgentApi({ startAgentRun } as unknown as CortexApi);
    await expect(agent.start({ ...payload, confirmed_project: false })).rejects.toThrow("Confirm your project folder");
    expect(startAgentRun).not.toHaveBeenCalled();
  });

  it("refuses a cloud provider without explicit per-run confirmation", async () => {
    const startAgentRun = vi.fn().mockResolvedValue({ id: "run-1" });
    const agent = createAgentApi({ startAgentRun } as unknown as CortexApi);
    await expect(agent.start({ ...payload, model: "cloud:openai:gpt-4o", confirmed_cloud: false }))
      .rejects.toThrow("Confirm the cloud provider warning");
    expect(startAgentRun).not.toHaveBeenCalled();
  });
});
