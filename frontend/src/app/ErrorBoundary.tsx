import { Component, useState, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangle, Copy, RotateCcw } from "lucide-react";
import { describeCrash, readLastCrash, recordCrash } from "../lib/crashLog";

type FallbackContext = {
  error: Error;
  /** Try the children again. Runs `onReset` first. */
  reset: () => void;
};

type Props = {
  children: ReactNode;
  /**
   * While a crash is showing, a change to this value tries the children again.
   * A route boundary passes the pathname, so choosing another chat or page
   * leaves the crashed view behind instead of pinning the error to the shell.
   */
  resetKey?: unknown;
  /** Which part of the app this guards. Recorded with the crash. */
  scope?: string;
  /** Rendered in place of the full-screen restart page. */
  fallback?: (context: FallbackContext) => ReactNode;
  /** Runs when the user asks to try again, before the children render again. */
  onReset?: () => void;
};
type State = { error: Error | null };

/**
 * Keeps a render crash inside the part of the app that crashed.
 *
 * With no `fallback` it is the root boundary: it takes the whole window and
 * offers a reload. Route and widget boundaries pass a `fallback` so the rest of
 * the workspace stays usable. Either way the crash is recorded for the session
 * (see lib/crashLog) before anything is offered, since a reload clears the
 * error from the screen and nothing else would remember it.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    const record = recordCrash(error, info.componentStack, this.props.scope ?? "app");
    if (import.meta.env.PROD) {
      // An error's own text comes from running code and can carry anything it
      // was holding; a console line is also what gets copied into reports. A
      // production build says what kind of error it was and where to find the
      // details the person can copy.
      console.error("Cortex UI boundary caught an error. Use Copy details on the error screen.", record.name);
      return;
    }
    // A development build runs on the developer's own machine, where the full
    // error and component stack are what they need.
    console.error("Cortex UI boundary caught an error", error, info.componentStack);
  }

  componentDidUpdate(previous: Props): void {
    if (this.state.error && !Object.is(previous.resetKey, this.props.resetKey)) this.reset();
  }

  reset = (): void => {
    this.props.onReset?.();
    this.setState({ error: null });
  };

  render(): ReactNode {
    const { error } = this.state;
    if (!error) {
      return this.props.children;
    }
    if (this.props.fallback) {
      return this.props.fallback({ error, reset: this.reset });
    }
    return (
      <main className="fatal-state" aria-labelledby="fatal-title">
        <AlertTriangle aria-hidden="true" size={28} />
        <h1 id="fatal-title">Cortex needs a restart</h1>
        <p>
          The interface hit an unexpected state. The crashed view took no further action.
          Copy the details first, because reloading clears them from the screen, then
          reload the workspace to try again. The copied details name the part of Cortex
          that failed and the kind of error, and leave out the error's own text.
        </p>
        <div className="crash-actions">
          <button className="button button-primary" onClick={() => window.location.reload()}>
            <RotateCcw aria-hidden="true" size={16} />
            Reload workspace
          </button>
          <CopyCrashDetails />
        </div>
      </main>
    );
  }
}

/**
 * Copies the recorded crash. The clipboard is the only place it goes, and only
 * when the user presses the button.
 */
function CopyCrashDetails() {
  const [outcome, setOutcome] = useState<"idle" | "copied" | "failed">("idle");
  const copy = async () => {
    const record = readLastCrash();
    const text = record ? describeCrash(record) : "No crash details were recorded for this session.";
    try {
      await navigator.clipboard.writeText(text);
      setOutcome("copied");
    } catch {
      setOutcome("failed");
    }
  };
  return (
    <>
      <button className="button button-secondary" type="button" onClick={() => void copy()}>
        <Copy aria-hidden="true" size={16} />
        Copy details
      </button>
      <span className="crash-copy-status" role="status">
        {outcome === "copied" ? "Copied." : outcome === "failed" ? "Could not copy to the clipboard." : ""}
      </span>
    </>
  );
}

/** What a crashed route shows: the shell around it is still there and still works. */
function RouteCrash({ name, reset }: { name: string; reset: () => void }) {
  return (
    <section className="route-error" role="alert" aria-labelledby="route-error-title">
      <AlertTriangle aria-hidden="true" size={24} />
      <h2 id="route-error-title">{name} hit a problem</h2>
      <p>
        The crashed view took no further action. The chat list and the rest of Cortex are
        still available: pick another chat, or try this view again.
      </p>
      <div className="crash-actions">
        <button className="button button-primary" type="button" onClick={reset}>
          <RotateCcw aria-hidden="true" size={16} />
          Try again
        </button>
        <button className="button button-secondary" type="button" onClick={() => window.location.reload()}>
          Reload workspace
        </button>
        <CopyCrashDetails />
      </div>
    </section>
  );
}

/** A route's boundary: a crash stays in that route, and Try again re-imports its code. */
export function RouteBoundary({
  name,
  scope,
  resetKey,
  onRetry,
  children,
}: {
  name: string;
  scope: string;
  resetKey: unknown;
  onRetry?: () => void;
  children: ReactNode;
}) {
  return (
    <ErrorBoundary
      scope={scope}
      resetKey={resetKey}
      onReset={onRetry}
      fallback={({ reset }) => <RouteCrash name={name} reset={reset} />}
    >
      {children}
    </ErrorBoundary>
  );
}

/**
 * A small stand-in for one widget of the shell, which keeps working around it.
 * `floating` is for a widget that is positioned over the window rather than in
 * the page flow, so its stand-in sits where it would have been.
 */
export function InlineBoundary({
  scope,
  label,
  floating = false,
  children,
}: {
  scope: string;
  label: string;
  floating?: boolean;
  children: ReactNode;
}) {
  return (
    <ErrorBoundary
      scope={scope}
      fallback={({ reset }) => (
        <div className={`inline-crash${floating ? " inline-crash-floating" : ""}`} role="alert">
          <span>{label} could not be shown.</span>
          <button className="button button-quiet" type="button" onClick={reset}>Try again</button>
        </div>
      )}
    >
      {children}
    </ErrorBoundary>
  );
}
