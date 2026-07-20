import { KeyRound, ShieldCheck } from "lucide-react";
import { useState, type FormEvent } from "react";

type Props = {
  initialToken: string;
  error: string | null;
  busy: boolean;
  onSubmit: (token: string) => Promise<void>;
};

export function Onboarding({ initialToken, error, busy, onSubmit }: Props) {
  const [token, setToken] = useState(initialToken);

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void onSubmit(token.trim());
  };

  return (
    <main className="onboarding" aria-labelledby="onboarding-title">
      <section className="onboarding-card">
        <div className="brand-mark" aria-hidden="true">C</div>
        <p className="eyebrow">LOCAL WORKSPACE</p>
        <h1 id="onboarding-title">Connect to Cortex</h1>
        <p className="lede">
          Use the one-time token printed by the local Cortex preview launcher.
          The browser session stays on this machine.
        </p>
        <div className="privacy-note">
          <ShieldCheck aria-hidden="true" size={18} />
          <span>Loopback-only access with a short-lived session.</span>
        </div>
        <form onSubmit={handleSubmit} className="stack-lg">
          <label className="field-label" htmlFor="bootstrap-token">Launcher token</label>
          <div className="input-with-icon">
            <KeyRound aria-hidden="true" size={17} />
            <input
              id="bootstrap-token"
              value={token}
              onChange={(event) => setToken(event.target.value)}
              autoComplete="off"
              spellCheck={false}
              placeholder="Paste the local launcher token"
              required
            />
          </div>
          {error && <p className="field-error" role="alert">{error}</p>}
          <button className="button button-primary button-wide" disabled={busy || !token.trim()}>
            {busy ? "Connecting…" : "Open workspace"}
          </button>
        </form>
      </section>
    </main>
  );
}
