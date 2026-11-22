/**
 * Plain-language text for the stop reason codes the agent backend records
 * (agent/loop.py, agent/run_store.py, services/agent_runs.py). A finished run
 * needs no explanation; an unknown code is still shown so nothing is hidden.
 */
const STOP_REASONS: Record<string, string> = {
  cancelled: "You stopped the run.",
  wall_clock_budget: "The run reached its time limit.",
  model_call_budget: "The run reached its limit on model calls.",
  tool_call_budget: "The run reached its limit on tool calls.",
  step_budget: "The run reached its step limit.",
  context_budget: "The conversation outgrew the model's context limit.",
  context_error: "Cortex could not prepare the next request to the model.",
  invalid_model_replies: "The model kept answering in a format Cortex cannot use. Try another model.",
  model_error: "The model returned an error or could not be reached. Check that it is installed and running.",
  repeated_tool_call: "The model repeated the same action three times, so the run stopped.",
  tool_error: "A tool returned an unexpected result.",
  tool_interrupted: "A tool was interrupted before it finished.",
  hook_error: "An internal step after a tool failed.",
  check_inspection_failed: "Cortex could not inspect the work copy to run the project checks.",
  approval_repeated: "An approved action asked for approval again, so the run stopped.",
  approval_unavailable: "Cortex could not read the approval decision.",
  approval_expired: "The approval request expired before a decision was made.",
  process_restart: "Cortex restarted while this run was active.",
  agent_error: "The agent stopped because of an internal error.",
};

export function stopReasonText(reason: string | null | undefined): string | null {
  if (!reason || reason === "final_answer") return null;
  return Object.hasOwn(STOP_REASONS, reason) ? STOP_REASONS[reason] : `Stopped: ${reason}`;
}
