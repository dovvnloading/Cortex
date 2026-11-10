import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Check, Code2, FolderOpen, Play, RotateCcw, Search, Square, X } from "lucide-react";
import { CortexApi, describeApiError } from "../../api/client";
import type {
  WorkspaceAgentAnswer, WorkspaceCheckResult, WorkspaceExcerpt, WorkspaceHit,
  WorkspaceOpened, WorkspacePreview,
} from "../../api/workspaceTypes";
import { registerNavigationGuard } from "../../lib/navigation";

export type WorkspacePageProps = {
  api: CortexApi;
  localModels: readonly string[];
  selectedModel: string | null;
};

type DiffLine = { left: string; right: string; changed: boolean };

function diffRows(diff: string): DiffLine[] {
  const rows: DiffLine[] = [];
  let removed: string[] = [];
  let added: string[] = [];
  const flush = () => {
    for (let i = 0; i < Math.max(removed.length, added.length); i += 1) {
      rows.push({ left: removed[i] ?? "", right: added[i] ?? "", changed: true });
    }
    removed = [];
    added = [];
  };
  for (const line of diff.split(/\r?\n/)) {
    if (line.startsWith("--- ") || line.startsWith("+++ ")) continue;
    if (line.startsWith("-")) removed.push(line.slice(1));
    else if (line.startsWith("+")) added.push(line.slice(1));
    else {
      flush();
      if (line.startsWith(" ")) {
        rows.push({ left: line.slice(1), right: line.slice(1), changed: false });
      } else if (line.startsWith("@@")) {
        rows.push({ left: line, right: line, changed: false });
      } else if (line.startsWith("\\ No newline")) {
        rows.push({ left: line, right: line, changed: false });
      }
    }
  }
  flush();
  return rows;
}

type NativeApi = { choose_workspace_directory?: () => Promise<string | null> };

const CHECKS = [
  { id: "pytest", label: "Python: pytest -q" },
  { id: "npm-test", label: "Node: npm test -- --run" },
  { id: "npm-build", label: "Node: npm run build" },
] as const;

