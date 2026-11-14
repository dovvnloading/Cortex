import { useCallback, useEffect, useRef, useState } from "react";
import type { CloudProviderInfo } from "../../../contracts/cortex-api";
import { CortexApi, describeApiError } from "../api/client";
import { useVisiblePolling } from "./useVisiblePolling";

type ProviderCatalog = { models: string[]; error?: string };

export function useCloudModels(api: CortexApi, enabled: boolean) {
  const [providers, setProviders] = useState<CloudProviderInfo[] | null>(null);
  const [catalogs, setCatalogs] = useState<Record<string, ProviderCatalog>>({});
  const [error, setError] = useState<string | null>(null);
  const active = useRef<{ controller: AbortController; promise: Promise<void> } | null>(null);

  useEffect(() => () => {
    active.current?.controller.abort();
    active.current = null;
  }, []);

  const refresh = useCallback((): Promise<void> => {
    if (active.current) return active.current.promise;
    const controller = new AbortController();
    const { signal } = controller;
    const promise = (async () => {
      try {
        const result = await api.cloudProviders(signal);
        if (signal.aborted) return;
        setError(null);
        setProviders((previous) => JSON.stringify(previous) === JSON.stringify(result.providers) ? previous : result.providers);
        await Promise.all(result.providers.filter((provider) => provider.configured).map(async ({ id }) => {
          let catalog: ProviderCatalog;
          try {
            const { models } = await api.cloudModels(id, signal);
            catalog = { models };
          } catch (cause) {
            catalog = { models: [], error: describeApiError(cause, "Could not load this provider’s models.") };
          }
          if (!signal.aborted) {
            setCatalogs((current) => JSON.stringify(current[id]) === JSON.stringify(catalog) ? current : { ...current, [id]: catalog });
          }
        }));
      } catch (cause) {
        if (!signal.aborted) {
          setProviders((previous) => previous?.length === 0 ? previous : []);
          setError(describeApiError(cause, "Could not load cloud providers."));
        }
      }
    })();
    active.current = { controller, promise };
    void promise.finally(() => {
      if (active.current?.controller === controller) active.current = null;
    });
    return promise;
  }, [api]);

  // The first read is local. Only providers configured on the backend receive catalog requests.
  useVisiblePolling(refresh, 5 * 60_000, enabled);
  const models = (providers ?? []).flatMap(({ id, configured }) => configured ? catalogs[id]?.models ?? [] : []);
  return { providers, models, catalogs, error, refresh };
}

export type CloudCatalog = ReturnType<typeof useCloudModels>;
