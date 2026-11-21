import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AgentRunResponse } from "../../../../contracts/cortex-api";
import { RunHistory } from "./RunHistory";

type FixtureRun = AgentRunResponse & { goal?: string };

function run(id: string, overrides: Partial<FixtureRun> = {}): FixtureRun {
  return {
    id,
    goal: `Goal ${id}`,
    project_root: `C:\\projects\\${id}`,
    mode: "ask",
    model: "local-model",
    status: "completed",
    budgets: {},
    created: "2026-11-01T12:00:00Z",
    updated: "2026-11-01T12:00:00Z",
    stop_reason: null,
    ...overrides,
  };
}

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}

function mockFetch(runs: FixtureRun[]) {
  const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ runs }));
  vi.stubGlobal("fetch", fetcher);
  return fetcher;
}

function postCalls(fetcher: ReturnType<typeof mockFetch>) {
  return fetcher.mock.calls.filter(([, init]) => init?.method === "POST");
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.sessionStorage.removeItem("cortex.session.token");
});

describe("RunHistory", () => {
  it("shows loading while the list request is pending", async () => {
    let finish!: (response: Response) => void;
    const fetcher = vi.fn<typeof fetch>().mockImplementation(() => new Promise<Response>((resolve) => { finish = resolve; }));
    vi.stubGlobal("fetch", fetcher);
    render(<RunHistory onOpen={vi.fn()} />);

    expect(screen.getByRole("status")).toHaveTextContent("Loading agent runs");
    expect(screen.queryByText("No agent runs yet.")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh" })).toBeDisabled();

    finish(jsonResponse({ runs: [] }));
    expect(await screen.findByText("No agent runs yet.")).toBeInTheDocument();
  });

  it("shows an empty state for an empty run list", async () => {
    const fetcher = mockFetch([]);
    render(<RunHistory onOpen={vi.fn()} />);

    expect(await screen.findByText("No agent runs yet.")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Agent runs" })).not.toBeInTheDocument();
    expect(fetcher).toHaveBeenCalledWith("/api/v1/agent/runs", expect.objectContaining({ method: "GET" }));
  });

  it("sorts newest first and shows goals, plain statuses, folder names and dates without HTML or file contents", async () => {
    const maliciousGoal = '<img src=x onerror="alert(1)"> Review <script>bad()</script>';
    const newer = {
      ...run("newer", { goal: maliciousGoal, created: "2026-11-02T09:00:00Z", project_root: "/work/new-folder/" }),
      file_contents: "PRIVATE SOURCE CONTENT",
    };
    const fetcher = mockFetch([
      run("older", { goal: "Old task", created: "2026-10-01T09:00:00Z", project_root: "C:\\projects\\old-folder\\" }),
      newer,
      run("middle", { goal: "Middle task", created: "2026-11-01T09:00:00Z" }),
    ]);
    const onOpen = vi.fn();
    render(<RunHistory onOpen={onOpen} />);

    const openButtons = await screen.findAllByRole("button", { name: /^Open run:/ });
    expect(openButtons.map((button) => button.textContent)).toEqual([maliciousGoal, "Middle task", "Old task"]);
    const newest = openButtons[0].closest("li");
    expect(newest).not.toBeNull();
    expect(within(newest as HTMLElement).getByText("new-folder")).toBeInTheDocument();
    expect(within(newest as HTMLElement).getByText("Completed")).toBeInTheDocument();
    expect(newest?.querySelector("time")).toHaveAttribute("datetime", "2026-11-02T09:00:00Z");
    expect(screen.getByText("old-folder")).toBeInTheDocument();
    expect(screen.getByText(maliciousGoal)).toBeInTheDocument();
    expect(screen.queryByText("PRIVATE SOURCE CONTENT")).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Agent run history" }).querySelector("img, script, iframe, pre, code")).toBeNull();
    expect(onOpen).not.toHaveBeenCalled();
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("renders every persisted status as plain words, including an unknown status fallback", async () => {
    const statuses = [
      ["pending", "Pending"],
      ["running", "Running"],
      ["waiting_approval", "Waiting for approval"],
      ["completed", "Completed"],
      ["failed", "Failed"],
      ["cancelled", "Cancelled"],
      ["interrupted", "Interrupted"],
      ["<marquee>unexpected</marquee>", "Unknown status"],
    ] as const;
    mockFetch(statuses.map(([status], index) => run(`status-${index}`, {
      goal: status.startsWith("<") ? "Unknown entry" : status, status,
    })));
    render(<RunHistory onOpen={vi.fn()} />);
    await screen.findByRole("list", { name: "Agent runs" });

    for (const [value, label] of statuses) {
      const open = screen.getByRole("button", { name: `Open run: ${value.startsWith("<") ? "Unknown entry" : value}` });
      const item = open.closest("li") as HTMLElement;
      expect(within(item).getByText(label)).toBeInTheDocument();
    }
    expect(screen.queryByText("<marquee>unexpected</marquee>")).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Agent run history" }).querySelector("marquee")).toBeNull();
    expect(screen.getByRole("button", { name: "Delete run: running" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Delete run: waiting_approval" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Delete run: interrupted" })).toBeEnabled();
  });

  it("uses a clearly labeled fallback when the current API omits the goal", async () => {
    mockFetch([run("no-goal", { goal: undefined, project_root: "C:\\projects\\public-name" })]);
    const onOpen = vi.fn();
    const user = userEvent.setup();
    render(<RunHistory onOpen={onOpen} />);
    const open = await screen.findByRole("button", { name: "Open run: Goal unavailable" });

    expect(screen.getByText("public-name")).toBeInTheDocument();
    await user.click(open);
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(onOpen).toHaveBeenCalledWith("no-goal");
  });

  it("opens a selected run through its keyboard-accessible button", async () => {
    mockFetch([run("selected")]);
    const onOpen = vi.fn();
    const user = userEvent.setup();
    render(<RunHistory onOpen={onOpen} />);
    const open = await screen.findByRole("button", { name: "Open run: Goal selected" });

    open.focus();
    await user.keyboard("{Enter}");
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(onOpen).toHaveBeenCalledWith("selected");
    expect(open).toHaveFocus();
  });

  it("never resumes without confirmation, then posts and updates the status after consent", async () => {
    const interrupted = run("r/1", { goal: "Continue work", status: "interrupted" });
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async (_url, init) => init?.method === "POST"
      ? jsonResponse({ ...interrupted, status: "running" })
      : jsonResponse({ runs: [interrupted] }));
    vi.stubGlobal("fetch", fetcher);
    window.sessionStorage.setItem("cortex.session.token", "session-test-token");
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const onOpen = vi.fn();
    const user = userEvent.setup();
    render(<RunHistory onOpen={onOpen} />);
    const resume = await screen.findByRole("button", { name: "Resume run: Continue work" });

    await user.click(resume);
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("Resume this interrupted agent run?"));
    expect(postCalls(fetcher)).toHaveLength(0);
    expect(screen.getByText("Interrupted")).toBeInTheDocument();

    confirm.mockReturnValue(true);
    resume.focus();
    await user.keyboard(" ");
    await waitFor(() => expect(postCalls(fetcher)).toHaveLength(1));
    expect(postCalls(fetcher)[0][0]).toBe("/api/v1/agent/runs/r%2F1/resume");
    expect(postCalls(fetcher)[0][1]).toEqual(expect.objectContaining({ method: "POST" }));
    expect(new Headers(postCalls(fetcher)[0][1]?.headers).get("Authorization")).toBe("Bearer session-test-token");
    expect(await screen.findByText("Run resumed.")).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Resume run: Continue work" })).not.toBeInTheDocument();
    expect(onOpen).not.toHaveBeenCalled();
  });

  it("never discards without a warning that the work copy will be removed", async () => {
    const entry = run("delete-me", { goal: "Disposable task", status: "failed" });
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async (_url, init) => init?.method === "POST"
      ? jsonResponse({ discarded: true })
      : jsonResponse({ runs: [entry] }));
    vi.stubGlobal("fetch", fetcher);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const onOpen = vi.fn();
    const user = userEvent.setup();
    render(<RunHistory onOpen={onOpen} />);
    const remove = await screen.findByRole("button", { name: "Delete run: Disposable task" });

    await user.click(remove);
    expect(confirm).toHaveBeenCalledWith(expect.stringMatching(/work copy will be removed/i));
    expect(postCalls(fetcher)).toHaveLength(0);
    expect(remove).toBeEnabled();

    confirm.mockReturnValue(true);
    remove.focus();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(postCalls(fetcher)).toHaveLength(1));
    expect(postCalls(fetcher)[0][0]).toBe("/api/v1/agent/runs/delete-me/discard");
    expect(await screen.findByText("No agent runs yet.")).toBeInTheDocument();
    expect(screen.getByText("The run's work copy was removed.")).toBeInTheDocument();
    expect(onOpen).not.toHaveBeenCalled();
  });

  it("keeps a run visible and reports failure when resume or discard is rejected", async () => {
    const entry = run("interrupted", { status: "interrupted" });
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async (_url, init) => init?.method === "POST"
      ? jsonResponse({ detail: "No private stack trace or file contents" }, 409)
      : jsonResponse({ runs: [entry] }));
    vi.stubGlobal("fetch", fetcher);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<RunHistory onOpen={vi.fn()} />);

    await user.click(await screen.findByRole("button", { name: "Resume run: Goal interrupted" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not resume the run. Try again.");
    expect(screen.getByText("Interrupted")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Delete run: Goal interrupted" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not delete the run. Try again.");
    expect(screen.getByRole("button", { name: "Open run: Goal interrupted" })).toBeInTheDocument();
    expect(screen.queryByText(/private stack trace/i)).not.toBeInTheDocument();
    expect(postCalls(fetcher)).toHaveLength(2);
  });

  it("does not treat an unsuccessful or malformed discard response as deletion", async () => {
    const entry = run("keep");
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async (_url, init) => init?.method === "POST"
      ? jsonResponse({ discarded: false })
      : jsonResponse({ runs: [entry] }));
    vi.stubGlobal("fetch", fetcher);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<RunHistory onOpen={vi.fn()} />);

    await user.click(await screen.findByRole("button", { name: "Delete run: Goal keep" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not delete the run.");
    expect(screen.getByRole("button", { name: "Open run: Goal keep" })).toBeInTheDocument();
  });

  it("shows a plain error state and can retry a failed list request", async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({ detail: "sensitive server text" }, 503))
      .mockResolvedValueOnce(jsonResponse({ runs: [run("retried")] }));
    vi.stubGlobal("fetch", fetcher);
    const user = userEvent.setup();
    render(<RunHistory onOpen={vi.fn()} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load agent runs.");
    expect(screen.queryByText("sensitive server text")).not.toBeInTheDocument();
    expect(screen.queryByText("No agent runs yet.")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("button", { name: "Open run: Goal retried" })).toBeInTheDocument();
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("fails closed on malformed list data and network errors", async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({ runs: [{ id: "invalid" }] }))
      .mockRejectedValueOnce(new TypeError("network down"));
    vi.stubGlobal("fetch", fetcher);
    const user = userEvent.setup();
    render(<RunHistory onOpen={vi.fn()} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load agent runs.");
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load agent runs.");
    expect(screen.queryByRole("list", { name: "Agent runs" })).not.toBeInTheDocument();
  });
});
