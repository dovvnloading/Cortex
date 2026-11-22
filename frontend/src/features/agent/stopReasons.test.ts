import { describe, expect, it } from "vitest";
import { stopReasonText } from "./stopReasons";

describe("stopReasonText", () => {
  it("explains backend codes in plain language", () => {
    expect(stopReasonText("wall_clock_budget")).toBe("The run reached its time limit.");
    expect(stopReasonText("invalid_model_replies")).toMatch(/Try another model/);
    expect(stopReasonText("cancelled")).toBe("You stopped the run.");
  });

  it("says nothing for a normal finish or a missing reason", () => {
    expect(stopReasonText("final_answer")).toBeNull();
    expect(stopReasonText(null)).toBeNull();
    expect(stopReasonText(undefined)).toBeNull();
    expect(stopReasonText("")).toBeNull();
  });

  it("still shows a code it does not know", () => {
    expect(stopReasonText("new_reason")).toBe("Stopped: new_reason");
  });

  it("never treats inherited object keys as known reasons", () => {
    expect(stopReasonText("toString")).toBe("Stopped: toString");
    expect(stopReasonText("constructor")).toBe("Stopped: constructor");
  });
});
