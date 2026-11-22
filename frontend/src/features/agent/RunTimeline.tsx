import type { AgentSSEEvent } from "../../../../contracts/cortex-api";

export type RunTimelineProps = { events: readonly AgentSSEEvent[] };

function textField(data: Record<string, unknown>, field: string): string | null {
  return typeof data[field] === "string" ? data[field] as string : null;
}

function isAgentResponse(event: AgentSSEEvent): boolean {
  // Missing or unknown provenance is untrusted output, never an agent answer.
  return event.event === "output" && event.data.source === "agent";
}

/** Cortex's own note to the user (for example, a tool the sandbox cannot use). */
function isCortexNotice(event: AgentSSEEvent): boolean {
  return event.event === "output" && event.data.source === "cortex";
}

function stepTitle(event: AgentSSEEvent): string {
  const { data } = event;
  if (event.event === "output") {
    if (isAgentResponse(event)) return "Agent response";
    return isCortexNotice(event) ? "Cortex notice" : "Command output";
  }
  const summary = textField(data, "summary")?.trim();
  // The run store intentionally records generic operation labels. Translate
  // them without pretending to know file counts or test outcomes.
  if (summary && summary !== "Registered tool dispatched" && summary !== "Validated model turn") {
    return summary;
  }
  const tool = textField(data, "tool");
  if (tool === "read_file") return "Read a project file";
  if (tool === "replace_file") return "Prepared a change in the work copy";
  if (tool === "run_tests" || tool === "test") return "Ran project tests";
  if (event.data.kind === "model") return "Asked the model what to do next";
  return "Worked on your request";
}

/**
 * Every untrusted string is rendered as React text, including source, command
 * and model output. Never interpret an SSE field as HTML or Markdown.
 */
export function RunTimeline({ events }: RunTimelineProps) {
  const entries = events.filter((event) => event.event === "step" || event.event === "output");
  return (
    <section className="coding-panel agent-timeline" aria-label="Agent timeline">
      <h3>Live timeline</h3>
      {entries.length === 0 ? (
        <p className="coding-muted" role="status">Waiting for the first step…</p>
      ) : (
        <ol aria-label="Agent activity" aria-live="polite">
          {entries.map((event) => (
            <li key={event.event_id}>
              <details open={isCortexNotice(event) || undefined}>
                <summary>{stepTitle(event)}</summary>
                {event.event === "output" ? (
                  <pre
                    className={isAgentResponse(event) ? "coding-source" : "coding-source agent-command-output"}
                    aria-label={isAgentResponse(event) ? "Agent response text"
                      : isCortexNotice(event) ? "Cortex notice text" : "Command output text"}
                  >{textField(event.data, "text") ?? (isAgentResponse(event) ? "(No response text)" : "(No command output)")}</pre>
                ) : (
                  <div className="coding-muted">
                    <p>Step {typeof event.data.seq === "number" ? event.data.seq : "—"}
                      {typeof event.data.kind === "string" ? " · " + event.data.kind : ""}
                    </p>
                    {typeof event.data.tool === "string" && <p>Tool: {event.data.tool}</p>}
                    {typeof event.data.summary === "string" && <p>Details: {event.data.summary}</p>}
                  </div>
                )}
              </details>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
