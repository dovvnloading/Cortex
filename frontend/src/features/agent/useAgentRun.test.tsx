import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { AgentRunResponse, AgentSSEEvent, StartAgentRun } from "../../../../contracts/cortex-api";
import type { AgentApi } from "./agentApi";
import { PARKED_POLL_MS, useAgentRun } from "./useAgentRun";

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

  it.each([
    [
      { tool: "edit_file", path: "calc.py", scope: "workcopy",
        preview: { old: "return left - right", new: "return left + right", replace_all: false, truncated: false } },
      ["Requested tool: edit_file", "Project file: calc.py", "", "Replace this text:", "return left - right", "", "With:", "return left + right"].join("\n"),
    ],
    [
      { tool: "edit_file", path: "calc.py", scope: "workcopy",
        preview: { old: "a", new: "b", replace_all: true, truncated: true } },
      ["Requested tool: edit_file", "Project file: calc.py", "", "Replace every occurrence of:", "a", "", "With:", "b", "", "(Shortened here. The review shows the full change after it is staged.)"].join("\n"),
    ],
    [
      { tool: "write_file", path: "new.py", scope: "workcopy", preview: { content: "print(1)", truncated: false } },
      ["Requested tool: write_file", "Project file: new.py", "", "File content:", "print(1)"].join("\n"),
    ],
    [
      { tool: "move_file", source: "old.py", destination: "new.py", scope: "workcopy" },
      ["Requested tool: move_file", "Move from: old.py", "Move to: new.py"].join("\n"),
    ],
    [
      { tool: "edit_file", path: "calc.py", scope: "workcopy", preview: { old: 1, new: ["x"] } },
      ["Requested tool: edit_file", "Project file: calc.py"].join("\n"),
    ],
  ])("shows what a file change will do: %j", async (request, details) => {
    const api = fakeApi();
    vi.mocked(api.events).mockImplementation(async (_id, onEvent) => {
      onEvent(sse(1, "approval", { seq: 5, request }));
      onEvent(sse(2, "status", { status: "waiting_approval" }));
    });
    const { result } = renderHook(() => useAgentRun(api, true));
    await act(async () => { await result.current.start(started); });
    await waitFor(() => expect(result.current.pendingApproval?.seq).toBe(5));
    expect(result.current.pendingApproval?.request.command).toBe(details);
  });

  it("shows a web fetch's exact URL and that it uses the internet before consent (#675)", async () => {
    const api = fakeApi();
    const url = "https://example.com/reference?topic=test";
    vi.mocked(api.events).mockImplementation(async (_id, onEvent) => {
      onEvent(sse(1, "approval", {
        seq: 2, request: { tool: "web_fetch", url, summary: "Fetch this exact public URL once." },
      }));
      onEvent(sse(2, "status", { status: "waiting_approval" }));
    });
    const { result } = renderHook(() => useAgentRun(api, true));
    await act(async () => { await result.current.start(started); });
    await waitFor(() => expect(result.current.pendingApproval).not.toBeNull());
    const request = result.current.pendingApproval!.request;
    expect(request.network).toBe(true);
    expect(request.plain_text).toContain(url);
    expect(request.command).toContain("Destination URL: " + url);
  });

  it("does not offer a web fetch for approval without its URL (#675)", async () => {
    const api = fakeApi();
    vi.mocked(api.events).mockImplementation(async (_id, onEvent) => {
      onEvent(sse(1, "approval", { seq: 2, request: { tool: "web_fetch", summary: "Fetch this exact public URL once." } }));
      onEvent(sse(2, "status", { status: "waiting_approval" }));
    });
    const { result } = renderHook(() => useAgentRun(api, true));
    await act(async () => { await result.current.start(started); });
    await waitFor(() => expect(result.current.run?.status).toBe("waiting_approval"));
    expect(result.current.pendingApproval).toBeNull();
  });

  it("a late discard of one run never clears another run opened since (#683)", async () => {
    const api = fakeApi();
    vi.mocked(api.get).mockImplementation(async (id) => ({ ...run, id, status: "completed" }));
    const { result } = renderHook(() => useAgentRun(api, true));
    await act(async () => { await result.current.open("run-a"); });
    // The history row captured this callback while run A was shown.
    const forgetFromRunA = result.current.forget;
    await act(async () => { await result.current.open("run-b"); });

    act(() => forgetFromRunA("run-a"));

    expect(result.current.run?.id).toBe("run-b");
  });

  it("offers Resume for a reopened run that the server says is waiting for it (#682)", async () => {
    const api = fakeApi();
    vi.mocked(api.get).mockResolvedValue({ ...run, status: "running", resume_required: true });
    const { result } = renderHook(() => useAgentRun(api, true));
    await act(async () => { await result.current.open(run.id); });
    expect(result.current.resumeRequired).toBe(true);

    await act(async () => { await result.current.resume(); });

    expect(api.resume).toHaveBeenCalledWith(run.id);
    expect(api.decide).not.toHaveBeenCalled();
    expect(result.current.resumeRequired).toBe(false);
  });

  it("re-reads the run when a decision's response is lost, without approving again (#682)", async () => {
    const api = fakeApi();
    vi.mocked(api.events).mockImplementation(async (_id, onEvent) => {
      onEvent(sse(1, "approval", { seq: 4, request: { tool: "run_command", command: "pytest", plain_text: "Run the tests." } }));
      onEvent(sse(2, "status", { status: "waiting_approval" }));
    });
    vi.mocked(api.decide).mockRejectedValue(new Error("network down"));
    const { result } = renderHook(() => useAgentRun(api, true));
    await act(async () => { await result.current.start(started); });
    await waitFor(() => expect(result.current.pendingApproval?.seq).toBe(4));
    // The decision was saved before its response was lost.
    vi.mocked(api.get).mockResolvedValue({ ...run, status: "running", resume_required: true });

    await act(async () => { await result.current.decide(4, "allow_once"); });

    expect(api.decide).toHaveBeenCalledTimes(1);
    expect(api.resume).not.toHaveBeenCalled();
    expect(result.current.run?.status).toBe("running");
    expect(result.current.resumeRequired).toBe(true);
  });

  it("replays a missed approval when the stream dropped before it arrived (#676)", async () => {
    const api = fakeApi();
    let connection = 0;
    vi.mocked(api.events).mockImplementation(async (_id, onEvent, options) => {
      connection += 1;
      if (connection === 1) {
        onEvent(sse(1, "step", { seq: 1, kind: "tool", tool: "read_file", summary: "Read a file" }));
        throw new Error("connection reset");
      }
      expect(options?.afterEventId).toBe(1);
      onEvent(sse(2, "approval", { seq: 3, request: { tool: "run_command", command: "pytest", plain_text: "Run the tests." } }));
      onEvent(sse(3, "status", { status: "waiting_approval" }));
    });
    vi.mocked(api.get).mockResolvedValue({ ...run, status: "waiting_approval" });
    const { result } = renderHook(() => useAgentRun(api, true));
    await act(async () => { await result.current.start(started); });

    await waitFor(() => expect(result.current.pendingApproval?.seq).toBe(3));
    expect(connection).toBe(2);
  });

  it("replays the final answer of a run that finished while the stream was down (#676)", async () => {
    const api = fakeApi();
    let connection = 0;
    vi.mocked(api.events).mockImplementation(async (_id, onEvent) => {
      connection += 1;
      if (connection === 1) throw new Error("connection reset");
      onEvent(sse(1, "output", { source: "agent", text: "All tests pass." }));
      onEvent(sse(2, "status", { status: "completed" }));
    });
    vi.mocked(api.get).mockResolvedValue({ ...run, status: "completed" });
    const { result } = renderHook(() => useAgentRun(api, true));
    await act(async () => { await result.current.start(started); });

    await waitFor(() => expect(result.current.events.map((event) => event.event)).toEqual(["output", "status"]));
    expect(result.current.run?.status).toBe("completed");
    expect(result.current.connection).toBe("idle");
  });

  it("says so when the missed updates of a finished run cannot be replayed (#676)", async () => {
    const api = fakeApi();
    vi.mocked(api.events).mockRejectedValue(new Error("connection reset"));
    vi.mocked(api.get).mockResolvedValue({ ...run, status: "completed" });
    const { result } = renderHook(() => useAgentRun(api, true));
    await act(async () => { await result.current.start(started); });

    await waitFor(() => expect(result.current.error).toMatch(/could not be loaded/), { timeout: 8000 });
    // One first attempt plus a bounded number of retries, then it stops.
    const attempts = vi.mocked(api.events).mock.calls.length;
    expect(attempts).toBeGreaterThan(1);
    expect(attempts).toBeLessThanOrEqual(5);
    expect(result.current.connection).toBe("idle");
  }, 10000);

  it("notices an approval that expired while parked, and clears it (#677)", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      const api = fakeApi();
      let expired = false;
      vi.mocked(api.events).mockImplementation(async (_id, onEvent, options) => {
        if (!expired) {
          onEvent(sse(1, "approval", { seq: 2, request: { tool: "run_command", command: "pytest", plain_text: "Run the tests." } }));
          onEvent(sse(2, "status", { status: "waiting_approval" }));
          return;
        }
        expect(options?.afterEventId).toBe(2);
        onEvent(sse(3, "approval", { seq: 2, decision: "expired" }));
        onEvent(sse(4, "status", { status: "failed", stop_reason: "approval_expired" }));
      });
      vi.mocked(api.get).mockImplementation(async () => (expired
        ? { ...run, status: "failed", stop_reason: "approval_expired" }
        : { ...run, status: "waiting_approval" }));
      const { result } = renderHook(() => useAgentRun(api, true));
      await act(async () => { await result.current.start(started); });
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      expect(result.current.pendingApproval?.seq).toBe(2);

      // Still parked: the page keeps checking, and keeps the approval.
      await act(async () => { await vi.advanceTimersByTimeAsync(PARKED_POLL_MS); });
      expect(api.get).toHaveBeenCalledTimes(1);
      expect(result.current.pendingApproval?.seq).toBe(2);

      expired = true;
      await act(async () => { await vi.advanceTimersByTimeAsync(PARKED_POLL_MS); });

      expect(result.current.run?.status).toBe("failed");
      expect(result.current.run?.stop_reason).toBe("approval_expired");
      expect(result.current.pendingApproval).toBeNull();
      expect(api.decide).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("reports the one-time model check while it runs and the result it ends with", async () => {
    const api = fakeApi();
    let emit: ((event: AgentSSEEvent) => void) | null = null;
    vi.mocked(api.events).mockImplementation(async (_id, onEvent) => {
      emit = onEvent;
      onEvent(sse(1, "status", { status: "pending", phase: "model_check" }));
      return new Promise<void>(() => undefined);
    });
    const { result } = renderHook(() => useAgentRun(api, true));
    await act(async () => { await result.current.start(started); });
    await waitFor(() => expect(result.current.checkingModel).toBe(true));
    expect(result.current.modelCheck).toBeNull();

    act(() => emit?.(sse(2, "status", {
      status: "running", model_check: { recommended: false, failed_checks: ["injection_resistance", 7] },
    })));
    await waitFor(() => expect(result.current.checkingModel).toBe(false));
    expect(result.current.modelCheck).toEqual({ recommended: false, failedChecks: ["injection_resistance"] });

    // A later status without a result keeps it; a malformed one is ignored.
    act(() => emit?.(sse(3, "status", { status: "running", model_check: { recommended: "yes" } })));
    expect(result.current.modelCheck).toEqual({ recommended: false, failedChecks: ["injection_resistance"] });
  });

  it("never reports a model as recommended when a failed check is listed", async () => {
    const api = fakeApi();
    vi.mocked(api.events).mockImplementation(async (_id, onEvent) => {
      onEvent(sse(1, "status", { status: "running", model_check: { recommended: true, failed_checks: ["ordered_calls"] } }));
      onEvent(sse(2, "status", { status: "completed" }));
    });
    const { result } = renderHook(() => useAgentRun(api, true));
    await act(async () => { await result.current.start(started); });
    await waitFor(() => expect(result.current.modelCheck).not.toBeNull());
    expect(result.current.modelCheck?.recommended).toBe(false);
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
