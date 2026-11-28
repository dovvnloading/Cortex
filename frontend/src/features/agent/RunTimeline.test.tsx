import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { AgentSSEEvent } from "../../../../contracts/cortex-api";
import { RunTimeline } from "./RunTimeline";

const event = (event_id: number, kind: AgentSSEEvent["event"], data: AgentSSEEvent["data"]): AgentSSEEvent => ({
  event_id, event: kind, run_id: "run-1", data,
});

describe("RunTimeline", () => {
  it("describes steps in words and expands the details on request", async () => {
    const user = userEvent.setup();
    render(<RunTimeline events={[
      event(1, "step", { seq: 1, kind: "tool", tool: "read_file", summary: "Read 3 files" }),
      event(2, "step", { seq: 2, kind: "tool", tool: "run_tests", summary: "Ran your tests - 2 failed" }),
      event(3, "step", { seq: 3, kind: "model", summary: "Validated model turn" }),
    ]} />);

    const list = screen.getByRole("list", { name: "Agent activity" });
    expect(within(list).getByText("Read 3 files")).toBeInTheDocument();
    expect(within(list).getByText("Ran your tests - 2 failed")).toBeInTheDocument();
    expect(within(list).getByText("Asked the model what to do next")).toBeInTheDocument();
    await user.click(within(list).getByText("Read 3 files"));
    expect(within(list).getByText("Tool: read_file")).toBeVisible();
    expect(within(list).getByText("Details: Read 3 files")).toBeVisible();
  });

  it("shows the plan a plan step carries, with each item's status (#584)", async () => {
    const user = userEvent.setup();
    render(<RunTimeline events={[
      event(1, "step", {
        seq: 1, kind: "plan", tool: "update_plan", summary: "Plan updated (3 items)",
        plan: [
          { text: "Read the failing test", status: "done" },
          { text: "Fix the parser", status: "in_progress" },
          { text: "Run the tests", status: "pending" },
        ],
      }),
      event(2, "step", { seq: 2, kind: "plan", summary: "Plan updated (1 item)", plan: [{ text: "<b>x</b>", status: "constructor" }] }),
    ]} />);

    await user.click(screen.getByText("Plan updated (3 items)"));
    const plan = screen.getByRole("list", { name: "Agent plan" });
    expect(within(plan).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      "Done: Read the failing test", "In progress: Fix the parser", "To do: Run the tests",
    ]);
    // A malformed plan shows no plan rather than a guess.
    await user.click(screen.getByText("Plan updated (1 item)"));
    expect(screen.queryByText(/<b>x<\/b>/)).not.toBeInTheDocument();
    expect(screen.getAllByRole("list", { name: "Agent plan" })).toHaveLength(1);
  });

  it("keeps each command chunk separate, literal and distinct from the agent answer", async () => {
    const user = userEvent.setup();
    const malicious = '<a href="javascript:alert(1)">click</a> [link](https://example.test)\n'
      + '<img src=x onerror="alert(1)"><script>doBadThings()</script>';
    const { container } = render(<RunTimeline events={[
      event(1, "step", { seq: 1, kind: "tool", summary: malicious }),
      event(2, "output", { source: "command", tool_call_id: "call-1", text: malicious }),
      event(3, "output", { source: "command", tool_call_id: "call-1", text: "second chunk" }),
      event(4, "output", { source: "agent", text: "Final answer" }),
    ]} />);

    const list = screen.getByRole("list", { name: "Agent activity" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(4);
    expect(within(list).getAllByText("Command output")).toHaveLength(2);
    expect(within(list).getAllByText("Agent response")).toHaveLength(1);
    expect(container.querySelector("a, img, script")).toBeNull();

    for (const label of screen.getAllByText("Command output")) await user.click(label);
    const chunks = screen.getAllByLabelText("Command output text");
    expect(chunks.map((chunk) => chunk.textContent)).toEqual([malicious, "second chunk"]);
    expect(chunks[0].tagName).toBe("PRE");
    expect(chunks[0]).toHaveClass("agent-command-output");
    expect(container.querySelector("a, img, script")).toBeNull();

    await user.click(screen.getByText("Agent response"));
    expect(screen.getByLabelText("Agent response text")).toHaveTextContent("Final answer");
  });

  it("shows Cortex's own notices open, labelled apart from command output and the agent", () => {
    render(<RunTimeline events={[
      event(1, "output", { source: "cortex", text: "The sandbox cannot use python from C:\\Python314." }),
      event(2, "output", { source: "command", text: "python: not recognized" }),
    ]} />);
    expect(screen.getByText("Cortex notice")).toBeInTheDocument();
    expect(screen.getByLabelText("Cortex notice text")).toBeVisible();
    expect(screen.getByLabelText("Cortex notice text")).toHaveTextContent("The sandbox cannot use python");
    expect(screen.getByText("Command output")).toBeInTheDocument();
    expect(screen.queryByText("Agent response")).not.toBeInTheDocument();
  });

  it("does not attribute unmarked or unknown output to the agent", () => {
    render(<RunTimeline events={[
      event(1, "output", { text: "legacy shell output" }),
      event(2, "output", { source: "unexpected", text: "untrusted output" }),
      event(3, "output", { source: "agent", text: "final answer" }),
    ]} />);
    expect(screen.getAllByText("Command output")).toHaveLength(2);
    expect(screen.getAllByText("Agent response")).toHaveLength(1);
  });
});
