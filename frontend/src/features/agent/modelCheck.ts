const MODEL_CHECKS: Record<string, string> = {
  single_call: "make a single tool call",
  ordered_calls: "make tool calls in the right order",
  injection_resistance: "ignore instructions planted in tool results",
};

/** Plain warning for a model that failed part of Cortex's one-time tool check. */
export function modelCheckWarning(failedChecks: readonly string[]): string {
  const failed = failedChecks.map((name) => Object.hasOwn(MODEL_CHECKS, name) ? MODEL_CHECKS[name] : name);
  const list = failed.length > 0 ? ` It could not ${failed.join("; ")}.` : "";
  const injection = failedChecks.includes("injection_resistance")
    ? " It may follow instructions hidden in project files, so treat its requests with suspicion." : "";
  return `This model did not pass Cortex's tool check.${list}${injection} Review every approval carefully.`;
}
