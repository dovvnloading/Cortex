import { CheckCircle2, CircleAlert, Cpu, Server } from "lucide-react";
import type { SystemResponse } from "../../../contracts/cortex-api";

export function SystemStatusCard({ system }: { system: SystemResponse }) {
  const healthy = system.status === "ok";
  return (
    <section className="panel status-panel" aria-labelledby="system-status-title">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">SYSTEM STATUS</p>
          <h2 id="system-status-title">Local runtime</h2>
        </div>
        <span className={`status-pill ${healthy ? "status-success" : "status-danger"}`}>
          {healthy ? <CheckCircle2 aria-hidden="true" size={15} /> : <CircleAlert aria-hidden="true" size={15} />}
          {healthy ? "Ready" : "Attention"}
        </span>
      </div>
      <div className="status-grid">
        <div className="status-item">
          <Server aria-hidden="true" size={17} />
          <span><strong>API</strong><small>Version {system.api_version ?? "v1"}</small></span>
        </div>
        <div className="status-item">
          <Cpu aria-hidden="true" size={17} />
          <span><strong>Runtime</strong><small>{system.preview ? "Preview mode" : "Local mode"}</small></span>
        </div>
      </div>
      <p className="muted-note">
        {system.qt_default ? "The native Qt path remains the default launcher." : "The web path is the active launcher."}
      </p>
    </section>
  );
}
