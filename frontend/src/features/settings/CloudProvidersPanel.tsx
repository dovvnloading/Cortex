import { useState } from "react";
import type { CloudCatalog } from "../../hooks/useCloudModels";
import { Select } from "../../shared/ui/Select";

export function CloudProvidersPanel({ cloud, selectedProviderId }: { cloud: CloudCatalog; selectedProviderId?: string | null }) {
  const [selectedId, setSelectedId] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const refresh = async () => {
    setRefreshing(true);
    try { await cloud.refresh(); }
    finally { setRefreshing(false); }
  };
  const providers = cloud.providers ?? [];
  const selected = providers.find(({ id }) => id === selectedId) ??
    providers.find(({ id }) => id === selectedProviderId) ??
    providers.find(({ configured }) => configured) ?? providers[0];
  const catalog = selected ? cloud.catalogs[selected.id] : undefined;
  const count = catalog?.models.length ?? 0;
  const countLabel = `${count} chat model${count === 1 ? "" : "s"}`;
  let modelStatus = "Checking available models…";
  if (cloud.error || catalog?.error) {
    modelStatus = count > 0 ? `Last successful check found ${countLabel}.` : "";
  } else if (catalog) {
    modelStatus = count > 0 ? `${countLabel} available` : "No chat models returned. Check the models or deployments available to this account.";
  }

  return (
    <section className="settings-group cloud-provider-settings" aria-labelledby="cloud-providers-title">
      <div className="settings-group-head">
        <span>
          <strong id="cloud-providers-title">Cloud providers</strong>
          <small>Set the provider’s environment variables on the computer running Cortex, then restart the app.</small>
        </span>
        <button className="button button-secondary" type="button" disabled={refreshing} onClick={() => void refresh()}>
          {refreshing ? "Refreshing…" : "Refresh cloud models"}
        </button>
      </div>
      {cloud.error && <p className="field-error" role="status">{cloud.error}</p>}
      {cloud.providers === null && <p className="muted-note" role="status">Loading cloud providers…</p>}
      {cloud.providers?.length === 0 && !cloud.error && (
        <p className="muted-note" role="status">No cloud providers are registered.</p>
      )}
      {selected && (
        <>
          <Select aria-label="Cloud provider" value={selected.id} options={providers.map(({ id, label, configured }) => ({
            value: id, label, detail: !configured ? "Setup required" : cloud.catalogs[id]?.error ? "Refresh failed" : "Configured",
          }))} onChange={setSelectedId} />
          <p className="muted-note">{selected.setup}</p>
          {selected.configured && (
            <div role="status">
              {catalog?.error && <p className="field-error">{catalog.error}</p>}
              {modelStatus && <p className="muted-note">{modelStatus}</p>}
            </div>
          )}
        </>
      )}
    </section>
  );
}
