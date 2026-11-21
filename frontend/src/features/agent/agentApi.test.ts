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
  it("requires consent and validates accepted, discarded, undone, and changeset responses", async () => {
    const client = {
      agentChangeset: vi.fn().mockResolvedValue({ run_id: "r 1", changes: [] }),
      acceptAgentChanges: vi.fn().mockResolvedValue({ paths: ["app.py"] }),
      discardAgentRun: vi.fn().mockResolvedValue({ discarded: true }),
      undoAgentChanges: vi.fn().mockResolvedValue({ paths: ["app.py"] }),
    };
    const agent = createAgentApi(client as unknown as CortexApi);
    await expect(agent.accept("r 1", ["app.py"], false)).rejects.toThrow(/confirm acceptance/i);
    await expect(agent.accept("r 1", [], true)).rejects.toThrow(/Select changed files/i);
    await expect(agent.discard("r 1", false)).rejects.toThrow(/Confirm discard/i);
    await expect(agent.undo("r 1", false)).rejects.toThrow(/Confirm undo/i);
    expect(client.acceptAgentChanges).not.toHaveBeenCalled();
    expect(client.discardAgentRun).not.toHaveBeenCalled();
    expect(client.undoAgentChanges).not.toHaveBeenCalled();

    await expect(agent.changeset("r 1")).resolves.toEqual({ run_id: "r 1", changes: [] });
    await expect(agent.accept("r 1", ["app.py"], true)).resolves.toEqual({ paths: ["app.py"] });
    await expect(agent.discard("r 1", true)).resolves.toEqual({ discarded: true });
    await expect(agent.undo("r 1", true)).resolves.toEqual({ paths: ["app.py"] });
    expect(client.acceptAgentChanges).toHaveBeenCalledWith("r 1", {
      paths: ["app.py"], confirmed: true,
    });
    expect(client.undoAgentChanges).toHaveBeenCalledWith("r 1", { confirmed: true });

    client.agentChangeset.mockResolvedValueOnce({ run_id: "other", changes: [] });
    client.acceptAgentChanges.mockResolvedValueOnce({ paths: [] });
    client.discardAgentRun.mockResolvedValueOnce({ discarded: false });
    client.undoAgentChanges.mockResolvedValueOnce({ paths: [] });
    await expect(agent.changeset("r 1")).rejects.toThrow(/changeset response is invalid/i);
    await expect(agent.accept("r 1", ["app.py"], true)).rejects.toThrow(/did not confirm the accepted paths/i);
    await expect(agent.discard("r 1", true)).rejects.toThrow(/did not confirm discard/i);
    await expect(agent.undo("r 1", true)).rejects.toThrow(/did not confirm restored paths/i);
  });

});
