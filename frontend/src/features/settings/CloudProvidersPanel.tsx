import { useState } from "react";
import type { CloudCatalog } from "../../hooks/useCloudModels";
import { Select } from "../../shared/ui/Select";

export function CloudProvidersPanel({ cloud }: { cloud: CloudCatalog }) {
  const [selectedId, setSelectedId] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const refresh = async () => {
    setRefreshing(true);
    try { await cloud.refresh(); }
    finally { setRefreshing(false); }
  };
  const providers = cloud.providers ?? [];
  const selected = providers.find(({ id }) => id === selectedId) ?? providers.find(({ configured }) => configured) ?? providers[0];
  const catalog = selected ? cloud.catalogs[selected.id] : undefined;
  return (
    <section className="settings-group" aria-labelledby="cloud-providers-title">
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
      {selected && (
        <>
          <Select aria-label="Cloud provider" value={selected.id} options={providers.map(({ id, label, configured }) => ({
            value: id, label, detail: configured ? "Configured" : "Setup required",
          }))} onChange={setSelectedId} />
          <p className="muted-note">{selected.setup}</p>
          {selected.configured && (
            <p className={catalog?.error ? "field-error" : "muted-note"} role="status">
              {catalog?.error ?? (catalog ? `${catalog.models.length} chat model${catalog.models.length === 1 ? "" : "s"} available` : "Checking available models…")}
            </p>
          )}
        </>
      )}
    </section>
  );
}