export function WorkspacePage({ api, localModels, selectedModel }: WorkspacePageProps) {
  const [root, setRoot] = useState("");
  const [readConsent, setReadConsent] = useState(false);
  const [opened, setOpened] = useState<WorkspaceOpened | null>(null);
  const sessionRef = useRef<string | null>(null);
  const undoRef = useRef(false);
  const [paths, setPaths] = useState<string[]>([]);
  const [searchText, setSearchText] = useState("");
  const [hits, setHits] = useState<WorkspaceHit[]>([]);
  const [source, setSource] = useState<WorkspaceExcerpt | null>(null);
  const [question, setQuestion] = useState("");
  const [model, setModel] = useState(selectedModel?.startsWith("gguf:") ? "" : selectedModel ?? localModels[0] ?? "");
  const [previewAllowed, setPreviewAllowed] = useState(true);
  const [answer, setAnswer] = useState<WorkspaceAgentAnswer | null>(null);
  const [preview, setPreview] = useState<WorkspacePreview | null>(null);
  const [undoAvailable, setUndoAvailable] = useState(false);
  const [checkKind, setCheckKind] = useState("pytest");
  const [checkResult, setCheckResult] = useState<WorkspaceCheckResult | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => () => {
    const id = sessionRef.current;
    if (id) void api.closeWorkspace(id).catch(() => undefined);
  }, [api]);

  useEffect(() => registerNavigationGuard(() => {
    if (!undoRef.current) return true;
    return window.confirm(
      "Leaving this workspace discards the temporary undo checkpoint, but keeps edits already applied. Continue?",
    );
  }), []);

  const rows = useMemo(() => diffRows(preview?.diff ?? ""), [preview]);

  async function execute<T>(kind: string, operation: () => Promise<T>): Promise<T | undefined> {
    setError(null);
    setNotice(null);
    setBusy(kind);
    try {
      return await operation();
    } catch (cause) {
      setError(describeApiError(cause, "The workspace operation failed. Check Cortex, your project and the local model."));
      return undefined;
    } finally {
      setBusy(null);
    }
  }

  async function openProject(event?: FormEvent) {
    event?.preventDefault();
    if (!readConsent || !root.trim()) return;
    if (undoRef.current && !window.confirm(
      "Refreshing this project loses the temporary undo checkpoint. Applied edits will remain. Continue?",
    )) return;
    await execute("opening", async () => {
      if (sessionRef.current) {
        await api.closeWorkspace(sessionRef.current);
        sessionRef.current = null;
      }
      setOpened(null);
      setPreview(null);
      setAnswer(null);
      setCheckResult(null);
      setUndoAvailable(false);
      undoRef.current = false;
      setSource(null);
      setHits([]);
      const next = await api.openWorkspace(root.trim());
      sessionRef.current = next.session_id;
      const result = await api.workspacePaths(next.session_id);
      setOpened(next);
      setPaths(result.paths);
      setNotice("Indexed " + next.file_count + " eligible files; this consented snapshot expires at " + new Date(next.expires_at).toLocaleTimeString() + ".");
    });
  }

  async function browse() {
    const native = (window as Window & { pywebview?: { api?: NativeApi } }).pywebview?.api;
    if (!native?.choose_workspace_directory) {
      setError("The native folder picker is not available in this browser. Enter an absolute path.");
      return;
    }
    try {
      const chosen = await native.choose_workspace_directory();
      if (chosen) {
        setRoot(chosen);
        setReadConsent(false);
      }
    } catch {
      setError("The folder picker could not open. You can still enter a path.");
    }
  }

  async function closeProject() {
    if (!sessionRef.current) return;
    if (undoRef.current && !window.confirm(
      "Closing this project loses the temporary undo checkpoint. Applied edits will remain. Continue?",
    )) return;
    const id = sessionRef.current;
    await execute("closing", async () => {
      await api.closeWorkspace(id);
      sessionRef.current = null;
      setOpened(null);
      setPreview(null);
      setAnswer(null);
      setSource(null);
      setUndoAvailable(false);
      undoRef.current = false;
      setPaths([]);
    });
  }

  async function search(event: FormEvent) {
    event.preventDefault();
    if (!sessionRef.current || searchText.trim().length < 2) return;
    const id = sessionRef.current;
    await execute("searching", async () => setHits((await api.workspaceSearch(id, searchText.trim())).matches));
  }

  async function read(path: string, line = 1) {
    if (!sessionRef.current) return;
    const id = sessionRef.current;
    await execute("reading", async () => setSource(await api.workspaceRead(id, path, line)));
  }

  async function research(event: FormEvent) {
    event.preventDefault();
    if (!sessionRef.current || !model || !question.trim()) return;
    const id = sessionRef.current;
    await execute("researching", async () => {
      setPreview(null);
      const result = await api.workspaceAsk(id, question.trim(), model, previewAllowed);
      setAnswer(result);
      if (result.status === "completed" && result.preview_diff) {
        setPreview({
          diff: result.preview_diff, paths: result.preview_paths,
          replacements: result.preview_replacements, applied_to_checkout: false,
        });
      } else if (result.status !== "completed") {
        setNotice("Research ended: " + result.status + ". No changes were applied.");
      }
    });
  }

  async function apply() {
    const id = sessionRef.current;
    if (!id || !preview?.diff) return;
    if (!window.confirm("Apply the reviewed patch to " + preview.paths.join(", ") + "? Cortex will recheck file hashes and retain an in-memory undo checkpoint.")) return;
    await execute("applying", async () => {
      const result = await api.workspaceApply(id);
      setUndoAvailable(result.undo_available);
      undoRef.current = result.undo_available;
      setPreview(null);
      setNotice("Applied edits to " + result.paths.join(", ") + ". Undo is available only while this session is open.");
    });
  }

  async function undo() {
    const id = sessionRef.current;
    if (!id || !undoAvailable) return;
    if (!window.confirm("Restore the original files? Changes by another editor will block undo.")) return;
    await execute("undoing", async () => {
      await api.workspaceUndo(id);
      setUndoAvailable(false);
      undoRef.current = false;
      setNotice("Original files restored. Reopen the project before proposing another edit.");
    });
  }

  async function check() {
    const id = sessionRef.current;
    if (!id) return;
    const label = CHECKS.find((item) => item.id === checkKind)?.label ?? checkKind;
    if (!window.confirm("Run " + label + "?\n\nProject tests and build scripts execute with YOUR operating-system permissions. Windows job limits are not a filesystem or network sandbox. Only run commands on projects you trust.")) return;
    await execute("checking", async () => {
      const result = await api.workspaceCheck(id, checkKind);
      setCheckResult(result);
      setNotice("Check finished: " + result.status + ".");
    });
  }

  async function cancel() {
    if (!sessionRef.current) return;
    try {
      await api.workspaceCancel(sessionRef.current);
      setNotice("Cancellation requested.");
    } catch (cause) {
      setError(describeApiError(cause, "Could not cancel this operation."));
    }
  }

  return <div className="coding-workspace">
    <header className="coding-header">
      <div>
        <span className="coding-eyebrow"><Code2 size={15} aria-hidden="true" /> Local coding agent</span>
        <h2>Project workspace</h2>
        <p>Research a trusted project, review source edits, and run explicitly approved checks. The model does not receive shell or write access.</p>
      </div>
      {opened && <button type="button" className="coding-quiet" disabled={!!busy} onClick={() => void closeProject()}><X size={15} /> Close project</button>}
    </header>

    <form className="coding-panel coding-open" onSubmit={(event) => void openProject(event)}>
      <label htmlFor="coding-path">Project folder</label>
      <div className="coding-row">
        <input id="coding-path" aria-label="Project folder" placeholder="C:\src\my-project" value={root}
          onChange={(event) => { setRoot(event.target.value); setReadConsent(false); }} autoComplete="off" />
        <button className="coding-quiet" type="button" onClick={() => void browse()}><FolderOpen size={16} /> Browse</button>
      </div>
      <label className="coding-consent">
        <input type="checkbox" checked={readConsent} onChange={(event) => setReadConsent(event.target.checked)} />
        I approve reading eligible source files from this trusted folder for one temporary snapshot.
      </label>
      <div className="coding-row">
        <button type="submit" className="coding-primary" disabled={!root.trim() || !readConsent || !!busy}><FolderOpen size={16} /> {opened ? "Refresh snapshot" : "Open project"}</button>
        {opened && <span className="coding-muted">{opened.file_count} files · {opened.truncated ? "partial snapshot" : "bounded snapshot"}</span>}
      </div>
    </form>
    {error && <div className="coding-error" role="alert">{error}</div>}
    {notice && <div className="coding-notice" role="status">{notice}</div>}

    {opened && <>
      <div className="coding-two-column">
        <section className="coding-panel">
          <h3>Source files</h3>
          <form className="coding-row" onSubmit={(event) => void search(event)}>
            <input aria-label="Search project source" placeholder="Find a symbol or phrase" value={searchText}
              onChange={(event) => setSearchText(event.target.value)} />
            <button className="coding-quiet" type="submit" disabled={!!busy || searchText.trim().length < 2}><Search size={16} /> Search</button>
          </form>
          {hits.length > 0 && <div className="coding-file-list">
            {hits.map((hit, index) => <button key={hit.path + hit.line + ":" + index} type="button"
              disabled={!!busy} onClick={() => void read(hit.path)}>{hit.path}:{hit.line} — {hit.text}</button>)}
          </div>}
          <div className="coding-file-list" aria-label="Indexed files">
            {paths.map((path) => <button key={path} type="button" disabled={!!busy} aria-pressed={source?.path === path}
              onClick={() => void read(path)}>{path}</button>)}
          </div>
        </section>
        <section className="coding-panel">
          <h3>{source?.path ?? "Source preview"}</h3>
          {source ? <>
            <p className="coding-muted">Lines {source.start_line}–{source.end_line} / {source.total_lines} · SHA-256 {source.sha256.slice(0, 12)}…</p>
            <pre className="coding-source">{source.text}</pre>
            {source.truncated && source.end_line < source.total_lines &&
              <button type="button" className="coding-quiet" disabled={!!busy}
                onClick={() => void read(source.path, source.end_line + 1)}>Read next lines</button>}
          </> : <p className="coding-muted">Choose an indexed file to view its read-only contents.</p>}
        </section>
      </div>
      <section className="coding-panel">
        <h3>Ask your local model</h3>
        <p className="coding-muted">Repository tools currently support local Ollama models only. Each research turn has a bounded number of search/read/preview actions.</p>
        <form className="coding-question" onSubmit={(event) => void research(event)}>
          <div className="coding-row">
            <label htmlFor="coding-model">Model</label>
            <select id="coding-model" value={model} onChange={(event) => setModel(event.target.value)}>
              {!model && <option value="">Select local Ollama model</option>}
              {localModels.map((name) => <option key={name} value={name}>{name}</option>)}
            </select>
            <label className="coding-consent"><input type="checkbox" checked={previewAllowed}
              onChange={(event) => setPreviewAllowed(event.target.checked)} /> Allow in-memory patch previews</label>
          </div>
          <textarea aria-label="Question for local model" rows={4} maxLength={20000}
            placeholder="Locate this parser bug and propose a patch." value={question}
            onChange={(event) => setQuestion(event.target.value)} />
          <div className="coding-row">
            <button className="coding-primary" type="submit" disabled={!model || !question.trim() || !!busy}><Code2 size={16} /> {busy === "researching" ? "Researching…" : "Research project"}</button>
            {(busy === "researching" || busy === "checking") &&
              <button className="coding-quiet" type="button" onClick={() => void cancel()}><Square size={14} /> Stop</button>}
          </div>
        </form>
        {answer && <div className="coding-answer" aria-live="polite">
          <p className="coding-muted">Status: {answer.status} · {answer.model_calls} model calls · {answer.tool_calls} actions</p>
          <p>{answer.answer ?? "This research turn did not complete."}</p>
        </div>}
      </section>
      {preview?.diff && <section className="coding-panel">
        <h3>Review proposed edits</h3>
        <p className="coding-muted">{preview.paths.join(", ")} · {preview.replacements} replacements · No files have been written.</p>
        <div className="coding-diff" role="region" aria-label="Side-by-side patch preview">
          <div className="coding-diff-head"><strong>Original</strong><strong>Proposed</strong></div>
          {rows.map((line, index) => <div key={index} className={"coding-diff-row" + (line.changed ? " coding-diff-change" : "")}>
            <pre>{line.left || " "}</pre><pre>{line.right || " "}</pre>
          </div>)}
        </div>
        <div className="coding-row">
          <button className="coding-primary" type="button" disabled={!!busy} onClick={() => void apply()}><Check size={16} /> Review & apply patch</button>
          <p className="coding-muted">Applying changes requires separate approval and exact source hash checks.</p>
        </div>
      </section>}
      <section className="coding-panel">
        <h3>Verify & recover</h3>
        <p className="coding-muted">Tests run trusted project code with your Windows permissions. A job limit is not a security sandbox.</p>
        <div className="coding-row">
          <label htmlFor="coding-check">Check</label>
          <select id="coding-check" value={checkKind} onChange={(event) => setCheckKind(event.target.value)} disabled={!!busy}>
            {CHECKS.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
          </select>
          <button className="coding-primary" type="button" disabled={!!busy} onClick={() => void check()}><Play size={16} /> Run approved check</button>
          {undoAvailable && <button className="coding-quiet" type="button" disabled={!!busy} onClick={() => void undo()}><RotateCcw size={16} /> Undo applied patch</button>}
        </div>
        {checkResult && <>
          <p className="coding-muted">Result: {checkResult.status} · exit {checkResult.exit_code} · {checkResult.duration_seconds}s{checkResult.output_truncated ? " · output truncated" : ""}</p>
          <pre className="coding-source" aria-label="Test command output">{checkResult.output || "(No output)"}</pre>
          {checkResult.status === "failed" && <button className="coding-quiet" type="button" onClick={() => {
            setQuestion("Investigate this test failure and propose a fix:\n" + checkResult.output.slice(-2500));
            setNotice("Failure copied into the question. Refresh the snapshot if files have changed.");
          }}>Ask model to investigate failure</button>}
        </>}
      </section>
    </>}
  </div>;
}
