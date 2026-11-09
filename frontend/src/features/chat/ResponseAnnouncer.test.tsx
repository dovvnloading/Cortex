import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { useChatStore } from "../../stores/useChatStore";
import { ResponseAnnouncer } from "./ResponseAnnouncer";

function resetGeneration() {
  const state = useChatStore.getState();
  if (state.generation.jobId) state.endGeneration(state.generation.jobId);
  useChatStore.setState({ lastCompletion: null, lastFailure: null });
}

beforeEach(resetGeneration);
afterEach(resetGeneration);

describe("ResponseAnnouncer", () => {
  it("does not announce a generation that started in another chat", () => {
    render(<ResponseAnnouncer threadId="thread-b" />);
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("");

    act(() => useChatStore.getState().beginGeneration("job-a", "thread-a"));
    expect(status).toHaveTextContent("");

    act(() => {
      useChatStore.getState().endGeneration("job-a");
      useChatStore.getState().beginGeneration("job-b", "thread-b");
    });
    expect(status).toHaveTextContent("Response started");
  });

  it("re-scopes announcements after navigating to another thread", () => {
    const view = render(<ResponseAnnouncer threadId="thread-a" />);
    const status = screen.getByRole("status");

    act(() => useChatStore.getState().beginGeneration("job-a", "thread-a"));
    expect(status).toHaveTextContent("Response started");
    act(() => useChatStore.getState().endGeneration("job-a"));
    view.rerender(<ResponseAnnouncer threadId="thread-b" />);

    act(() => useChatStore.getState().beginGeneration("job-a2", "thread-a"));
    expect(status).toHaveTextContent("Response started"); // no new unrelated announcement
    act(() => {
      useChatStore.getState().endGeneration("job-a2");
      useChatStore.getState().beginGeneration("job-b", "thread-b");
    });
    expect(status).toHaveTextContent("Response started");
  });
});
