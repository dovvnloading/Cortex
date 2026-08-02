import { create } from "zustand";
import type { CortexSettings } from "../../../contracts/cortex-api";

interface SettingsStoreState {
  settings: CortexSettings | null;
  saving: boolean;
  setSettings: (settings: CortexSettings | null) => void;
  setSaving: (saving: boolean) => void;
}

export const useSettingsStore = create<SettingsStoreState>((set) => ({
  settings: null,
  saving: false,
  setSettings: (settings) => set({ settings }),
  setSaving: (saving) => set({ saving }),
}));
