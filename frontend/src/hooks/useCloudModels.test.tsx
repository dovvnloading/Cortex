import { act, renderHook, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { describe, expect, it, vi } from "vitest";
import type { CloudProviderInfo } from "../../../contracts/cortex-api";
import { ApiError, type CortexApi } from "../api/client";
import { useCloudModels } from "./useCloudModels";

const first: CloudProviderInfo = { id: "first", label: "First", configured: true, setup: "Set FIRST_KEY." };
const other: CloudProviderInfo = { id: "other", label: "Other", configured: true, setup: "Set OTHER_KEY." };
const model = (id: string) => `cloud:${id}:chat`;
const failure = new ApiError(502, "The catalog could not be reached.");

function backend(providers = [first, other]) {
  const cloudProviders = vi.fn<CortexApi["cloudProviders"]>().mockResolvedValue({ providers });
  const cloudModels = vi.fn<CortexApi["cloudModels"]>().mockImplementation(async (id) => ({ models: [model(id)] }));
  return { api: { cloudModels, cloudProviders }, cloudProviders, cloudModels };
}

describe("cloud model discovery", () => {
  it("retains the last successful results on partial and registry failures, then honors an authoritative empty catalog", async () => {
    const { api, cloudModels, cloudProviders } = backend();
    const { result } = renderHook(() => useCloudModels(api, true));
    await waitFor(() => expect(result.current.models).toEqual([model("first"), model("other")]));
    cloudModels.mockImplementation(async (id) => {
      if (id === "first") throw failure;
      return { models: ["cloud:other:new"] };
    });
    await act(() => result.current.refresh());
    expect(result.current.models).toEqual([model("first"), "cloud:other:new"]);
    expect(result.current.catalogs.first.error).toBe(failure.detail);
    expect(result.current.catalogs.other.error).toBeUndefined();

    cloudProviders.mockRejectedValueOnce(failure);
    await act(() => result.current.refresh());
    expect(result.current.providers).toEqual([first, other]);
    expect(result.current.models).toEqual([model("first"), "cloud:other:new"]);
    expect(result.current.error).toBe(failure.detail);

    cloudModels.mockResolvedValue({ models: [] });
    await act(() => result.current.refresh());
    expect(result.current.models).toEqual([]);
    expect(result.current.catalogs.first.error).toBeUndefined();
    expect(result.current.error).toBeNull();
  });

  it("drops cached results when a provider is removed or disabled, even if it is enabled again", async () => {
    const { api, cloudModels, cloudProviders } = backend();
    const { result } = renderHook(() => useCloudModels(api, true));
    await waitFor(() => expect(result.current.models).toHaveLength(2));
    cloudProviders.mockResolvedValueOnce({ providers: [{ ...first, configured: false }] });
    cloudModels.mockClear();
    await act(() => result.current.refresh());
    expect(result.current.catalogs).toEqual({});
    expect(cloudModels).not.toHaveBeenCalled();

    cloudModels.mockRejectedValue(failure);
    await act(() => result.current.refresh());
    expect(result.current.models).toEqual([]);
    expect(result.current.catalogs.first.error).toBe(failure.detail);
  });

  it.each([404, 409])("invalidates cached models when the local backend rejects provider configuration (%s)", async (status) => {
    const { api, cloudModels } = backend([first]);
    const { result } = renderHook(() => useCloudModels(api, true));
    await waitFor(() => expect(result.current.models).toEqual([model("first")]));
    cloudModels.mockRejectedValueOnce(new ApiError(status, "Reconfigure this provider."));
    await act(() => result.current.refresh());
    expect(result.current.models).toEqual([]);
    expect(result.current.catalogs.first.error).toBe("Reconfigure this provider.");
  });

  it("releases a stalled catalog refresh after timeout so the next attempt can recover", async () => {
    const { api, cloudModels } = backend([first]);
    const deadline = new AbortController();
    const timeout = vi.spyOn(AbortSignal, "timeout").mockReturnValue(deadline.signal);
    try {
      cloudModels.mockImplementationOnce((_id, signal) => new Promise((_resolve, reject) => {
        signal?.addEventListener("abort", () => reject(new ApiError(0, "Cancelled.", "aborted")), { once: true });
      }));
      const { result } = renderHook(() => useCloudModels(api, true));
      await waitFor(() => expect(cloudModels).toHaveBeenCalledTimes(1));
      await act(async () => {
        const pending = result.current.refresh();
        deadline.abort();
        await pending;
      });
      expect(result.current.catalogs.first.error).toMatch(/timed out/i);
      expect(result.current.models).toEqual([]);
      timeout.mockRestore();
      await act(() => result.current.refresh());
      expect(result.current.models).toEqual([model("first")]);
      expect(result.current.catalogs.first.error).toBeUndefined();
    } finally {
      timeout.mockRestore();
    }
  });

  it("cancels in-flight discovery when disabled and ignores late replies after a fresh read", async () => {
    const { api, cloudProviders } = backend([first]);
    let oldReply!: (value: { providers: CloudProviderInfo[] }) => void;
    cloudProviders.mockImplementationOnce(() => new Promise((resolve) => { oldReply = resolve; }));
    const { result, rerender } = renderHook(({ enabled }) => useCloudModels(api, enabled), { initialProps: { enabled: true } });
    const signal = cloudProviders.mock.calls[0][0];
    rerender({ enabled: false });
    expect(signal?.aborted).toBe(true);
    rerender({ enabled: true });
    await waitFor(() => expect(result.current.models).toEqual([model("first")]));
    await act(async () => oldReply({ providers: [other] }));
    expect(result.current.providers).toEqual([first]);
  });

  it("coalesces foreground/manual discovery and completes the first enabled load in StrictMode", async () => {
    const { api, cloudModels } = backend([first]);
    const { result } = renderHook(() => useCloudModels(api, true), { wrapper: StrictMode });
    await waitFor(() => expect(result.current.models).toEqual([model("first")]));
    cloudModels.mockClear();
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
      const reading = result.current.refresh();
      expect(result.current.refresh()).toBe(reading);
      await reading;
    });
    expect(cloudModels).toHaveBeenCalledTimes(1);
  });
});
