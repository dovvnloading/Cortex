import { useMemo, useState, type FormEvent } from "react";
import { Bot, FolderOpen, Square } from "lucide-react";
import type { StartAgentRun } from "../../../../contracts/cortex-api";
import type { CortexApi } from "../../api/client";
import { cloudModelProvider, displayModelName } from "../../lib/localModels";
import { chooseWorkspaceDirectory } from "../../lib/nativeFolderPicker";
import { NavigationLink } from "../shell/NavigationLink";
import { ApprovalCard } from "./ApprovalCard";
import { AgentChangesPanel } from "./AgentChangesPanel";
import { createAgentApi } from "./agentApi";
import { RunHistory } from "./RunHistory";
import { SavedRecoveryPanel } from "./SavedRecoveryPanel";
import { RunTimeline } from "./RunTimeline";
import { stopReasonText } from "./stopReasons";
import { useAgentRun } from "./useAgentRun";

export type AgentPageProps = {
  api: CortexApi;
  enabled: boolean;
  localModels: readonly string[];
  cloudModels: readonly string[];
  selectedModel: string | null;
};

const STARTERS = [
  "Fix failing tests",
  "Explain this project",
  "Add a feature",
  "Start a new app in an empty folder",
] as const;

const MODES: { value: StartAgentRun["mode"]; label: string; explanation: string }[] = [
  { value: "plan_only", label: "Plan only", explanation: "Explore the project and suggest a plan without editing files." },
  { value: "ask", label: "Ask before every command", explanation: "Pause for an explicit decision on actions that require approval." },
  { value: "auto_sandboxed", label: "Auto-approve safe commands in the sandbox", explanation: "Only backend-permitted safe actions can proceed; other actions still require approval." },
];

const TERMINAL = new Set(["completed", "failed", "cancelled", "interrupted"]);

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    pending: "Preparing your work copy",
    running: "Working on your goal",
    waiting_approval: "Waiting for your approval",
    completed: "Run completed",
    failed: "Run failed",
    cancelled: "Run stopped",
    interrupted: "Run interrupted",
  };
  return labels[status] ?? "Run status unavailable";
}

