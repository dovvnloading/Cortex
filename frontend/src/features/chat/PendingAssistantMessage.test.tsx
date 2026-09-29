import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { GENERATION_GAP_NOTICE } from "../../lib/generationStatus";
import { useChatStore } from "../../stores/useChatStore";
import { PendingAssistantMessage } from "./PendingAssistantMessage";

describe("PendingAssistantMessage", () => {
  it("shows the text so far and says the full answer is coming once part of it was missed", () => {
    useChatStore.getState().beginGeneration("job-1", "thread-1");
    useChatStore.getState().appendContentToken("job-1", "The part that arrived");
    useChatStore.getState().markGenerationGap("job-1");

    render(<PendingAssistantMessage onOutputChange={vi.fn()} />);

    expect(screen.getByText("The part that arrived")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent(GENERATION_GAP_NOTICE);
  });

  it("uses the notice as the status line when none of the answer could be shown", () => {
    useChatStore.getState().beginGeneration("job-1", "thread-1");
    useChatStore.getState().markGenerationGap("job-1");
    // Reconnect and progress messages keep arriving; they must not hide it.
    useChatStore.getState().setStatusText("job-1", "Connection interrupted. Retrying in 1s...");

    render(<PendingAssistantMessage onOutputChange={vi.fn()} />);

    expect(screen.getByRole("status")).toHaveTextContent(GENERATION_GAP_NOTICE);
    expect(screen.queryByText(/connection interrupted/i)).not.toBeInTheDocument();
  });

  it("shows no notice for an answer that arrived whole", () => {
    useChatStore.getState().beginGeneration("job-1", "thread-1");
    useChatStore.getState().appendContentToken("job-1", "All of it");

    render(<PendingAssistantMessage onOutputChange={vi.fn()} />);

    expect(screen.getByText("All of it")).toBeVisible();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
