import { describe, expect, it } from "vitest";
import { humanizeGenerationStatus } from "../lib/generationStatus";

describe("humanizeGenerationStatus", () => {
  it("never exposes an internal all-caps control marker", () => {
    expect(humanizeGenerationStatus("START_FINAL_ANIMATION")).toBe("Generating a response...");
  });

  it("keeps useful human-facing progress text", () => {
    expect(humanizeGenerationStatus("Analyzing the request...")).toBe("Analyzing the request...");
  });
});
