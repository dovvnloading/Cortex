import { useCallback, useEffect, useRef, useState } from "react";
import type { CloudProviderInfo } from "../../../contracts/cortex-api";
import { ApiError, type CortexApi, describeApiError } from "../api/client";
import { useVisiblePolling } from "./useVisiblePolling";

type ProviderCatalog = { models: string[]; error?: string };

export function useCloudModels(api: Pick<CortexApi, "cloudProviders" | "cloudModels">, enabled: boolean) {
  const [providers, setProviders] = useState<CloudProviderInfo[] | null>(null);
  const [catalogs, setCatalogs] = useState<Record<string, ProviderCatalog>>({});
  const [error, setError] = useState<string | null>(null);
  const active = useRef<{ controller: AbortController; promise: Promise<void> } | null>(null);

  const refresh = useCallback((): Promise<void> => {
    if (!enabled) return Promise.resolve();
    if (active.current) return active.current.promise;
    const controller = new AbortController();
    // A stalled local request must release the shared refresh so a retry can run.
    const timeout = AbortSignal.timeout(120_000);
    const signal = AbortSignal.any([controller.signal, timeout]);
    const promise = (async () => {
      try {
        const result = await api.cloudProviders(signal);
        if (controller.signal.aborted) return;
        setError(null);
        setProviders((previous) => JSON.stringify(previous) === JSON.stringify(result.providers) ? previous : result.providers);

        const configured = result.providers.filter((provider) => provider.configured);
        const ids = new Set(configured.map((provider) => provider.id));
        setCatalogs((previous) => {
          if (Object.keys(previous).every((id) => ids.has(id))) return previous;
          return Object.fromEntries(Object.entries(previous).filter(([id]) => ids.has(id)));
        });
        await Promise.all(configured.map(async ({ id, label }) => {
          let models: string[] | undefined;
          let error: string | undefined;
          try {
            ({ models } = await api.cloudModels(id, signal));
          } catch (cause) {
            // These are local, definitive rejections. A 502 or transport error
            // only says that the latest inventory check failed.
            if (cause instanceof ApiError && (cause.status === 404 || cause.status === 409)) models = [];
            error = timeout.aborted ? `The ${label} model list timed out. Try refreshing again.` :
              describeApiError(cause, "Could not load this provider’s models.");
          }
          if (controller.signal.aborted) return;
          setCatalogs((previous) => {
            const catalog = { models: models ?? previous[id]?.models ?? [], error };
            return JSON.stringify(previous[id]) === JSON.stringify(catalog) ? previous : { ...previous, [id]: catalog };
          });
        }));
      } catch (cause) {
        if (!controller.signal.aborted) {
          setProviders((previous) => previous ?? []);
          setError(timeout.aborted ? "The cloud provider list timed out. Try refreshing again." :
            describeApiError(cause, "Could not load cloud providers."));
        }
      }
    })();
    active.current = { controller, promise };
    void promise.finally(() => {
      if (active.current?.controller === controller) active.current = null;
    });
    return promise;
  }, [api, enabled]);

  useEffect(() => {
    // Pair the first request with its cleanup so StrictMode's second setup
    // restarts the request it just aborted. The visibility poll shares it.
    if (enabled) void refresh();
    return () => {
      active.current?.controller.abort();
      active.current = null;
    };
  }, [enabled, refresh]);

  useVisiblePolling(refresh, 5 * 60_000, enabled);
  const models = (providers ?? []).flatMap(({ id, configured }) => configured ? catalogs[id]?.models ?? [] : []);
  return { providers, models, catalogs, error, refresh };
}

export type CloudCatalog = ReturnType<typeof useCloudModels>;
