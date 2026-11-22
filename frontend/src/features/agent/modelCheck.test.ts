import { describe, expect, it } from "vitest";
import { modelCheckWarning } from "./modelCheck";

describe("modelCheckWarning", () => {
  it("names each failed check in plain language", () => {
    expect(modelCheckWarning(["ordered_calls"])).toBe(
      "This model did not pass Cortex's tool check. It could not make tool calls in the right order. Review every approval carefully.",
    );
  });

  it("warns about planted instructions when the injection check failed", () => {
    const text = modelCheckWarning(["injection_resistance"]);
    expect(text).toMatch(/ignore instructions planted in tool results/);
    expect(text).toMatch(/may follow instructions hidden in project files/);
  });

  it("still shows a check it does not know, and inherited keys are not known checks", () => {
    expect(modelCheckWarning(["new_check"])).toMatch(/It could not new_check\./);
    expect(modelCheckWarning(["toString"])).toMatch(/It could not toString\./);
  });
});
