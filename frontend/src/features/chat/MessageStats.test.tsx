import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MessageStats } from "./MessageStats";

describe("MessageStats", () => {
  it("renders nothing when there are no stats", () => {
    const { container } = render(<MessageStats stats={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when tokens_per_second is missing", () => {
    const { container } = render(<MessageStats stats={{ eval_count: 10 }} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows token count, throughput, and duration when fully present", () => {
    render(<MessageStats stats={{ eval_count: 48, tokens_per_second: 100, total_duration_ms: 620 }} />);
    expect(screen.getByText(/48 tok/)).toBeInTheDocument();
    expect(screen.getByText(/100 tok\/s/)).toBeInTheDocument();
    expect(screen.getByText(/0\.6s/)).toBeInTheDocument();
  });

  it("omits duration when total_duration_ms is absent", () => {
    render(<MessageStats stats={{ eval_count: 48, tokens_per_second: 100 }} />);
    expect(screen.getByTitle("Generation performance for this response").textContent).not.toMatch(/·\s*\d+(\.\d+)?s\b/);
  });
});
