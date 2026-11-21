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

  it("shows output and file text literally, without constructing HTML elements", async () => {
    const user = userEvent.setup();
    const malicious = '<img src=x onerror="alert(1)"><script>doBadThings()</script>';
    const { container } = render(<RunTimeline events={[
      event(1, "step", { seq: 1, kind: "tool", summary: malicious }),
      event(2, "output", { text: malicious }),
    ]} />);
    expect(container.querySelector("script, img")).toBeNull();
    await user.click(screen.getByText("Agent response"));
    expect(screen.getByLabelText("Agent response text")).toHaveTextContent(malicious);
    expect(container.querySelector("script, img")).toBeNull();
  });
});
