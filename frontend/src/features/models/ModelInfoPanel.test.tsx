import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ModelInfoPanel } from "./ModelInfoPanel";

describe("ModelInfoPanel", () => {
  it("renders the model name and a detail summary when metadata is present", () => {
    render(<ModelInfoPanel model={{ name: "qwen3:8b", parameter_size: "8.0B", quantization_level: "Q4_K_M", context_length: 40960, size: 5_000_000_000 }} />);

    expect(screen.getByText("qwen3:8b")).toBeInTheDocument();
    expect(screen.getByText(/8\.0B params/)).toBeInTheDocument();
    expect(screen.getByText(/Q4_K_M/)).toBeInTheDocument();
    expect(screen.getByText(/40,960 ctx/)).toBeInTheDocument();
  });

  it("renders only the name when no detail metadata is available", () => {
    const { container } = render(<ModelInfoPanel model={{ name: "unknown-model" }} />);

    expect(screen.getByText("unknown-model")).toBeInTheDocument();
    expect(container.querySelector(".model-info-detail")).toBeNull();
  });

  it("omits fields that are individually missing rather than showing a placeholder", () => {
    render(<ModelInfoPanel model={{ name: "qwen3:8b", parameter_size: "8.0B" }} />);
    expect(screen.getByText("8.0B params")).toBeInTheDocument();
  });
});
