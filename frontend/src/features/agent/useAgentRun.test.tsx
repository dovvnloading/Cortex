import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { AgentRunResponse, AgentSSEEvent, StartAgentRun } from "../../../../contracts/cortex-api";
import type { AgentApi } from "./agentApi";
import { useAgentRun } from "./useAgentRun";

const started: StartAgentRun = {
  project_root: "C:\\dev\\project",
  goal: "Explain this project",
  mode: "plan_only",
  model: "qwen3:8b",
  confirmed_project: true,
  confirmed_cloud: false,
};
const run: AgentRunResponse = {
  id: "run-1", project_root: started.project_root, mode: "plan_only", model: started.model,
  status: "running", budgets: {}, created: "2026-01-01", updated: "2026-01-01",
};
const sse = (event_id: number, event: AgentSSEEvent["event"], data: AgentSSEEvent["data"]): AgentSSEEvent => ({
  event_id, event, data, run_id: run.id,
});

function fakeApi(): AgentApi {
  return {
    start: vi.fn().mockResolvedValue(run),
    list: vi.fn().mockResolvedValue({ runs: [] }),
    get: vi.fn().mockResolvedValue(run),
    events: vi.fn().mockResolvedValue(undefined),
    decide: vi.fn().mockResolvedValue({ ...run, status: "running" }),
    resume: vi.fn().mockResolvedValue({ ...run, status: "completed" }),
    stop: vi.fn().mockResolvedValue({ ...run, status: "cancelled" }),
    changeset: vi.fn(),
    accept: vi.fn(),
    discard: vi.fn(),
    undo: vi.fn(),
  };
}

describe("useAgentRun", () => {
  it("reconnects to SSE with the Last-Event-ID cursor and ignores replayed steps", async () => {
    const api = fakeApi();
    let connection = 0;
    vi.mocked(api.events).mockImplementation(async (_id, onEvent, options) => {
      connection += 1;
      if (connection === 1) {
        expect(options?.afterEventId).toBeUndefined();
        onEvent(sse(1, "step", { seq: 1, kind: "tool", tool: "read_file", summary: "Read 3 files" }));
      } else {
        onEvent(sse(1, "step", { seq: 1, kind: "tool" })); // replay is ignored
        onEvent(sse(2, "status", { status: "completed" }));
      }
    });
    const { result } = renderHook(() => useAgentRun(api, true));
    await act(async () => { expect(await result.current.start(started)).toBe(true); });
    await waitFor(() => expect(api.events).toHaveBeenCalledTimes(2), { timeout: 2500 });
    expect(vi.mocked(api.events).mock.calls[1][2]?.afterEventId).toBe(1);
    await waitFor(() => expect(result.current.run?.status).toBe("completed"));
    expect(result.current.events.map((item) => item.event_id)).toEqual([1, 2]);
  });

  it("does not decide an approval until a person chooses Allow once and then resumes", async () => {
    const api = fakeApi();
    vi.mocked(api.events).mockImplementation(async (_id, onEvent) => {
      onEvent(sse(1, "approval", { seq: 3, request: { tool: "replace_file", path: "app.py", summary: "Stage a replacement" } }));
      onEvent(sse(2, "status", { status: "waiting_approval" }));
    });
    const { result } = renderHook(() => useAgentRun(api, true));
    await act(async () => { await result.current.start(started); });
    await waitFor(() => expect(result.current.pendingApproval?.seq).toBe(3));
    expect(api.decide).not.toHaveBeenCalled();
    expect(api.resume).not.toHaveBeenCalled();
    expect(result.current.pendingApproval?.request.sandbox_level).toBe("unsandboxed");

    await act(async () => { await result.current.decide(3, "allow_once"); });
    expect(api.decide).toHaveBeenCalledWith("run-1", 3, "allow_once");
    expect(api.resume).toHaveBeenCalledWith("run-1");
    expect(result.current.run?.status).toBe("completed");
    expect(result.current.pendingApproval).toBeNull();
  });

  it.each([
    [{ tool: "run_command", command: "git status", level: "appcontainer" }, "appcontainer"],
    [{ tool: "run_command", command: "git status", level: "unsandboxed" }, "unsandboxed"],
    [{ tool: "run_command", command: "git status" }, "unsandboxed"],
    [{ tool: "run_command", command: "git status", level: "AppContainer" }, "unsandboxed"],
    [{ tool: "run_command", command: "git status", sandbox_level: "appcontainer" }, "unsandboxed"],
    [{ tool: "run_command", command: "git status", scope: "workcopy" }, "unsandboxed"],
    [{ tool: "edit_file", path: "app.py", scope: "workcopy" }, "workcopy"],
    [{ tool: "edit_file", path: "app.py", level: "appcontainer" }, "unsandboxed"],
    [{ tool: "git_commit" }, "unsandboxed"],
  ])("reads the execution context the backend sends: %j", async (request, expected) => {
    const api = fakeApi();
    vi.mocked(api.events).mockImplementation(async (_id, onEvent) => {
      onEvent(sse(1, "approval", { seq: 4, request }));
      onEvent(sse(2, "status", { status: "waiting_approval" }));
    });
    const { result } = renderHook(() => useAgentRun(api, true));
    await act(async () => { await result.current.start(started); });
    await waitFor(() => expect(result.current.pendingApproval?.seq).toBe(4));
    expect(result.current.pendingApproval?.request.sandbox_level).toBe(expected);
  });

  it("sends Stop to the backend rather than treating an abort as server cancellation", async () => {
    const api = fakeApi();
    vi.mocked(api.events).mockImplementation(async () => new Promise<void>(() => undefined));
    const { result, unmount } = renderHook(() => useAgentRun(api, true));
    await act(async () => { await result.current.start(started); });
    await act(async () => { await result.current.stop(); });
    expect(api.stop).toHaveBeenCalledWith("run-1");
    expect(result.current.run?.status).toBe("cancelled");
    unmount();
  });

  it("replays history when the start response is already completed", async () => {
    const api = fakeApi();
    vi.mocked(api.start).mockResolvedValue({ ...run, status: "completed" });
    vi.mocked(api.events).mockImplementation(async (_id, onEvent) => {
      onEvent(sse(1, "step", { seq: 1, kind: "tool", tool: "read_file", summary: "Read 3 files" }));
      onEvent(sse(2, "status", { status: "completed" }));
    });
    const { result } = renderHook(() => useAgentRun(api, true));
    await act(async () => { await result.current.start(started); });
    await waitFor(() => expect(result.current.events.map((item) => item.event_id)).toEqual([1, 2]));
    expect(api.events).toHaveBeenCalledTimes(1);
  });

  it("does not start or stream when the feature is disabled", async () => {
    const api = fakeApi();
    const { result } = renderHook(() => useAgentRun(api, false));
    await act(async () => { expect(await result.current.start(started)).toBe(false); });
    expect(api.start).not.toHaveBeenCalled();
    expect(api.events).not.toHaveBeenCalled();
  });
});