export function AgentPage({ api, enabled, localModels, cloudModels, selectedModel }: AgentPageProps) {
  const agentApi = useMemo(() => createAgentApi(api), [api]);
  const agent = useAgentRun(agentApi, enabled);
  const [goal, setGoal] = useState("");
  const [root, setRoot] = useState("");
  const [folderConsent, setFolderConsent] = useState(false);
  const [cloudConsent, setCloudConsent] = useState(false);
  const [modelChoice, setModelChoice] = useState("");
  const [mode, setMode] = useState<StartAgentRun["mode"]>("plan_only");
  const [pickerError, setPickerError] = useState<string | null>(null);
  const [approvalView, setApprovalView] = useState(0);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [recoveryRefreshKey, setRecoveryRefreshKey] = useState(0);

  const availableModels = Array.from(new Set([...localModels, ...cloudModels]));
  const model = availableModels.includes(modelChoice) ? modelChoice
    : (selectedModel && availableModels.includes(selectedModel) ? selectedModel : availableModels[0] ?? "");
  const provider = cloudModelProvider(model);
  const active = Boolean(agent.run && !TERMINAL.has(agent.run.status));
  const stopReason = stopReasonText(agent.run?.stop_reason);
  const canStart = Boolean(enabled && goal.trim() && root.trim() && model && folderConsent
    && (!provider || cloudConsent) && !active && !agent.busy);

  const openHistory = (runId: string) => {
    if (active && agent.run?.id !== runId) {
      setHistoryError("Stop or finish the current run before opening another run.");
      return;
    }
    setHistoryError(null);
    void agent.open(runId);
  };

  const chooseFolder = async () => {
    setPickerError(null);
    try {
      const selected = await chooseWorkspaceDirectory();
      if (selected) {
        setRoot(selected);
        setFolderConsent(false);
        setCloudConsent(false);
      }
    } catch {
      setPickerError("The native folder picker is unavailable. Enter an absolute project path instead.");
    }
  };

  const start = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    // A checked box is the only way these flags can be true. The API facade
    // and backend independently reject missing project/cloud confirmation.
    if (!canStart || !folderConsent || (provider && !cloudConsent)) return;
    const started = await agent.start({
      goal: goal.trim(),
      project_root: root.trim(),
      mode,
      model,
      confirmed_project: folderConsent,
      confirmed_cloud: Boolean(provider && cloudConsent),
    });
    if (started) {
      setFolderConsent(false);
      setCloudConsent(false);
    }
  };

  if (!enabled) {
    return (
      <div className="coding-workspace">
        <header className="coding-header">
          <div><span className="coding-eyebrow"><Bot size={15} aria-hidden="true" /> Agent mode</span><h2>Agent mode is off</h2></div>
        </header>
        <section className="coding-panel" role="status">
          <p>Agent mode is disabled by default. To use it, open Settings, turn on <strong>Enable Agent mode</strong> under General, and save your settings.</p>
          <NavigationLink to="/settings" className="coding-primary">Open Settings</NavigationLink>
        </section>
        <SavedRecoveryPanel api={api} />
      </div>
    );
  }

  return (
    <div className="coding-workspace">
      <header className="coding-header">
        <div>
          <span className="coding-eyebrow"><Bot size={15} aria-hidden="true" /> Opt-in coding agent</span>
          <h2>Agent mode</h2>
          <p>Tell Cortex what you want done. It works in a separate copy of your chosen project; changes to your original files require a separate review and acceptance.</p>
        </div>
      </header>

      <form className="coding-panel coding-open" onSubmit={(event) => void start(event)}>
        <label htmlFor="agent-goal">What would you like to accomplish?</label>
        <textarea
          id="agent-goal"
          placeholder="Describe your goal in everyday language…"
          value={goal}
          maxLength={20000}
          rows={4}
          disabled={active || Boolean(agent.busy)}
          onChange={(event) => setGoal(event.target.value)}
        />
        <div className="coding-row agent-starters" role="group" aria-label="Starter goals">
          {STARTERS.map((starter) => (
            <button key={starter} className="coding-quiet" type="button" disabled={active || Boolean(agent.busy)}
              onClick={() => setGoal(starter)}>{starter}</button>
          ))}
        </div>

        <label htmlFor="agent-folder">Project folder</label>
        <div className="coding-row">
          <input id="agent-folder" type="text" placeholder="C:\src\my-project"
            autoComplete="off" maxLength={4096} value={root} disabled={active || Boolean(agent.busy)}
            onChange={(event) => { setRoot(event.target.value); setFolderConsent(false); setCloudConsent(false); }} />
          <button type="button" className="coding-quiet" disabled={active || Boolean(agent.busy)}
            onClick={() => void chooseFolder()}><FolderOpen size={16} aria-hidden="true" /> Browse</button>
        </div>
        {pickerError && <p role="alert" className="coding-error">{pickerError}</p>}
        <label className="coding-consent">
          <input type="checkbox" checked={folderConsent} disabled={active || Boolean(agent.busy)}
            onChange={(event) => setFolderConsent(event.target.checked)} />
          I approve Cortex reading and copying eligible files from this trusted folder for this run. It will not apply proposed edits to my original files without a separate review.
        </label>

        <label htmlFor="agent-model">Model</label>
        <select id="agent-model" value={model} disabled={active || Boolean(agent.busy)}
          onChange={(event) => { setModelChoice(event.target.value); setCloudConsent(false); }}>
          {availableModels.length === 0 && <option value="">No models available — configure a model in Settings</option>}
          {availableModels.map((name) => (
            <option key={name} value={name}>{displayModelName(name)}{cloudModelProvider(name) ? " (cloud)" : " (local)"}</option>
          ))}
        </select>

        {provider && (
          <div className="coding-panel" role="group" aria-label="Cloud model warning">
            <p><strong>Cloud model warning:</strong> Your files will be sent to {provider}. The goal and eligible project context may leave your computer and be processed by this provider.</p>
            <label className="coding-consent">
              <input type="checkbox" checked={cloudConsent} disabled={active || Boolean(agent.busy)}
                onChange={(event) => setCloudConsent(event.target.checked)} />
              I understand and explicitly approve sending project context to {provider} for this run.
            </label>
          </div>
        )}

        <fieldset className="agent-modes" disabled={active || Boolean(agent.busy)}>
          <legend>How should Agent mode work?</legend>
          {MODES.map((choice) => (
            <label className="coding-consent" key={choice.value}>
              <input type="radio" name="agent-mode" value={choice.value} checked={mode === choice.value}
                onChange={() => setMode(choice.value)} />
              <span><strong>{choice.label}</strong> — {choice.explanation}</span>
            </label>
          ))}
        </fieldset>
        <div className="coding-row">
          <button type="submit" className="coding-primary" disabled={!canStart}>
            {agent.busy === "starting" ? "Starting…" : "Start Agent"}
          </button>
          <p className="coding-muted">No command or file change is approved by this page automatically.</p>
        </div>
      </form>

      {agent.error && <div className="coding-error" role="alert">{agent.error}</div>}
      {agent.run && (
        <section aria-label="Current agent run">
          <div className="coding-panel coding-row">
            <div>
              <h3>{statusLabel(agent.run.status)}</h3>
              <p className="coding-muted">Model: {displayModelName(agent.run.model)} · Mode: {MODES.find((choice) => choice.value === agent.run?.mode)?.label ?? agent.run.mode}</p>
              {stopReason && <p className="coding-muted">{stopReason}</p>}
              {agent.connection === "reconnecting" && <p role="status">Connection interrupted — reconnecting to the run…</p>}
            </div>
            {active && (
              <button type="button" className="coding-quiet" disabled={Boolean(agent.busy)} onClick={() => void agent.stop()}>
                <Square size={15} aria-hidden="true" /> {agent.busy === "stopping" ? "Stopping…" : "Stop"}
              </button>
            )}
            {agent.resumeRequired && <button type="button" className="coding-quiet" disabled={Boolean(agent.busy)}
              onClick={() => void agent.resume()}>Resume run</button>}
          </div>

          <RunTimeline events={agent.events} />

          {agent.pendingApproval && (
            <>
              <section className="coding-panel" aria-label="Pending command approval">
                <h3>Approval needed</h3>
                <p>Review the proposed action before the run can continue. You can deny it or stop the run.</p>
                <button type="button" className="coding-quiet" onClick={() => setApprovalView((previous) => previous + 1)}>
                  Review pending approval
                </button>
              </section>
              <ApprovalCard
                key={agent.run.id + ":" + agent.pendingApproval.seq + ":" + approvalView}
                request={agent.pendingApproval.request}
                allowAlways={false}
                onAllowOnce={() => void agent.decide(agent.pendingApproval!.seq, "allow_once")}
                onAlwaysAllow={() => undefined}
                onDeny={() => void agent.decide(agent.pendingApproval!.seq, "deny")}
              />
            </>
          )}
          {agent.run.status === "waiting_approval" && !agent.pendingApproval && (
            <p role="alert" className="coding-error">The approval details are unavailable. No action will be approved automatically; you can stop this run.</p>
          )}

          {TERMINAL.has(agent.run.status) && (
            <AgentChangesPanel key={agent.run.id} api={agentApi} runId={agent.run.id}
              onRecoveryChanged={() => setRecoveryRefreshKey((current) => current + 1)} />
          )}
        </section>
      )}

      {historyError && <p role="alert" className="coding-error">{historyError}</p>}
      <RunHistory
        api={agentApi}
        onOpen={openHistory}
        onDiscarded={(runId) => agent.forget(runId)}
        refreshKey={agent.run?.id}
      />
      <SavedRecoveryPanel api={api} refreshKey={recoveryRefreshKey} />
    </div>
  );
}
