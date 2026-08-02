import { beforeEach, describe, expect, it } from "vitest";
import { useModelStore } from "./useModelStore";

describe("useModelStore", () => {
  beforeEach(() => {
    useModelStore.setState({ models: null, modelBusy: false, modelProgress: null });
  });

  it("setModels replaces the inventory wholesale", () => {
    const response = { required_models: [], optional_models: [], installed_models: ["a"] };
    useModelStore.getState().setModels(response);
    expect(useModelStore.getState().models).toBe(response);
  });

  it("setModelBusy and setModelProgress track a pull/check job independently of models", () => {
    useModelStore.getState().setModelBusy(true);
    useModelStore.getState().setModelProgress({ model: "qwen3:8b", status: "Downloading...", percent: 42 });
    expect(useModelStore.getState().modelBusy).toBe(true);
    expect(useModelStore.getState().modelProgress).toEqual({ model: "qwen3:8b", status: "Downloading...", percent: 42 });
    expect(useModelStore.getState().models).toBeNull();
  });
});
