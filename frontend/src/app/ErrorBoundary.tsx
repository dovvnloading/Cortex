import { Component, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangle, RotateCcw } from "lucide-react";

type Props = { children: ReactNode };
type State = { error: Error | null };

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("Cortex UI boundary caught an error", error, info.componentStack);
  }

  render(): ReactNode {
    if (!this.state.error) {
      return this.props.children;
    }
    return (
      <main className="fatal-state" aria-labelledby="fatal-title">
        <AlertTriangle aria-hidden="true" size={28} />
        <h1 id="fatal-title">Cortex needs a restart</h1>
        <p>The interface hit an unexpected state. Your local data was not changed.</p>
        <button className="button button-primary" onClick={() => window.location.reload()}>
          <RotateCcw aria-hidden="true" size={16} />
          Reload workspace
        </button>
      </main>
    );
  }
}
