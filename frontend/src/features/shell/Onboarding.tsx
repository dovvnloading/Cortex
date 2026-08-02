import { RefreshCw } from "lucide-react";
import { useEffect, useRef } from "react";

type Props = {
  initialToken: string;
  error: string | null;
  busy: boolean;
  onSubmit: (token: string) => Promise<void>;
};

export function Onboarding({ initialToken, error, busy, onSubmit }: Props) {
  const autoSubmittedToken = useRef<string | null>(null);
  const suppliedToken = initialToken.trim();
  const canAutoConnect = Boolean(suppliedToken) && !error;

  useEffect(() => {
    if (!canAutoConnect || busy || autoSubmittedToken.current === suppliedToken) return;
    autoSubmittedToken.current = suppliedToken;
    void onSubmit(suppliedToken);
  }, [busy, canAutoConnect, onSubmit, suppliedToken]);

  return (
    <main className="onboarding" aria-labelledby="onboarding-title">
      <section className="onboarding-card">
        <div className="brand-mark" aria-hidden="true"><img src="/cortex.svg" alt="" /></div>
        <p className="eyebrow">LOCAL WORKSPACE</p>
        <h1 id="onboarding-title">{canAutoConnect ? "Opening local workspace" : "Start local workspace"}</h1>
        <p className="lede">
          {canAutoConnect
            ? "Starting your private workspace."
            : "Launch the desktop app to open this local workspace."}
        </p>
        {canAutoConnect ? (
          <div className="onboarding-auto-connect" role="status" aria-live="polite">
            <span className="loading-spinner" />
            <span>{busy ? "Connecting securely..." : "Preparing local workspace..."}</span>
          </div>
        ) : (
          <div className="stack-lg">
            {error && <p className="field-error" role="alert">{error}</p>}
            {suppliedToken && (
              <button className="button button-secondary button-wide" type="button" onClick={() => void onSubmit(suppliedToken)} disabled={busy}>
                <RefreshCw aria-hidden="true" size={16} /> {busy ? "Retrying..." : "Retry workspace startup"}
              </button>
            )}
          </div>
        )}
      </section>
    </main>
  );
}
