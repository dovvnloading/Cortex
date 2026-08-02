import { beforeEach, describe, expect, it } from "vitest";
import { useSettingsStore } from "./useSettingsStore";

describe("useSettingsStore", () => {
  beforeEach(() => {
    useSettingsStore.setState({ settings: null, saving: false });
  });

  it("setSettings and setSaving update independently", () => {
    const settings = { schema_version: 1 as const, models: { chat: "qwen3:8b" } };
    useSettingsStore.getState().setSaving(true);
    useSettingsStore.getState().setSettings(settings);
    expect(useSettingsStore.getState().saving).toBe(true);
    expect(useSettingsStore.getState().settings).toBe(settings);

    useSettingsStore.getState().setSaving(false);
    expect(useSettingsStore.getState().settings).toBe(settings);
  });
});
